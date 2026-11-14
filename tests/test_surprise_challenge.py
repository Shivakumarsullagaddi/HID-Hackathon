"""Automated tests for HID Hackathon Surprise Challenges:
- 01 Context-Aware Access Control (Campus Schedules & Zones)
- 02 Offline Fail-Safe Mode (Privacy-Preserving Offline Queue & Auto-Sync)
"""
import base64
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend import config, decision, main
from backend.decision import decide, decide_stepup, evaluate_schedule
from backend.pipeline import Pipeline
from backend.store import Store

FIX = Path(__file__).parent / "fixtures"
NEEDED = [FIX / n for n in ("image_T1.jpg", "image_F1.jpg", "image_F2.jpg", "group.jpg")]
MODELS = [config.MODELS_DIR / n for n in ("minifasnet_v2.onnx", "minifasnet_v1se.onnx",
                                          "buffalo_sc/det_500m.onnx", "buffalo_sc/w600k_mbf.onnx")]
pytestmark = pytest.mark.skipif(
    not all(p.exists() for p in NEEDED + MODELS),
    reason="models or sample photos missing")


def burst(img, n=5, noise=2.0, seed=0):
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n):
        M = np.float32([[1, 0, rng.uniform(-1.5, 1.5)], [0, 1, rng.uniform(-1.5, 1.5)]])
        j = cv2.warpAffine(img, M, (img.shape[1], img.shape[0]), borderMode=cv2.BORDER_REFLECT)
        out.append(np.clip(j.astype(np.float32) + rng.normal(0, noise, j.shape), 0, 255).astype(np.uint8))
    return out


def b64(frames):
    return [base64.b64encode(cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 92])[1]).decode()
            for f in frames]


def blink_clip(pipeline, img, closed=(), n=16, seed=3):
    face = pipeline.faces.detect(img)[0]
    (lx, ly), (rx, ry) = face["kps"][0], face["kps"][1]
    r = int(0.22 * np.hypot(rx - lx, ry - ly))
    frames = burst(img, n=n, seed=seed)
    for i in closed:
        mask = np.zeros(img.shape[:2], np.uint8)
        for x, y in ((lx, ly), (rx, ry)):
            cv2.ellipse(mask, (int(x), int(y)), (int(1.6 * r), r), 0, 0, 360, 255, -1)
        frames[i] = cv2.inpaint(frames[i], mask, 5, cv2.INPAINT_TELEA)
    return frames


@pytest.fixture(scope="module")
def imgs():
    return {p.stem: cv2.imread(str(p)) for p in NEEDED}


# ---- Task 01: Context-Aware Schedule Unit Tests ------------------------------

def test_context_aware_schedule_matrix():
    # Student rules
    assert evaluate_schedule("student", "main_gate", 10)[0] == "ALLOW"
    assert evaluate_schedule("student", "main_gate", 21)[0] == "STEPUP"
    assert evaluate_schedule("student", "main_gate", 2)[0] == "DENY"

    assert evaluate_schedule("student", "research_lab", 10)[0] == "ALLOW"
    assert evaluate_schedule("student", "research_lab", 20)[0] == "STEPUP"
    assert evaluate_schedule("student", "research_lab", 23)[0] == "DENY"

    assert evaluate_schedule("student", "server_room", 12)[0] == "DENY"

    # Faculty rules
    assert evaluate_schedule("faculty", "main_gate", 12)[0] == "ALLOW"
    assert evaluate_schedule("faculty", "main_gate", 23)[0] == "STEPUP"
    assert evaluate_schedule("faculty", "server_room", 14)[0] == "STEPUP"
    assert evaluate_schedule("faculty", "server_room", 22)[0] == "DENY"

    # Contractor rules
    assert evaluate_schedule("contractor", "main_gate", 12)[0] == "ALLOW"
    assert evaluate_schedule("contractor", "main_gate", 18)[0] == "STEPUP"
    assert evaluate_schedule("contractor", "main_gate", 21)[0] == "DENY"
    assert evaluate_schedule("contractor", "research_lab", 12)[0] == "STEPUP"

    # Security admin rules
    assert evaluate_schedule("security_admin", "main_gate", 3)[0] == "ALLOW"
    assert evaluate_schedule("security_admin", "research_lab", 3)[0] == "ALLOW"
    assert evaluate_schedule("security_admin", "server_room", 14)[0] == "ALLOW"
    assert evaluate_schedule("security_admin", "server_room", 22)[0] == "STEPUP"

    # Legacy backward compatibility: no hour and no zone defaults to ALLOW
    assert evaluate_schedule(None, None, None)[0] == "ALLOW"


def test_decide_policy_with_schedules():
    # Student in allowed window -> ALLOW
    dec, _ = decide(0.95, 98.0, True, "active", True, schedule_status="ALLOW")
    assert dec == "ALLOW"

    # Student in step-up window -> REQUEST_VERIFICATION
    dec, reason = decide(0.95, 98.0, True, "active", True, schedule_status="STEPUP")
    assert dec == "REQUEST_VERIFICATION"

    # Student in curfew/denied window -> DENY
    dec, reason = decide(0.95, 98.0, True, "active", True, schedule_status="DENY",
                         schedule_reason="Outside permitted campus schedule")
    assert dec == "DENY"
    assert "Outside permitted campus schedule" in reason

    # Suspended user always denied regardless of schedule
    assert decide(0.95, 98.0, True, "suspended", True, schedule_status="ALLOW")[0] == "DENY"


# ---- Task 02: Offline Fail-Safe Policy & Privacy Scrubbing Tests -------------

def test_offline_mode_tighter_liveness_threshold():
    # In online mode, liveness 0.87 (>= 0.85) is ALLOWed
    dec, _ = decide(0.87, 98.0, True, "active", True, offline_mode=False)
    assert dec == "ALLOW"

    # In offline fail-safe mode, liveness 0.87 (< 0.90) forces Step-Up
    dec, reason = decide(0.87, 98.0, True, "active", True, offline_mode=True)
    assert dec == "REQUEST_VERIFICATION"
    assert "Offline fail-safe: step-up required" in reason

    # Liveness >= 0.90 still passes in offline fail-safe
    dec, _ = decide(0.92, 98.0, True, "active", True, offline_mode=True)
    assert dec == "ALLOW"

    # Step-up liveness floor is tighter (0.65 vs 0.60) in offline mode
    assert decide_stepup(True, 0.62, 98.0, "active", True, True, offline_mode=False)[0] == "ALLOW"
    assert decide_stepup(True, 0.62, 98.0, "active", True, True, offline_mode=True)[0] == "DENY"
    assert decide_stepup(True, 0.68, 98.0, "active", True, True, offline_mode=True)[0] == "ALLOW"


def test_zero_raw_biometrics_in_offline_scrub():
    raw_record = {
        "ts": "2026-11-14T10:00:00.000+00:00",
        "request_id": "req-12345",
        "stage": "verify",
        "source": "kiosk",
        "decision": "ALLOW",
        "reason": "Live person and identity verified",
        "matched_user": "u_001",
        "standing": "active",
        "role": "student",
        "zone": "main_gate",
        "access_hour": 10,
        "gate_passed": True,
        "latency_ms": 150.0,
        # Raw biometric keys that MUST be scrubbed:
        "embedding": [0.123] * 512,
        "signals": {"passive": 0.99, "moire_ratio": 3.2},
        "frames": 5,
        "liveness": 0.99,
        "similarity": 98.5,
    }

    scrubbed = Store.scrub_biometrics(raw_record)
    assert scrubbed["biometrics_scrubbed"] is True
    assert "integrity_hash" in scrubbed and len(scrubbed["integrity_hash"]) == 64
    assert scrubbed["offline_mode"] is True

    # Absolute invariant: no raw biometric keys
    for forbidden in ("embedding", "signals", "frames", "liveness", "similarity"):
        assert forbidden not in scrubbed, f"Forbidden key {forbidden} found in scrubbed record!"


def test_offline_queue_caching_and_auto_sync(tmp_path):
    store = Store(tmp_path)
    assert len(store.read_offline_queue()) == 0

    record1 = {"ts": "2026-11-14T10:00:00Z", "request_id": "r1", "decision": "ALLOW", "liveness": 0.95}
    record2 = {"ts": "2026-11-14T10:01:00Z", "request_id": "r2", "decision": "DENY", "liveness": 0.20}

    store.append_offline_decision(record1)
    store.append_offline_decision(record2)

    q = store.read_offline_queue()
    assert len(q) == 2
    assert q[0]["request_id"] == "r1"
    assert q[1]["request_id"] == "r2"
    assert store.read_decisions() == []  # Not in central log yet

    res = store.sync_offline_to_central()
    assert res["synced_count"] == 2
    assert len(store.read_offline_queue()) == 0

    central = store.read_decisions()
    assert len(central) == 2
    assert central[0]["synced_from_offline"] is True


# ---- End-to-End API Integration Tests (Both Challenges) ----------------------

@pytest.fixture
def client(tmp_path_factory):
    with TestClient(main.app) as c:
        main.state["pipeline"] = Pipeline(Store(tmp_path_factory.mktemp("api")))
        yield c


def test_end_to_end_context_aware_and_offline(client, imgs):
    # 1. Enrol Alice as a student
    r = client.post("/api/enrol", json={
        "name": "Alice",
        "frames": b64(burst(imgs["image_T1"])),
        "role": "student",
    })
    assert r.status_code == 200
    user_id = r.json()["user_id"]

    # 2. Student at Main Gate at 10:00 AM -> ALLOW
    r_allow = client.post("/api/verify", json={
        "frames": b64(burst(imgs["image_T1"], seed=1)),
        "zone": "main_gate",
        "access_hour": 10,
    }).json()
    assert r_allow["decision"] == "ALLOW"
    assert r_allow["role"] == "student"
    assert r_allow["schedule_status"] == "ALLOW"

    # 3. Student at Research Lab at 20:00 (8:00 PM) -> STEPUP required
    r_stepup = client.post("/api/verify", json={
        "frames": b64(burst(imgs["image_T1"], seed=2)),
        "zone": "research_lab",
        "access_hour": 20,
    }).json()
    assert r_stepup["decision"] == "REQUEST_VERIFICATION"
    assert r_stepup["schedule_status"] == "STEPUP"

    # Complete the step-up blink challenge -> ALLOW
    blink_frames = b64(blink_clip(main.state["pipeline"], imgs["image_T1"], closed=(8, 9)))
    r_stepup_done = client.post("/api/stepup/blink", json={
        "frames": blink_frames,
        "request_id": r_stepup["request_id"],
        "zone": "research_lab",
        "access_hour": 20,
    }).json()
    assert r_stepup_done["decision"] == "ALLOW"
    assert r_stepup_done["blinked"] is True

    # 4. Student at Main Gate at 02:00 AM (Curfew) -> DENY
    r_curfew = client.post("/api/verify", json={
        "frames": b64(burst(imgs["image_T1"], seed=3)),
        "zone": "main_gate",
        "access_hour": 2,
    }).json()
    assert r_curfew["decision"] == "DENY"
    assert "Outside permitted campus schedule" in r_curfew["reason"]

    # 5. Student at Server Room at 10:00 AM -> DENY (unauthorized zone)
    r_server = client.post("/api/verify", json={
        "frames": b64(burst(imgs["image_T1"], seed=4)),
        "zone": "server_room",
        "access_hour": 10,
    }).json()
    assert r_server["decision"] == "DENY"

    # 6. Promote Alice to Faculty
    p = client.patch(f"/api/users/{user_id}", json={"role": "faculty"})
    assert p.status_code == 200
    assert p.json()["role"] == "faculty"

    # Faculty at Server Room at 14:00 -> STEPUP permitted
    r_fac = client.post("/api/verify", json={
        "frames": b64(burst(imgs["image_T1"], seed=5)),
        "zone": "server_room",
        "access_hour": 14,
    }).json()
    assert r_fac["decision"] == "REQUEST_VERIFICATION"
    assert r_fac["schedule_status"] == "STEPUP"

    # 7. Test Offline Fail-Safe Mode via API
    c_off = client.post("/api/connectivity", json={"online": False}).json()
    assert c_off["online"] is False
    assert c_off["mode"] == "offline_failsafe"

    # Verification while offline -> writes scrubbed record to offline queue
    r_offline = client.post("/api/verify", json={
        "frames": b64(burst(imgs["image_T1"], seed=6)),
        "zone": "main_gate",
        "access_hour": 10,
    }).json()
    assert r_offline["offline_mode"] is True
    assert r_offline["offline_queue_size"] >= 1

    # Inspect offline queue endpoint
    q_state = client.get("/api/offline-queue").json()
    assert q_state["online"] is False
    assert q_state["queue_size"] >= 1
    assert q_state["queue"][0]["biometrics_scrubbed"] is True
    assert "signals" not in q_state["queue"][0]

    # Switch back online -> auto-syncs offline queue
    c_on = client.post("/api/connectivity", json={"online": True}).json()
    assert c_on["online"] is True
    assert c_on["last_sync"]["synced_count"] >= 1
    assert c_on["offline_queue_size"] == 0
