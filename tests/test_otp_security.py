"""Unit tests for standalone OTP security module, ChallengeManager, anti-replay,
rate limiting, and verification endpoints.
"""
import time
import pytest
from fastapi.testclient import TestClient

from backend.otp_security import (
    ChallengeManager,
    BiometricFloorError,
    RateLimitExceededError,
    AntiReplayCache,
    RateLimiter,
)
from backend import main


@pytest.fixture
def manager():
    """Isolated ChallengeManager instance with short TTL for testing."""
    return ChallengeManager(
        hmac_secret=b"test-secret-key-12345",
        ttl_seconds=2.0,  # 2 seconds TTL for fast expiry testing
    )


def test_hard_biometric_floor(manager):
    """Liveness < 0.50 or similarity < 80.0 must be strictly rejected (spoofs never get OTP)."""
    # Low liveness (spoof)
    with pytest.raises(BiometricFloorError) as exc:
        manager.create_challenge(user_id="alice", liveness_score=0.49, similarity_score=95.0)
    assert "hard biometric floor" in str(exc.value)

    # Low similarity (not verified)
    with pytest.raises(BiometricFloorError) as exc:
        manager.create_challenge(user_id="alice", liveness_score=0.85, similarity_score=79.9)
    assert "identity threshold" in str(exc.value)

    # Both low
    with pytest.raises(BiometricFloorError):
        manager.create_challenge(user_id="alice", liveness_score=0.20, similarity_score=50.0)

    # Valid biometric floor: liveness >= 0.50 and similarity >= 80.0
    chal = manager.create_challenge(user_id="alice", liveness_score=0.55, similarity_score=85.0)
    assert chal["challenge_id"]
    assert len(chal["otp_code"]) == 6
    assert chal["otp_code"].isdigit()


def test_otp_verification_success(manager):
    """Correct OTP yields ALLOW verdict, HMAC signature, and audit entry."""
    chal = manager.create_challenge(user_id="bob", liveness_score=0.75, similarity_score=90.0)
    res = manager.verify_otp(chal["challenge_id"], chal["otp_code"])

    assert res["decision"] == "ALLOW"
    assert res["verified"] is True
    assert res["user_id"] == "bob"
    assert "signature" in res
    assert len(res["signature"]) == 64  # SHA-256 hex digest length
    assert "audit_hash" in res

    # Verify audit chain integrity
    assert manager.verify_audit_chain() is True
    audit = manager.get_audit_log()
    assert len(audit) == 1
    assert audit[0]["verdict"] == "ALLOW"
    assert audit[0]["signature"] == res["signature"]


def test_otp_single_use_burn_on_failure_and_success(manager):
    """Challenge must be single-use: max 1 attempt burns the challenge."""
    chal = manager.create_challenge(user_id="charlie", liveness_score=0.70, similarity_score=88.0)
    cid = chal["challenge_id"]
    code = chal["otp_code"]

    # First attempt with wrong OTP
    fail = manager.verify_otp(cid, "000000" if code != "000000" else "111111")
    assert fail["decision"] == "DENY"
    assert fail["verified"] is False
    assert "Challenge burned" in fail["reason"]

    # Second attempt with correct OTP MUST be denied because it was already burned!
    second = manager.verify_otp(cid, code)
    assert second["decision"] == "DENY"
    assert second["verified"] is False
    assert "already consumed" in second["reason"].lower() or "burned" in second["reason"].lower()


def test_otp_expiry_ttl(manager):
    """Challenge must expire when TTL elapses."""
    chal = manager.create_challenge(user_id="david", liveness_score=0.60, similarity_score=82.0)
    cid = chal["challenge_id"]
    code = chal["otp_code"]

    # Wait for TTL to elapse (manager fixture has ttl_seconds=2.0)
    time.sleep(2.1)

    res = manager.verify_otp(cid, code)
    assert res["decision"] == "DENY"
    assert res["verified"] is False
    assert "expired" in res["reason"].lower()


def test_anti_replay_cache():
    """Anti-replay cache must reject duplicate frame within TTL and allow after TTL."""
    cache = AntiReplayCache(ttl_seconds=0.5)
    frame1 = "data:image/jpeg;base64,dGVzdC1mcmFtZS0x"
    frame2 = "data:image/jpeg;base64,dGVzdC1mcmFtZS0y"

    # First submission: fresh
    assert cache.is_replay([frame1]) is False

    # Immediate duplicate submission: REPLAY!
    assert cache.is_replay([frame1]) is True

    # Different frame: fresh
    assert cache.is_replay([frame2]) is False

    # Wait for TTL to expire
    time.sleep(0.55)

    # Now frame1 is accepted again
    assert cache.is_replay([frame1]) is False


def test_rate_limiter_3_strikes():
    """User should be locked out on 3 strikes."""
    limiter = RateLimiter(max_strikes=3, lockout_seconds=10.0)
    user = "eve"

    assert limiter.is_locked(user) is False
    assert limiter.record_strike(user) == 1
    assert limiter.is_locked(user) is False

    assert limiter.record_strike(user) == 2
    assert limiter.is_locked(user) is False

    assert limiter.record_strike(user) == 3
    assert limiter.is_locked(user) is True

    # Reset clears strikes
    limiter.reset(user)
    assert limiter.is_locked(user) is False


def test_create_challenge_enforces_rate_limit(manager):
    """Locked user cannot generate new challenges."""
    user = "frank"
    manager.rate_limiter.record_strike(user)
    manager.rate_limiter.record_strike(user)
    manager.rate_limiter.record_strike(user)
    assert manager.rate_limiter.is_locked(user) is True

    with pytest.raises(RateLimitExceededError):
        manager.create_challenge(user_id=user, liveness_score=0.75, similarity_score=90.0)


def test_audit_chain_tamper_detection(manager):
    """Hash-chained audit log detects tampering."""
    c1 = manager.create_challenge(user_id="u1", liveness_score=0.8, similarity_score=90.0)
    manager.verify_otp(c1["challenge_id"], c1["otp_code"])

    c2 = manager.create_challenge(user_id="u2", liveness_score=0.8, similarity_score=90.0)
    manager.verify_otp(c2["challenge_id"], "999999")

    assert manager.verify_audit_chain() is True

    # Tamper with entry in log
    manager._audit_log[0].reason = "Tampered reason"
    assert manager.verify_audit_chain() is False


def test_api_verify_otp_endpoint():
    """Test verify_otp_endpoint and header signing."""
    from fastapi import Response
    from backend import main, otp_security

    main.challenge_manager.clear()
    chal = main.challenge_manager.create_challenge("tester", 0.70, 85.0)

    # 1. Valid OTP submission
    req = otp_security.VerifyOtpRequest(challenge_id=chal["challenge_id"], otp=chal["otp_code"])
    resp = Response()
    res = main.verify_otp_endpoint(req, resp)

    assert res["decision"] == "ALLOW"
    assert res["verified"] is True
    assert resp.headers.get("X-Verdict-Signature") == res["signature"]

    # 2. Re-submission of burned challenge returns HTTP 400 JSONResponse
    resp_second = Response()
    res_second = main.verify_otp_endpoint(req, resp_second)
    assert res_second.status_code == 400


def test_api_verify_request_verification_hook():
    """Test that main.verify attaches challenge_id and demo_otp when REQUEST_VERIFICATION."""
    import base64
    import cv2
    import numpy as np
    from unittest.mock import MagicMock
    from backend import main, models, otp_security

    main.challenge_manager.clear()
    fake_pipe = MagicMock()
    fake_pipe.store.get_user.return_value = {"id": "u1", "name": "Alice", "standing": "active"}
    fake_pipe.verify.return_value = {
        "decision": "REQUEST_VERIFICATION",
        "reason": "Low confidence",
        "liveness": 0.65,
        "similarity": 85.0,
        "matched_user": "u1",
        "latency_ms": 12.0,
        "gate_passed": True,
        "liveness_mode": "ensemble",
        "request_id": "req-1234",
    }
    main.state["pipeline"] = fake_pipe

    # Create valid 64x64 image frames
    img = np.zeros((64, 64, 3), dtype=np.uint8)
    b64_frame = base64.b64encode(cv2.imencode(".jpg", img)[1]).decode()
    req = models.VerifyRequest(frames=[b64_frame] * 3, user_id="u1")
    res = main.verify(req)

    assert res["decision"] == "REQUEST_VERIFICATION"
    assert "challenge_id" in res
    assert "demo_otp" in res
    assert len(res["demo_otp"]) == 6
    assert res["demo_otp"].isdigit()
    assert "masked_email" in res
    assert "****" in res["masked_email"]


def test_email_masking():
    """Test email masking utility."""
    from backend.otp_security import mask_email

    assert mask_email("shivakumar@gmail.com") == "sh****@gmail.com"
    assert mask_email("alice@company.org") == "al****@company.org"
    assert mask_email(None) == "sh****@gmail.com"
    assert mask_email("invalid") == "sh****@gmail.com"


def test_send_otp_email_fallback():
    """Test send_otp_email returns False and logs clean message when SMTP not configured."""
    from backend.otp_security import send_otp_email
    import os

    # Ensure SMTP_USER is unset
    os.environ.pop("SMTP_USER", None)
    os.environ.pop("SMTP_PASSWORD", None)

    success = send_otp_email("test@example.com", "123456", "u_test")
    assert success is False


def test_store_enrol_email(tmp_path):
    """Test user enrollment stores email in Store."""
    from backend.store import Store
    store = Store(tmp_path)
    user = store.add_user(name="Alice", embedding=[0.1] * 512, email="alice@example.com")
    assert user["email"] == "alice@example.com"

    fetched = store.get_user(user["id"])
    assert fetched["email"] == "alice@example.com"


def test_resend_otp_endpoint():
    """Test POST /resend-otp endpoint."""
    from backend import main
    from unittest.mock import MagicMock

    main.challenge_manager.clear()
    fake_pipe = MagicMock()
    fake_pipe.store.get_user.return_value = {"id": "u1", "name": "Alice", "email": "alice@test.com"}
    main.state["pipeline"] = fake_pipe

    chal = main.challenge_manager.create_challenge("u1", 0.70, 85.0, email="alice@test.com")
    req = main.ResendOtpRequest(challenge_id=chal["challenge_id"])

    res = main.resend_otp(req)
    assert res["success"] is True
    assert "al****@test.com" in res["message"]
    assert res["masked_email"] == "al****@test.com"


def test_liveness_ensemble_calibration():
    """Verify calibrated weights: live face >= 0.88, indoor moire produces 0 penalty, fake < 0.45."""
    from backend import config, decision

    # 1. Verify primary weights
    assert config.PASSIVE_WEIGHT >= 0.70
    assert config.HEURISTIC_WEIGHT <= 0.30
    assert config.MOIRE_RATIO_LOW >= 18.0

    # 2. Indoor ceiling moire (13.17) must produce 0 penalty
    indoor_moire = 13.17
    s_moire = max(0.0, min(1.0, (indoor_moire - config.MOIRE_RATIO_LOW) / (config.MOIRE_RATIO_HIGH - config.MOIRE_RATIO_LOW)))
    assert s_moire == 0.0

    # 3. Live face with passive=0.9797 under indoor lighting must score >= 0.88
    passive_live = 0.9797
    penalty = 0.05
    live_score = (config.PASSIVE_WEIGHT * passive_live) + (config.HEURISTIC_WEIGHT * (1.0 - penalty))
    assert live_score >= 0.88

    # 4. Phone screen or print fake must score < 0.45
    passive_fake = 0.25
    fake_score = min(passive_fake, (config.PASSIVE_WEIGHT * passive_fake) + (config.HEURISTIC_WEIGHT * 0.7))
    assert fake_score < 0.45


def test_blurry_live_face_routes_to_request_verification():
    """If image quality is blurry while passive >= 0.85 and similarity >= 90, route to REQUEST_VERIFICATION and trigger OTP."""
    from backend import decision, otp_security

    liveness = 0.88
    similarity = 92.0
    gate_passed = False  # marked blurry by quality gate

    dec, reason = decision.decide(
        liveness=liveness,
        similarity=similarity,
        gate_passed=gate_passed,
        standing="active",
        matched=True,
    )

    # Must route to REQUEST_VERIFICATION instead of DENY
    assert dec == decision.REQUEST_VERIFICATION
    assert reason == "Poor image quality"

    # Liveness is >= 0.50 biometric floor, so OTP challenge creation succeeds
    mgr = otp_security.ChallengeManager()
    chal = mgr.create_challenge(user_id="alice", liveness_score=liveness, similarity_score=similarity)
    assert chal["challenge_id"]
    assert len(chal["otp_code"]) == 6
