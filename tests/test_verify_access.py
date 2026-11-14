"""The access policy wired into the real pipeline (models + sample photos):
schedule, kiosk state, exam window, offline PIN step-up, client input."""
from datetime import datetime, timedelta, timezone

import pytest

from backend.decision import DEGRADED, LOCKED, ONLINE
from backend.kiosk_models import ExamWindow, ScheduleRule
from backend.pin import hash_pin
from backend.schedule import ALL_DAYS
from backend.state import StaticState
from test_pipeline import NEEDED, MODELS, blink_clip, burst, b64  # noqa: F401  (fixtures below)

pytestmark = pytest.mark.skipif(not all(p.exists() for p in NEEDED + MODELS),
                                reason="models or sample photos missing")

# Monday 2026-11-16 10:00 in India (04:30 UTC).
MON_10_IST = datetime(2026, 11, 16, 4, 30, tzinfo=timezone.utc)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture(scope="module")
def imgs():
    import cv2
    return {p.stem: cv2.imread(str(p)) for p in NEEDED}


@pytest.fixture(scope="module")
def setup(tmp_path_factory, imgs):
    from backend.pipeline import Pipeline
    from backend.store import Store
    store = Store(tmp_path_factory.mktemp("access"))
    clock = Clock(MON_10_IST)
    p = Pipeline(store, state=StaticState(ONLINE, 7), clock=clock)
    salt, digest = hash_pin("4321")
    emb = p.enrol("Alice", burst(imgs["image_T1"], seed=1))["embedding"]
    p.store.delete_user("u_001")
    alice = store.add_user("Alice", emb, pin_salt=salt, pin_hash=digest)
    with store.transaction() as s:   # 09:00-17:00 every day in lab_a only
        s.add(ScheduleRule(user_id=alice["id"], room_id="lab_a", days_mask=ALL_DAYS,
                           start_min=9 * 60, end_min=17 * 60))
    p.verify(burst(imgs["image_T1"]))  # warm-up
    return p, clock, alice


def test_online_in_window_allows_under_2s(setup, imgs):
    p, clock, alice = setup
    r = p.verify(burst(imgs["image_T1"], seed=4), room_id="lab_a")
    assert (r["bio_decision"], r["final_decision"]) == ("ALLOW", "ALLOW")
    assert r["schedule_status"] == "in_window" and r["kiosk_state"] == ONLINE
    assert r["snapshot_version"] == 7 and r["record_id"]
    assert r["latency_ms"] < 2000


def test_room_without_rule_denies_live_recognised_person(setup, imgs):
    p, _, _ = setup
    r = p.verify(burst(imgs["image_T1"]), room_id="hostel_gate")
    assert r["bio_decision"] == "ALLOW"
    assert (r["final_decision"], r["final_reason"]) == ("DENY", "No access rule for this room")


def test_outside_window_denies_and_exam_window_steps_up(setup, imgs):
    p, clock, _ = setup
    clock.t = MON_10_IST + timedelta(hours=9)       # 19:00 local
    try:
        r = p.verify(burst(imgs["image_T1"]))
        assert (r["final_decision"], r["final_reason"]) == ("DENY", "Outside permitted schedule")
        with p.store.transaction() as s:
            s.add(ExamWindow(room_id="lab_a", starts_at=clock.t - timedelta(hours=1),
                             ends_at=clock.t + timedelta(hours=1)))
        r = p.verify(burst(imgs["image_T1"]))
        assert (r["final_decision"], r["final_reason"]) == ("STEP_UP_VERIFICATION",
                                                            "Exam window, verify")
        out = p.stepup_blink(blink_clip(p, imgs["image_T1"], closed=(8, 9)), r["request_id"])
        assert out["final_decision"] == "ALLOW"   # exam window + blink, online: no PIN
    finally:
        clock.t = MON_10_IST


def test_locked_denies_everything(setup, imgs):
    p, _, _ = setup
    p.state = StaticState(LOCKED)
    try:
        r = p.verify(burst(imgs["image_T1"]))
        assert (r["final_decision"], r["final_reason"]) == ("DENY", "Kiosk locked, see the guard")
    finally:
        p.state = StaticState(ONLINE, 7)


def test_degraded_requires_blink_and_pin(setup, imgs):
    p, _, alice = setup
    p.state = StaticState(DEGRADED, 7)
    try:
        r = p.verify(burst(imgs["image_T1"]))
        assert (r["final_decision"], r["final_reason"]) == (
            "STEP_UP_VERIFICATION", "Offline: blink and PIN required")
        blink = blink_clip(p, imgs["image_T1"], closed=(8, 9))

        wrong = p.stepup_blink(blink, r["request_id"], pin="0000")
        assert wrong["final_decision"] == "DENY" and wrong["pin_checked"] is False

        r = p.verify(burst(imgs["image_T1"]))
        missing = p.stepup_blink(blink, r["request_id"])
        assert missing["final_decision"] == "DENY"

        r = p.verify(burst(imgs["image_T1"]))
        ok = p.stepup_blink(blink, r["request_id"], pin="4321")
        assert ok["final_decision"] == "ALLOW" and ok["pin_checked"] is True
        assert ok["request_id"] == r["request_id"] and ok["record_id"] != r["record_id"]

        r = p.verify(burst(imgs["image_T1"]))
        no_blink = p.stepup_blink(blink_clip(p, imgs["image_T1"]), r["request_id"], pin="4321")
        assert no_blink["final_decision"] == "DENY"

        # 3 wrong PINs lock PIN entry, even for the right PIN afterwards
        for _ in range(3):
            r = p.verify(burst(imgs["image_T1"]))
            p.stepup_blink(blink, r["request_id"], pin="1111")
        r = p.verify(burst(imgs["image_T1"]))
        locked = p.stepup_blink(blink, r["request_id"], pin="4321")
        assert locked["final_decision"] == "DENY"
        assert locked["final_reason"] == "PIN entry locked, see the guard"
    finally:
        p.state = StaticState(ONLINE, 7)
        p.pin_lockout.record_success(alice["id"])
        p.pin_lockout._locked_until.clear()


def test_pin_never_reaches_the_log(setup):
    p, _, _ = setup
    import json
    text = json.dumps(p.store.read_decisions())
    assert "4321" not in text and "1111" not in text and "0000" not in text


def test_client_cannot_supply_kiosk_state_or_schedule(setup, imgs):
    from fastapi.testclient import TestClient
    from backend import main
    with TestClient(main.app) as c:
        main.state["pipeline"] = setup[0]
        frames = b64(burst(imgs["image_T1"]))
        for extra in ({"kiosk_state": "ONLINE"}, {"schedule_status": "in_window"}):
            assert c.post("/api/verify", json={"frames": frames, **extra}).status_code == 422
        assert c.post("/api/verify", json={"frames": frames, "room_id": "mars"}).status_code == 400
        body = c.post("/api/verify", json={"frames": frames, "room_id": "lab_a"}).json()
        assert body["final_decision"] == "ALLOW" and body["decision"] == "ALLOW"
