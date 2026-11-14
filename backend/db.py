"""Database plumbing shared by the kiosk and the central service.

- SQLAlchemy 2.x engines. SQLite connections get WAL, synchronous=NORMAL and
  foreign_keys=ON on every connect.
- One metadata naming convention, so constraint names are stable across
  migrations and across SQLite and PostgreSQL.
- UTCDateTime: timezone-aware UTC in Python, naive UTC in the database.
- Alembic helpers for the two separate histories (migrations/kiosk and
  migrations/central), and the kiosk's backup-then-upgrade startup.
"""
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import DateTime, MetaData, create_engine, event
from sqlalchemy.types import TypeDecorator

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("spoof.db")

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def new_metadata():
    return MetaData(naming_convention=NAMING_CONVENTION)


def utcnow():
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Always aware UTC in Python; stored as naive UTC."""
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime given; use timezone-aware UTC")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


def sqlite_url(path):
    return f"sqlite:///{Path(path).resolve().as_posix()}"


def make_engine(url):
    is_sqlite = url.startswith("sqlite")
    engine = create_engine(
        url, connect_args={"check_same_thread": False, "timeout": 15} if is_sqlite else {})
    if is_sqlite:
        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    return engine


# ---- Alembic -------------------------------------------------------------

def alembic_config(which, url):
    """which: "kiosk" or "central"."""
    from alembic.config import Config
    cfg = Config(str(ROOT / f"alembic_{which}.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations" / which))
    cfg.attributes["url"] = url
    return cfg


def upgrade(which, url, revision="head"):
    from alembic import command
    command.upgrade(alembic_config(which, url), revision)


def downgrade(which, url, revision="base"):
    from alembic import command
    command.downgrade(alembic_config(which, url), revision)


def _sqlite_copy(src, dst):
    """Consistent copy of a live SQLite database (includes the WAL)."""
    s, d = sqlite3.connect(str(src)), sqlite3.connect(str(dst))
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def kiosk_startup(db_path, keep=5, upgrade_fn=None):
    """Back up data/kiosk.db, then upgrade it to head.

    On failure the backup is restored and the error is returned (the kiosk
    then starts LOCKED). Returns None on success.
    """
    db_path = Path(db_path)
    backup_dir = db_path.parent / "backup"
    backup = None
    if db_path.exists():
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = backup_dir / f"kiosk-{stamp}.db"
        _sqlite_copy(db_path, backup)
        for old in sorted(backup_dir.glob("kiosk-*.db"))[:-keep]:
            old.unlink()
    try:
        (upgrade_fn or (lambda: upgrade("kiosk", sqlite_url(db_path))))()
        return None
    except Exception as e:  # noqa: BLE001 - reported, kiosk starts LOCKED
        err = f"{type(e).__name__}: {e}"
        log.error("kiosk database upgrade failed: %s", err)
        if backup is not None:
            _sqlite_copy(backup, db_path)
            log.error("restored kiosk database from %s", backup.name)
        return err
