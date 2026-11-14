"""Integration tests on public sample photos. They need the model files and
`python tools/fetch_models.py --samples`; otherwise they are skipped.

These are still images with synthetic camera noise, not webcam captures.
They prove the wiring and the policy, not real-world accuracy.
"""
import base64
from pathlib import Path

import cv2
import numpy as np
import pytest

from backend import config

FIX = Path(__file__).parent / "fixtures"
NEEDED = [FIX / n for n in ("image_T1.jpg", "image_F1.jpg", "image_F2.jpg", "group.jpg")]
MODELS = [config.MODELS_DIR / n for n in ("minifasnet_v2.onnx", "minifasnet_v1se.onnx",
                                          "buffalo_sc/det_500m.onnx", "buffalo_sc/w600k_mbf.onnx")]
pytestmark = pytest.mark.skipif(
    not all(p.exists() for p in NEEDED + MODELS),
    reason="models or sample photos missing: run python tools/fetch_models.py --samples")


def burst(img, n=5, noise=2.0, seed=0):
    """Simulate a short webcam burst from one still: tiny shifts + sensor noise."""
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n):
        M = np.float32([[1, 0, rng.uniform(-1.5, 1.5)], [0, 1, rng.uniform(-1.5, 1.5)]])
        j = cv2.warpAffine(img, M, (img.shape[1], img.shape[0]), borderMode=cv2.BORDER_REFLECT)
        out.append(np.clip(j.astype(np.float32) + rng.normal(0, noise, j.shape), 0, 255)
                   .astype(np.uint8))
    return out


def b64(frames):
    return [base64.b64encode(cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 92])[1]).decode()
            for f in frames]


@pytest.fixture(scope="module")
def imgs():
    return {p.stem: cv2.imread(str(p)) for p in NEEDED}


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory, imgs):
    from backend.pipeline import Pipeline
    from backend.store import Store
    p = Pipeline(Store(tmp_path_factory.mktemp("data")))
    assert p.liveness.mode == "ensemble" and p.faces.identity_available
    p.alice = p.enrol("Alice", burst(imgs["image_T1"], seed=1))
    return p


def test_models_report_three_classes_and_separate_real_from_fake(pipeline, imgs):
    def passive(img):
        return pipeline.liveness.passive_frame(img, pipeline.faces.detect(img)[0]["bbox"])
    real, fake1, fake2 = (passive(imgs[k]) for k in ("image_T1", "image_F1", "image_F2"))
    assert real > 0.85 and fake1 < 0.4 and fake2 < 0.4


def test_live_enrolled_face_is_allowed_fast(pipeline, imgs):
    pipeline.verify(burst(imgs["image_T1"]))  # warm-up
    r = pipeline.verify(burst(imgs["image_T1"], seed=2))
    assert r["decision"] == "ALLOW" and r["matched_user"] == pipeline.alice["id"]
    assert r["similarity"] >= 95 and r["liveness"] >= 0.85
    assert r["latency_ms"] < 2000


def test_spoof_of_enrolled_face_is_denied(pipeline, imgs):
    # image_F2 is upstream's fake example and matches the enrolled face.
    r = pipeline.verify(burst(imgs["image_F2"]))
    assert r["decision"] == "DENY" and r["reason"] == "Spoof detected"


def test_other_faces_are_never_allowed(pipeline, imgs):
    for key in ("image_F1", "group"):
        r = pipeline.verify(burst(imgs[key]))
        assert r["decision"] != "ALLOW" and r["matched_user"] is None


def test_dark_blurry_and_faceless_frames_request_verification(pipeline, imgs):
    dark = cv2.convertScaleAbs(imgs["image_T1"], alpha=0.3)
    blurry = cv2.GaussianBlur(imgs["image_T1"], (21, 21), 0)
    blank = np.full((480, 640, 3), 120, np.uint8)
    for frames, why in ((burst(dark), "too_dark"), (burst(blurry, noise=0.5), "blurry"),
                        (burst(blank), "no_face")):
        r = pipeline.verify(frames)
        assert (r["decision"], r["reason"]) == ("REQUEST_VERIFICATION", "Poor image quality")
        assert r["gate_reason"] == why


def test_identical_frames_are_not_allowed(pipeline, imgs):
    r = pipeline.verify([imgs["image_T1"]] * 3)
    assert r["decision"] == "REQUEST_VERIFICATION"


def test_suspended_user_is_denied_even_when_live(pipeline, imgs):
    pipeline.store.set_standing(pipeline.alice["id"], "suspended")
    try:
        r = pipeline.verify(burst(imgs["image_T1"]))
        assert (r["decision"], r["reason"]) == ("DENY", "User suspended")
    finally:
        pipeline.store.set_standing(pipeline.alice["id"], "active")


def test_every_decision_is_logged_with_mode_and_latency(pipeline):
    records = pipeline.store.read_decisions()
    assert records and all(r["liveness_mode"] == "ensemble" and r["latency_ms"] > 0
                           and r["request_id"] for r in records)


# ---- blink step-up -------------------------------------------------------

def blink_clip(pipeline, img, closed=(), n=16, seed=3):
    """Synthetic clip: on `closed` frames the eyes are painted over with skin."""
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


def test_blink_detected_in_clip_with_closed_eye_frames(pipeline, imgs):
    out = pipeline.stepup_blink(blink_clip(pipeline, imgs["image_T1"], closed=(6, 7)))
    assert out["blinked"] is True


def test_no_blink_in_static_clip(pipeline, imgs):
    out = pipeline.stepup_blink(blink_clip(pipeline, imgs["image_T1"]))
    assert out["blinked"] is False


def test_stepup_replaces_first_decision_and_shares_request_id(pipeline, imgs):
    first = pipeline.verify([imgs["image_T1"]] * 3)  # identical frames -> REQUEST_VERIFICATION
    assert first["decision"] == "REQUEST_VERIFICATION"
    out = pipeline.stepup_blink(blink_clip(pipeline, imgs["image_T1"], closed=(8, 9)),
                                first["request_id"])
    assert out["decision"] == "ALLOW" and out["request_id"] == first["request_id"]
    linked = [r for r in pipeline.store.read_decisions()
              if r["request_id"] == first["request_id"]]
    assert [r["stage"] for r in linked] == ["verify", "stepup"]
    # the pending step-up is single use
    again = pipeline.stepup_blink(blink_clip(pipeline, imgs["image_T1"], closed=(8, 9)),
                                  first["request_id"])
    assert again.get("decision") is None


def test_stepup_without_blink_denies(pipeline, imgs):
    first = pipeline.verify([imgs["image_T1"]] * 3)
    out = pipeline.stepup_blink(blink_clip(pipeline, imgs["image_T1"]), first["request_id"])
    assert out["decision"] == "DENY" and "no blink" in out["reason"]


# ---- HTTP API ------------------------------------------------------------

@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from fastapi.testclient import TestClient
    from backend import main
    from backend.pipeline import Pipeline
    from backend.store import Store
    with TestClient(main.app) as c:
        main.state["pipeline"] = Pipeline(Store(tmp_path_factory.mktemp("api")))
        yield c


def test_api_end_to_end(client, imgs):
    assert client.post("/api/verify", json={"frames": b64(burst(imgs["image_T1"], 2))}
                       ).status_code == 422
    assert client.post("/api/verify", json={"frames": ["not-base64-not-base64!"] * 3}
                       ).status_code == 400

    r = client.post("/api/enrol", json={"name": "Alice", "frames": b64(burst(imgs["image_T1"]))})
    assert r.status_code == 200 and r.json() == {"user_id": "u_001", "status": "enrolled"}
    users = client.get("/api/users").json()
    assert users == [{"id": "u_001", "name": "Alice", "standing": "active"}]  # no embeddings

    body = client.post("/api/verify", json={"frames": b64(burst(imgs["image_T1"], seed=5)),
                                            "clip_label": "smoke"}).json()
    for field in ("decision", "reason", "liveness", "similarity", "matched_user",
                  "latency_ms", "gate_passed", "liveness_mode", "request_id"):
        assert field in body
    assert body["decision"] == "ALLOW" and body["liveness_mode"] == "ensemble"

    assert client.patch("/api/users/u_001", json={"standing": "suspended"}).status_code == 200
    assert client.post("/api/verify", json={"frames": b64(burst(imgs["image_T1"]))}
                       ).json()["decision"] == "DENY"
    client.patch("/api/users/u_001", json={"standing": "active"})

    for kind, key, enrolled in (("live", "image_T1", True), ("print", "image_F2", True),
                                ("live", "group", False)):
        clip = client.post("/api/lab/clips", json={
            "type": kind, "person": "alice" if enrolled else "stranger", "enrolled": enrolled,
            "lighting": "sample", "frames": b64(burst(imgs[key]))}).json()
        run = client.post(f"/api/lab/clips/{clip['name']}/run").json()
        assert run["actual"] == run["expected"], clip["name"]

    m = client.post("/api/evaluate").json()
    assert m["overall"]["n"] == 3 and m["overall"]["accuracy"] == 1.0
    assert m["by_type"]["print"]["far"] == 0.0 and m["by_type"]["live"]["frr"] == 0.0
    assert m["by_type"]["live"]["p95_ms"] > 0
    assert set(m["conditions"]["spoof_types_missing"]) == {"screen", "video", "mask"}
    assert client.get("/api/metrics").json()["harness"]["overall"]["n"] == 3
    for page in ("/", "/enrol", "/lab", "/metrics"):
        assert client.get(page).status_code == 200
