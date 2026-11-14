"""Central API: auth, snapshot content and signature, 304, intake idempotency,
registry changes bump the snapshot version, seed script idempotency."""
import uuid

import numpy as np
import pytest

from backend.outbox import GENESIS, record_hash
from backend.signing import signature_ok
from central_helpers import HMAC_KEY, Central


@pytest.fixture
def central(tmp_path):
    c = Central(tmp_path)
    yield c
    c.close()


def emb(seed):
    v = np.random.default_rng(seed).normal(size=512).astype(np.float32)
    return (v / np.linalg.norm(v)).tolist()


def wire_record(seq, prev, user_id="u_001", final="ALLOW", state="ONLINE", ts=None):
    from central_helpers import T0
    rec = {"record_id": str(uuid.uuid4()), "seq": seq, "request_id": uuid.uuid4().hex,
           "stepup_of": None, "ts": (ts or T0).isoformat(timespec="microseconds"),
           "room_id": "lab_a", "user_id": user_id, "bio_decision": "ALLOW",
           "final_decision": final, "reason": "x", "kiosk_state": state,
           "snapshot_version": 1, "liveness": 0.9, "similarity": 97.0, "latency_ms": 300.0,
           "liveness_mode": "ensemble", "prev_hash": prev}
    rec["record_hash"] = record_hash(prev, rec)
    return rec


def test_health(central):
    body = central.client.get("/health").json()
    assert body["ok"] is True and "time" in body


def test_kiosk_calls_need_a_valid_key(central):
    assert central.client.get("/snapshot", params={"kiosk_id": "kiosk_lab_a_1"}).status_code == 401
    assert central.client.get("/snapshot", params={"kiosk_id": "kiosk_lab_a_1"},
                              headers={"X-Kiosk-Key": "nope"}).status_code == 401
    assert central.client.post("/decisions", json={"records": []}).status_code == 401
    assert central.client.get("/snapshot", params={"kiosk_id": "other"},
                              headers=central.headers()).status_code == 403


def test_snapshot_is_signed_scoped_and_supports_304(central):
    alice = central.add_user("Alice", emb(1))                       # rule for lab_a
    central.add_user("Bob", emb(2), rules=[{"room_id": "hostel_gate", "days_mask": 127,
                                            "start_min": 0, "end_min": 0}])
    carol = central.add_user("Carol", emb(3), standing="suspended", rules=[])
    r = central.client.get("/snapshot", params={"kiosk_id": "kiosk_lab_a_1"},
                           headers=central.headers())
    snap = r.json()
    body = snap["body"]
    assert signature_ok(body, snap["signature"], HMAC_KEY)
    assert not signature_ok(body, snap["signature"], "other-key")
    ids = {u["user_id"] for u in body["users"]}
    assert ids == {alice, carol}            # Bob has no lab_a rule and is not suspended
    assert body["ttl_s"] == 600 and body["room_id"] == "lab_a" and body["schema_version"] == 1
    assert all(u["pin_hash"] is None or len(u["pin_hash"]) == 64 for u in body["users"])
    assert "4321" not in r.text                                    # PIN only as a hash
    assert all(rule["room_id"] == "lab_a" for rule in body["schedule_rules"])
    v = body["version"]
    again = central.client.get("/snapshot", params={"kiosk_id": "kiosk_lab_a_1", "since": v},
                               headers=central.headers())
    assert again.status_code == 304
    central.client.patch(f"/users/{alice}", json={"standing": "suspended"})
    newer = central.client.get("/snapshot", params={"kiosk_id": "kiosk_lab_a_1", "since": v},
                               headers=central.headers())
    assert newer.status_code == 200 and newer.json()["body"]["version"] == v + 1


def test_decisions_are_idempotent(central):
    a = wire_record(1, GENESIS)
    b = wire_record(2, a["record_hash"])
    first = central.client.post("/decisions", json={"records": [a, b]},
                                headers=central.headers()).json()
    assert first["accepted"] == [a["record_id"], b["record_id"]] and first["duplicates"] == 0
    second = central.client.post("/decisions", json={"records": [a, b]},
                                 headers=central.headers()).json()
    assert second["accepted"] == [] and second["duplicates"] == 2
    assert len(central.client.get("/decisions").json()) == 2


def test_decisions_reject_unknown_fields_and_bad_values(central):
    rec = wire_record(1, GENESIS)
    assert central.client.post("/decisions", json={"records": [{**rec, "frames": ["x"]}]},
                               headers=central.headers()).status_code == 422
    assert central.client.post("/decisions", json={"records": [{**rec, "final_decision": "MAYBE"}]},
                               headers=central.headers()).status_code == 422


def test_enrol_via_kiosk_key_creates_rule_and_bumps_version(central):
    v0 = central.client.get("/summary").json()["snapshot_version"]
    r = central.client.post("/users", json={"name": "Dana", "embedding": emb(5), "pin": "2468"},
                            headers=central.headers())
    assert r.status_code == 200
    uid = r.json()["user_id"]
    assert r.json()["snapshot_version"] == v0 + 1
    users = {u["id"]: u for u in central.client.get("/users").json()}
    assert users[uid]["rules"][0]["room_id"] == "lab_a" and users[uid]["has_pin"]
    assert central.client.post("/users", json={"name": "X", "embedding": emb(5), "pin": "12"},
                               headers=central.headers()).status_code == 422
    assert central.client.post("/users", json={"name": "X", "embedding": emb(5), "pin": "1234"}
                               ).status_code == 401


def test_standing_change_writes_event(central):
    from sqlalchemy import select
    from central.models import StandingEvent
    uid = central.add_user("Eve", emb(6))
    central.clock.advance(minutes=5)
    central.client.patch(f"/users/{uid}", json={"standing": "suspended"})
    with central.Session() as s:
        events = s.scalars(select(StandingEvent).where(StandingEvent.user_id == uid)
                           .order_by(StandingEvent.id)).all()
    assert [(e.old_standing, e.new_standing) for e in events] == [(None, "active"),
                                                                  ("active", "suspended")]
    assert events[1].at == central.clock()
    assert central.client.patch("/users/u_999", json={"standing": "active"}).status_code == 404


def test_central_refuses_to_start_on_an_old_schema(tmp_path):
    from fastapi.testclient import TestClient
    from backend.db import sqlite_url
    from central.main import create_app
    app = create_app(sqlite_url(tmp_path / "empty.db"), HMAC_KEY)
    with pytest.raises(RuntimeError, match="alembic -c alembic_central.ini upgrade head"):
        with TestClient(app):
            pass


def test_seed_is_idempotent(tmp_path):
    import json
    import sys
    sys.path.insert(0, "tools")
    from seed_central import seed
    from backend.db import sqlite_url, upgrade
    url = sqlite_url(tmp_path / "c.db")
    upgrade("central", url)
    gallery = tmp_path / "gallery.json"
    gallery.write_text(json.dumps({"users": [
        {"id": "u_001", "name": "Alice", "standing": "active", "embedding": emb(1)}]}))
    lines = []
    first = seed(url, gallery, write_env=tmp_path / "kiosk.env", out=lines.append)
    assert "api_key" in first and len(first["pins"]) == 1
    assert first["api_key"] in (tmp_path / "kiosk.env").read_text()
    second = seed(url, gallery, out=lines.append)
    assert second == {}                       # nothing new, no secrets shown again
    from sqlalchemy import create_engine, text
    e = create_engine(url)
    with e.connect() as c:
        counts = {t: c.execute(text(f"select count(*) from {t}")).scalar()
                  for t in ("rooms", "users", "kiosks", "exam_windows", "schedule_rules")}
        stored = c.execute(text("select api_key_hash from kiosks")).scalar()
    e.dispose()
    assert counts == {"rooms": 2, "users": 2, "kiosks": 1, "exam_windows": 1, "schedule_rules": 2}
    assert stored != first["api_key"] and len(stored) == 64          # only the hash is stored
