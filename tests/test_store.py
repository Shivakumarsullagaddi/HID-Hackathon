import pytest
from pydantic import ValidationError

from backend.models import EnrolRequest, VerifyRequest
from backend.store import Store

FRAME = "A" * 32


def test_decision_record_appends_and_reads_back(tmp_path):
    store = Store(tmp_path)
    record = {"request_id": "r1", "decision": "ALLOW", "liveness": 0.93}
    store.append_decision(record)
    store.append_decision({"request_id": "r2", "decision": "DENY"})
    assert store.read_decisions()[0] == record
    assert len(store.read_decisions()) == 2


def test_gallery_add_standing_delete(tmp_path):
    store = Store(tmp_path)
    alice = store.add_user("Alice", [0.1] * 512)
    carol = store.add_user("Carol", [0.2] * 512, standing="suspended")
    assert (alice["id"], carol["id"]) == ("u_001", "u_002")
    assert store.get_user("u_002")["standing"] == "suspended"
    assert store.set_standing("u_001", "suspended")["standing"] == "suspended"
    assert store.delete_user("u_001") and not store.delete_user("u_001")
    assert store.add_user("Dan", [0.3] * 512)["id"] == "u_003"


def test_clip_roundtrip(tmp_path):
    store = Store(tmp_path)
    meta = {"type": "print", "person": "Alice", "enrolled": True,
            "lighting": "Day light", "expected": "DENY", "notes": None}
    clip = store.add_clip(meta, [b"a", b"b", b"c"])
    assert clip["name"] == "print_alice_day-light_01"
    assert store.add_clip(meta, [b"a", b"b", b"c"])["name"] == "print_alice_day-light_02"
    assert len(store.clip_frame_paths(clip["name"])) == 3
    assert store.delete_clip(clip["name"])
    assert [c["name"] for c in store.expected()["clips"]] == ["print_alice_day-light_02"]


@pytest.mark.parametrize("n", [0, 2, 6])
def test_verify_request_rejects_wrong_frame_count(n):
    with pytest.raises(ValidationError):
        VerifyRequest(frames=[FRAME] * n)


def test_verify_request_accepts_3_to_5_and_rejects_unknown_fields():
    assert len(VerifyRequest(frames=[FRAME] * 3).frames) == 3
    assert len(VerifyRequest(frames=[FRAME] * 5, clip_label="x").frames) == 5
    with pytest.raises(ValidationError):
        VerifyRequest(frames=[FRAME] * 3, extra=1)


def test_enrol_request_needs_exactly_five_frames():
    EnrolRequest(name="Alice", frames=[FRAME] * 5)
    with pytest.raises(ValidationError):
        EnrolRequest(name="Alice", frames=[FRAME] * 4)
