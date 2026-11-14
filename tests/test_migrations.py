"""Both migration histories: upgrade head, downgrade base, upgrade head, and
the migrated schema matches the SQLAlchemy models (no drift)."""
import json

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from backend.db import kiosk_startup, make_engine, sqlite_url, downgrade, upgrade


def _histories():
    from backend.kiosk_models import KioskBase
    out = [("kiosk", KioskBase.metadata)]
    try:
        from central.models import CentralBase
        out.append(("central", CentralBase.metadata))
    except ImportError:
        pass
    return out


@pytest.mark.parametrize("which,metadata", _histories(), ids=lambda v: v if isinstance(v, str) else "")
def test_upgrade_downgrade_upgrade(tmp_path, which, metadata):
    url = sqlite_url(tmp_path / f"{which}.db")
    upgrade(which, url)
    engine = make_engine(url)
    tables = set(inspect(engine).get_table_names())
    assert set(metadata.tables) <= tables
    engine.dispose()

    downgrade(which, url)
    engine = make_engine(url)
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    engine.dispose()

    upgrade(which, url)
    engine = make_engine(url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), metadata)
    engine.dispose()
    assert diff == [], f"models and migrations differ: {diff}"


def test_sqlite_pragmas_are_set(tmp_path):
    engine = make_engine(sqlite_url(tmp_path / "p.db"))
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert conn.exec_driver_sql("PRAGMA synchronous").scalar() == 1  # NORMAL
    engine.dispose()


def test_startup_backs_up_and_restores_on_failed_upgrade(tmp_path):
    db = tmp_path / "kiosk.db"
    assert kiosk_startup(db) is None          # fresh database, nothing to back up
    from backend.store import Store
    store = Store(tmp_path, migrate=False)
    store.add_user("Alice", [0.1] * 512)
    store.engine.dispose()

    assert kiosk_startup(db) is None
    assert len(list((tmp_path / "backup").glob("kiosk-*.db"))) == 1

    def broken():
        import sqlite3
        c = sqlite3.connect(db)
        c.execute("DROP TABLE users_cache")   # damage, then fail
        c.commit()
        c.close()
        raise RuntimeError("migration exploded")

    err = kiosk_startup(db, upgrade_fn=broken)
    assert err and "migration exploded" in err
    restored = Store(tmp_path, migrate=False)
    assert [u["name"] for u in restored.users()] == ["Alice"]   # backup restored


def test_backups_keep_only_last_five(tmp_path):
    db = tmp_path / "kiosk.db"
    kiosk_startup(db)
    for _ in range(7):
        kiosk_startup(db)
    assert len(list((tmp_path / "backup").glob("kiosk-*.db"))) == 5


def test_import_json_is_idempotent(tmp_path):
    import sys
    sys.path.insert(0, "tools")
    from import_json import import_json
    from backend.store import Store
    (tmp_path / "gallery.json").write_text(json.dumps({"users": [
        {"id": "u_001", "name": "Alice", "standing": "active", "embedding": [0.1] * 512},
        {"id": "u_002", "name": "Carol", "standing": "suspended", "embedding": [0.2] * 512}]}))
    (tmp_path / "decisions.jsonl").write_text(
        json.dumps({"request_id": "r1", "stage": "verify", "ts": "t1", "decision": "ALLOW"}) + "\n"
        + json.dumps({"request_id": "r2", "stage": "verify", "ts": "t2", "decision": "DENY"}) + "\n")
    store = Store(tmp_path)
    first = import_json(store)
    second = import_json(store)
    assert (first["users_imported"], first["log_imported"]) == (2, 2)
    assert (second["users_imported"], second["log_imported"]) == (0, 0)
    assert (second["users_skipped"], second["log_skipped"]) == (2, 2)
    assert store.get_user("u_002")["standing"] == "suspended"
    assert len(store.read_decisions()) == 2
    assert (tmp_path / "gallery.json").exists() and (tmp_path / "decisions.jsonl").exists()
