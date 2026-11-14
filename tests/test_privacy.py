"""No raw biometrics in the audit trail: the outbox, the local log and the
central decisions/alerts tables have no binary or embedding columns, and a
full verify run leaves no image files on disk."""
import json

import pytest
from sqlalchemy import LargeBinary

from backend.kiosk_models import DecisionLog, DecisionOutbox
from central.models import Alert, Decision
from test_pipeline import MODELS, NEEDED, b64, burst

FORBIDDEN = ("embedding", "frame", "image", "photo", "template", "pin")


@pytest.mark.parametrize("model", [DecisionOutbox, DecisionLog, Decision, Alert],
                         ids=lambda m: m.__tablename__)
def test_audit_tables_have_no_blob_or_biometric_columns(model):
    for col in model.__table__.columns:
        assert not isinstance(col.type, LargeBinary), f"{model.__tablename__}.{col.name} is binary"
        assert not any(w in col.name.lower() for w in FORBIDDEN), \
            f"{model.__tablename__}.{col.name} looks like biometric data"


def test_migrated_outbox_has_no_blob_column(tmp_path):
    import sqlite3
    from backend.store import Store
    Store(tmp_path)
    con = sqlite3.connect(tmp_path / "kiosk.db")
    types = {r[1]: r[2].upper() for r in con.execute("pragma table_info(decision_outbox)")}
    con.close()
    assert types and not [n for n, t in types.items() if "BLOB" in t]


@pytest.mark.skipif(not all(p.exists() for p in NEEDED + MODELS), reason="models or samples missing")
def test_full_verify_run_writes_no_images_and_no_embeddings(tmp_path):
    import cv2
    from fastapi.testclient import TestClient
    from backend import main
    from backend.decision import ONLINE
    from backend.pipeline import Pipeline
    from backend.state import StaticState
    from backend.store import Store
    img = cv2.imread(str(NEEDED[0]))
    with TestClient(main.app) as c:
        main.state["pipeline"] = Pipeline(Store(tmp_path), state=StaticState(ONLINE, 1))
        assert c.post("/api/enrol", json={"name": "A", "frames": b64(burst(img))}).status_code == 200
        for seed in range(3):
            body = c.post("/api/verify", json={"frames": b64(burst(img, seed=seed))}).json()
            assert body["final_decision"]
    images = [p for p in tmp_path.rglob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp")]
    assert images == []
    store = main.state["pipeline"].store
    log_text = json.dumps(store.read_decisions())
    assert "embedding" not in log_text and len(log_text) < 20_000
    import sqlite3
    con = sqlite3.connect(tmp_path / "kiosk.db")
    longest = max(len(str(v)) for row in con.execute("select * from decision_outbox") for v in row)
    con.close()
    assert longest <= 64                       # ids and hashes; nothing like base64 images
