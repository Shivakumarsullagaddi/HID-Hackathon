"""Kiosk side of the snapshot: apply, reject, ignore, roll back."""
import base64
import copy
import json
import os
import subprocess
import sys
from datetime import timedelta

import numpy as np
import pytest

from backend import config
from backend.signing import sign_body
from backend.snapshot import apply, stored_signature_ok
from backend.store import Store
from central_helpers import HMAC_KEY, T0

KIOSK = "kiosk_lab_a_1"


def emb_b64(seed):
    v = np.random.default_rng(seed).normal(size=512).astype(np.float32)
    return base64.b64encode((v / np.linalg.norm(v)).tobytes()).decode()


def make_snapshot(version, users=("u_001",), issued=T0, schema=1, kiosk=KIOSK, key=HMAC_KEY,
                  model_id=config.FACE_MODEL_ID):
    body = {
        "schema_version": schema, "version": version, "issued_at": issued.isoformat(),
        "ttl_s": 600, "kiosk_id": kiosk, "room_id": "lab_a",
        "users": [{"user_id": u, "name": u.upper(), "standing": "active",
                   "pin_salt": None, "pin_hash": None} for u in users],
        "templates": [{"user_id": u, "model_id": model_id, "embedding_b64": emb_b64(i)}
                      for i, u in enumerate(users)],
        "schedule_rules": [{"id": i + 1, "user_id": u, "room_id": "lab_a", "days_mask": 127,
                            "start_min": 540, "end_min": 1020} for i, u in enumerate(users)],
        "exam_windows": [{"id": 1, "room_id": "lab_a", "starts_at": T0.isoformat(),
                          "ends_at": (T0 + timedelta(hours=2)).isoformat()}],
    }
    return {"alg": "HMAC-SHA256", "body": body, "signature": sign_body(body, key)}


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path)
    assert apply(s, make_snapshot(1, ("u_001", "u_002")), HMAC_KEY, KIOSK, T0)["applied"]
    return s


def names(store):
    return sorted(u["id"] for u in store.users())


def test_valid_apply_replaces_cache(store):
    assert names(store) == ["u_001", "u_002"]
    r = apply(store, make_snapshot(2, ("u_003",)), HMAC_KEY, KIOSK, T0)
    assert r["applied"] and r["version"] == 2
    assert names(store) == ["u_003"]
    meta = store.snapshot_meta()
    assert (meta.version, meta.ttl_s, meta.room_id) == (2, 600, "lab_a")
    assert store.matchable_users()[0]["embedding"].shape == (512,)
    assert stored_signature_ok(store, HMAC_KEY) is True


def test_tampered_signature_rejected_and_old_kept(store):
    snap = make_snapshot(2, ("u_666",))
    snap["body"]["users"][0]["standing"] = "active"
    snap["body"]["users"][0]["name"] = "Mallory"     # changed after signing
    r = apply(store, snap, HMAC_KEY, KIOSK, T0)
    assert not r["applied"] and r["reason"] == "invalid signature"
    assert names(store) == ["u_001", "u_002"] and store.snapshot_meta().version == 1


def test_wrong_key_rejected(store):
    r = apply(store, make_snapshot(2, key="attacker-key"), HMAC_KEY, KIOSK, T0)
    assert not r["applied"] and r["reason"] == "invalid signature"


def test_unsupported_schema_version_rejected(store):
    r = apply(store, make_snapshot(2, schema=99), HMAC_KEY, KIOSK, T0)
    assert not r["applied"] and "schema_version" in r["reason"]
    assert store.snapshot_meta().version == 1


def test_snapshot_for_another_kiosk_rejected(store):
    r = apply(store, make_snapshot(2, kiosk="kiosk_other"), HMAC_KEY, KIOSK, T0)
    assert not r["applied"] and r["rejected"]


def test_older_version_ignored(store):
    apply(store, make_snapshot(5, ("u_005",)), HMAC_KEY, KIOSK, T0)
    r = apply(store, make_snapshot(4, ("u_004",)), HMAC_KEY, KIOSK, T0)
    assert not r["applied"] and not r["rejected"] and r["reason"] == "not newer"
    assert names(store) == ["u_005"]


def test_same_version_later_issued_at_refreshes(store):
    later = T0 + timedelta(minutes=5)
    r = apply(store, make_snapshot(1, ("u_001", "u_002"), issued=later), HMAC_KEY, KIOSK, later)
    assert r["applied"] and store.snapshot_meta().issued_at == later
    r = apply(store, make_snapshot(1, ("u_001", "u_002"), issued=later), HMAC_KEY, KIOSK, later)
    assert not r["applied"]                          # same version, same issued_at


def test_failed_apply_rolls_back_and_keeps_old(store):
    def boom(stage):
        if stage == "before_meta":
            raise RuntimeError("disk full (simulated)")
    r = apply(store, make_snapshot(2, ("u_009",)), HMAC_KEY, KIOSK, T0, fail_hook=boom)
    assert not r["applied"] and "disk full" in r["reason"]
    assert names(store) == ["u_001", "u_002"] and store.snapshot_meta().version == 1
    with store.session() as s:
        from backend.kiosk_models import ExamWindow, ScheduleRule
        assert s.query(ScheduleRule).count() == 2 and s.query(ExamWindow).count() == 1


def test_malformed_body_rolls_back(store):
    snap = make_snapshot(2, ("u_009",))
    del snap["body"]["templates"][0]["embedding_b64"]
    snap["signature"] = sign_body(snap["body"], HMAC_KEY)   # validly signed but broken
    r = apply(store, snap, HMAC_KEY, KIOSK, T0)
    assert not r["applied"] and names(store) == ["u_001", "u_002"]


def test_templates_from_another_model_are_refused(store):
    r = apply(store, make_snapshot(2, ("u_007",), model_id="some/other-model"), HMAC_KEY, KIOSK, T0)
    assert r["applied"] and r["templates_refused"] == 1
    assert names(store) == ["u_007"] and store.matchable_users() == []


def test_tampered_stored_snapshot_is_detected(store):
    with store.transaction() as s:
        from backend.kiosk_models import SnapshotMeta
        meta = s.get(SnapshotMeta, 1)
        body = json.loads(meta.body_json)
        body["ttl_s"] = 10 ** 9
        meta.body_json = json.dumps(body)
    assert stored_signature_ok(store, HMAC_KEY) is False


def test_kill_during_apply_keeps_old_snapshot(store, tmp_path):
    """Acceptance check 10 in a test: a separate process dies mid-transaction."""
    snap_file = tmp_path / "snap.json"
    snap_file.write_text(json.dumps(make_snapshot(2, ("u_crash",))))
    code = ("import json,sys; from backend.store import Store; from backend.snapshot import apply;"
            "from central_helpers import HMAC_KEY, T0;"
            f"apply(Store(sys.argv[1], migrate=False), json.load(open(sys.argv[2])), HMAC_KEY, '{KIOSK}', T0)")
    env = {**os.environ, "SPOOF_FAULT_APPLY": "crash",
           "PYTHONPATH": os.pathsep.join([os.getcwd(), os.path.join(os.getcwd(), "tests")])}
    proc = subprocess.run([sys.executable, "-c", code, str(tmp_path), str(snap_file)],
                          env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 3, proc.stderr[-2000:]
    after = Store(tmp_path, migrate=False)
    assert names(after) == ["u_001", "u_002"] and after.snapshot_meta().version == 1
