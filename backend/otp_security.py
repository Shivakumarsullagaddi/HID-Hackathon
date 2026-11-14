"""OTP Challenge & Security Hardening Module.

Standalone, modular security hardening layer providing:
1. In-memory ChallengeManager with 60s TTL and single-use burn (max 1 verification attempt).
2. Hard biometric floor validation (rejects spoofs with liveness < 0.50 or similarity < 80.0).
3. HMAC-SHA256 signed ALLOW verdicts (X-Verdict-Signature).
4. Immutable hash-chained audit log with cryptographic verification.
5. Lightweight anti-replay frame SHA-256 cache (10s TTL).
6. 3-strike rate limiter with temporary lockout per user.
"""
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict
from .models import VerifyResponse

log = logging.getLogger("spoof.otp_security")


# ---- Custom Exceptions -------------------------------------------------

class SecurityHardeningError(Exception):
    """Base error for security hardening violations."""
    pass


class BiometricFloorError(SecurityHardeningError, ValueError):
    """Raised when biometric scores fail to meet minimum security thresholds."""
    pass


class RateLimitExceededError(SecurityHardeningError, PermissionError):
    """Raised when a user is locked out after exceeding failed attempts."""
    pass


class ChallengeNotFoundError(SecurityHardeningError, KeyError):
    """Raised when challenge_id does not exist or has already expired."""
    pass


class ChallengeBurnedError(SecurityHardeningError, ValueError):
    """Raised when challenge has already been consumed (single-use policy)."""
    pass


# ---- Pydantic Schemas ---------------------------------------------------

class VerifyOtpRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    challenge_id: Optional[str] = None
    challengeId: Optional[str] = None
    otp: Optional[str] = None
    otp_code: Optional[str] = None

    @property
    def effective_challenge_id(self) -> str:
        return self.challenge_id or self.challengeId or ""

    @property
    def effective_otp(self) -> str:
        return self.otp or self.otp_code or ""


class HardenedVerifyResponse(VerifyResponse):
    challenge_id: Optional[str] = None
    demo_otp: Optional[str] = None


# ---- Anti-Replay Frame SHA-256 Cache -----------------------------------

class AntiReplayCache:
    """In-memory SHA-256 frame cache rejecting duplicate frames within TTL (default 10s)."""

    def __init__(self, ttl_seconds: float = 10.0):
        self.ttl = ttl_seconds
        self._cache: Dict[str, float] = {}  # frame_hash -> timestamp
        self._lock = threading.Lock()

    def _clean(self, now: float) -> None:
        self._cache = {h: ts for h, ts in self._cache.items() if (now - ts) < self.ttl}

    @staticmethod
    def hash_frame(frame: Any) -> str:
        if isinstance(frame, str):
            # Base64 string or data URL
            data = frame.split(",", 1)[-1].strip().encode("utf-8")
        elif isinstance(frame, bytes):
            data = frame
        elif hasattr(frame, "tobytes"):
            data = frame.tobytes()
        else:
            data = str(frame).encode("utf-8")
        return hashlib.sha256(data).hexdigest()

    def is_replay(self, frames: List[Any]) -> bool:
        """Returns True if any frame was seen within the last ttl_seconds.
        Otherwise records all frames and returns False.
        """
        if not frames:
            return False
        now = time.time()
        with self._lock:
            self._clean(now)
            hashes = [self.hash_frame(f) for f in frames]
            for h in hashes:
                if h in self._cache and (now - self._cache[h]) < self.ttl:
                    log.warning("[ANTI-REPLAY] Duplicate frame detected within %.1fs: %s",
                                self.ttl, h[:12])
                    return True
            for h in hashes:
                self._cache[h] = now
            return False

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


# ---- 3-Strike Rate Limiter ----------------------------------------------

class RateLimiter:
    """Tracks failed verification strikes per user; enforces 3-strike lockout."""

    def __init__(self, max_strikes: int = 3, lockout_seconds: float = 300.0):
        self.max_strikes = max_strikes
        self.lockout_seconds = lockout_seconds
        self._strikes: Dict[str, List[float]] = {}  # user_id -> list of strike timestamps
        self._lock = threading.Lock()

    def _clean_user(self, user_id: str, now: float) -> List[float]:
        recent = [ts for ts in self._strikes.get(user_id, []) if (now - ts) < self.lockout_seconds]
        self._strikes[user_id] = recent
        return recent

    def record_strike(self, user_id: str) -> int:
        if not user_id:
            return 0
        now = time.time()
        with self._lock:
            recent = self._clean_user(user_id, now)
            recent.append(now)
            self._strikes[user_id] = recent
            count = len(recent)
            log.warning("[RATE LIMITER] Strike recorded for user '%s': %d/%d strikes",
                        user_id, count, self.max_strikes)
            return count

    def is_locked(self, user_id: str) -> bool:
        if not user_id:
            return False
        now = time.time()
        with self._lock:
            recent = self._clean_user(user_id, now)
            return len(recent) >= self.max_strikes

    def get_strikes(self, user_id: str) -> int:
        if not user_id:
            return 0
        now = time.time()
        with self._lock:
            recent = self._clean_user(user_id, now)
            return len(recent)

    def reset(self, user_id: str) -> None:
        if not user_id:
            return
        with self._lock:
            self._strikes.pop(user_id, None)

    def clear(self) -> None:
        with self._lock:
            self._strikes.clear()


# ---- Hash-Chained Audit Log ---------------------------------------------

@dataclass
class AuditEntry:
    index: int
    timestamp: str
    challenge_id: str
    user_id: str
    verdict: str  # "ALLOW" or "DENY"
    reason: str
    signature: Optional[str]
    prev_hash: str
    entry_hash: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "timestamp": self.timestamp,
            "challenge_id": self.challenge_id,
            "user_id": self.user_id,
            "verdict": self.verdict,
            "reason": self.reason,
            "signature": self.signature,
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
        }


# ---- In-Memory Challenge Record ----------------------------------------

@dataclass
class Challenge:
    challenge_id: str
    user_id: str
    otp_code: str
    liveness_score: float
    similarity_score: float
    created_at: float
    expires_at: float
    attempts: int = 0
    burned: bool = False


# ---- Main ChallengeManager ---------------------------------------------

class ChallengeManager:
    """Manages ephemeral step-up OTP challenges, cryptographic signatures, and audit trails."""

    LIVENESS_FLOOR = 0.50
    SIMILARITY_FLOOR = 80.0
    DEFAULT_TTL_SECONDS = 60.0

    def __init__(self, hmac_secret: Optional[bytes] = None, ttl_seconds: float = 60.0):
        self.ttl = ttl_seconds
        secret = hmac_secret or os.environ.get("VERDICT_HMAC_SECRET", "spot-a-spoof-verdict-secret-2026").encode("utf-8")
        self.hmac_secret = secret if isinstance(secret, bytes) else str(secret).encode("utf-8")
        self._challenges: Dict[str, Challenge] = {}
        self._audit_log: List[AuditEntry] = []
        self._lock = threading.Lock()

        # Embedded security guards
        self.anti_replay = AntiReplayCache(ttl_seconds=10.0)
        self.rate_limiter = RateLimiter(max_strikes=3, lockout_seconds=300.0)

    def _cleanup_expired(self, now: float) -> None:
        self._challenges = {
            cid: c for cid, c in self._challenges.items()
            if (now - c.created_at) < (self.ttl * 2) and not (c.burned and (now - c.created_at > self.ttl))
        }

    def create_challenge(self, user_id: str, liveness_score: float, similarity_score: float) -> Dict[str, Any]:
        """Creates a single-use 6-digit OTP challenge.

        Strictly rejects if liveness < 0.50 or similarity < 80.0 (hard biometric floor).
        Also checks user rate limit.
        """
        # Rate limit check
        if user_id and self.rate_limiter.is_locked(user_id):
            strikes = self.rate_limiter.get_strikes(user_id)
            raise RateLimitExceededError(
                f"User '{user_id}' locked out: {strikes} failed strikes recorded. Try again later."
            )

        # Biometric floor validation
        if liveness_score < self.LIVENESS_FLOOR:
            raise BiometricFloorError(
                f"Liveness score {liveness_score:.2f} is below hard biometric floor ({self.LIVENESS_FLOOR:.2f}). Spoofs cannot receive OTP."
            )
        if similarity_score < self.SIMILARITY_FLOOR:
            raise BiometricFloorError(
                f"Similarity score {similarity_score:.1f} is below identity threshold ({self.SIMILARITY_FLOOR:.1f}). Identity must match before step-up."
            )

        now = time.time()
        challenge_id = uuid.uuid4().hex
        otp_code = f"{secrets.randbelow(1_000_000):06d}"

        challenge = Challenge(
            challenge_id=challenge_id,
            user_id=user_id,
            otp_code=otp_code,
            liveness_score=liveness_score,
            similarity_score=similarity_score,
            created_at=now,
            expires_at=now + self.ttl,
            attempts=0,
            burned=False,
        )

        with self._lock:
            self._cleanup_expired(now)
            self._challenges[challenge_id] = challenge

        log.info("[OTP SECURITY] Challenge created: id=%s user=%s OTP=%s (TTL %ds)",
                 challenge_id, user_id, otp_code, int(self.ttl))

        # Optional cloud delivery via AWS SNS/SES if configured
        self._send_cloud_notification(user_id, otp_code)

        return {
            "challenge_id": challenge_id,
            "otp_code": otp_code,
            "user_id": user_id,
            "expires_in": int(self.ttl),
            "created_at": now,
        }

    def verify_otp(self, challenge_id: str, otp_code: str) -> Dict[str, Any]:
        """Verifies OTP code with single-use burn, expiry, HMAC signing, and audit chaining."""
        now = time.time()
        with self._lock:
            challenge = self._challenges.get(challenge_id)

        if not challenge:
            log.warning("[OTP SECURITY] Unknown or expired challenge_id: %s", challenge_id)
            return {
                "decision": "DENY",
                "reason": "Challenge not found or expired",
                "verified": False,
                "challenge_id": challenge_id,
            }

        # Check if already burned or maximum attempts reached
        if challenge.burned or challenge.attempts >= 1:
            strikes = self.rate_limiter.record_strike(challenge.user_id)
            entry = self._log_audit(
                challenge_id=challenge_id,
                user_id=challenge.user_id,
                verdict="DENY",
                reason="Challenge already burned (single-use violation)",
                signature=None
            )
            return {
                "decision": "DENY",
                "reason": f"Challenge already consumed. Max 1 verification attempt allowed. Strike recorded ({strikes}/3).",
                "verified": False,
                "challenge_id": challenge_id,
                "user_id": challenge.user_id,
                "audit_hash": entry.entry_hash,
            }

        # Check TTL expiry
        if now > challenge.expires_at:
            challenge.burned = True
            challenge.attempts += 1
            strikes = self.rate_limiter.record_strike(challenge.user_id)
            entry = self._log_audit(
                challenge_id=challenge_id,
                user_id=challenge.user_id,
                verdict="DENY",
                reason="Challenge expired (60s TTL)",
                signature=None
            )
            return {
                "decision": "DENY",
                "reason": f"Challenge expired ({int(self.ttl)}s TTL elapsed). Strike recorded ({strikes}/3).",
                "verified": False,
                "challenge_id": challenge_id,
                "user_id": challenge.user_id,
                "audit_hash": entry.entry_hash,
            }

        # Burn immediately (enforces max 1 attempt policy)
        challenge.attempts += 1
        challenge.burned = True

        # Constant-time comparison
        is_match = hmac.compare_digest(challenge.otp_code.strip(), str(otp_code or "").strip())

        if is_match:
            # Clear rate limit strikes on success
            self.rate_limiter.reset(challenge.user_id)

            # Generate HMAC-SHA256 verdict signature
            payload = f"{challenge_id}:{challenge.user_id}:ALLOW:{int(now)}"
            signature = hmac.new(self.hmac_secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()

            entry = self._log_audit(
                challenge_id=challenge_id,
                user_id=challenge.user_id,
                verdict="ALLOW",
                reason="OTP verified successfully",
                signature=signature
            )

            log.info("[OTP SECURITY] Challenge %s verified: ALLOW for user %s (sig=%s...)",
                     challenge_id, challenge.user_id, signature[:12])

            return {
                "decision": "ALLOW",
                "reason": "OTP verified successfully",
                "verified": True,
                "challenge_id": challenge_id,
                "user_id": challenge.user_id,
                "signature": signature,
                "audit_hash": entry.entry_hash,
                "timestamp": entry.timestamp,
            }
        else:
            strikes = self.rate_limiter.record_strike(challenge.user_id)
            strikes_left = max(0, 3 - strikes)
            entry = self._log_audit(
                challenge_id=challenge_id,
                user_id=challenge.user_id,
                verdict="DENY",
                reason="Incorrect OTP code",
                signature=None
            )

            log.warning("[OTP SECURITY] Challenge %s failed: incorrect code for user %s (strike %d/3)",
                        challenge_id, challenge.user_id, strikes)

            return {
                "decision": "DENY",
                "reason": f"Incorrect OTP code. Challenge burned. ({strikes_left} strikes remaining)",
                "verified": False,
                "challenge_id": challenge_id,
                "user_id": challenge.user_id,
                "audit_hash": entry.entry_hash,
            }

    # ---- Audit Chain ---------------------------------------------------

    def _log_audit(self, challenge_id: str, user_id: str, verdict: str, reason: str,
                   signature: Optional[str] = None) -> AuditEntry:
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock:
            index = len(self._audit_log)
            prev_hash = self._audit_log[-1].entry_hash if self._audit_log else "0" * 64
            content = f"{index}|{timestamp}|{challenge_id}|{user_id}|{verdict}|{reason}|{signature or ''}|{prev_hash}"
            entry_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

            entry = AuditEntry(
                index=index,
                timestamp=timestamp,
                challenge_id=challenge_id,
                user_id=user_id,
                verdict=verdict,
                reason=reason,
                signature=signature,
                prev_hash=prev_hash,
                entry_hash=entry_hash,
            )
            self._audit_log.append(entry)
            return entry

    def verify_audit_chain(self) -> bool:
        """Verifies the integrity of every hash-chained audit log entry."""
        with self._lock:
            for i, entry in enumerate(self._audit_log):
                expected_prev = self._audit_log[i - 1].entry_hash if i > 0 else "0" * 64
                if entry.prev_hash != expected_prev:
                    return False
                content = f"{entry.index}|{entry.timestamp}|{entry.challenge_id}|{entry.user_id}|{entry.verdict}|{entry.reason}|{entry.signature or ''}|{entry.prev_hash}"
                if hashlib.sha256(content.encode("utf-8")).hexdigest() != entry.entry_hash:
                    return False
        return True

    def get_audit_log(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [e.to_dict() for e in self._audit_log]

    # ---- Convenience Proxy Methods -------------------------------------

    def is_replay(self, frames: List[Any]) -> bool:
        return self.anti_replay.is_replay(frames)

    def is_rate_limited(self, user_id: str) -> bool:
        return self.rate_limiter.is_locked(user_id)

    def reset_rate_limit(self, user_id: str) -> None:
        self.rate_limiter.reset(user_id)

    def clear(self) -> None:
        with self._lock:
            self._challenges.clear()
            self._audit_log.clear()
        self.anti_replay.clear()
        self.rate_limiter.clear()

    # ---- Cloud Notification Helper -------------------------------------

    def _send_cloud_notification(self, user_id: str, otp_code: str) -> None:
        sns_arn = os.environ.get("AWS_SNS_TOPIC_ARN")
        sns_phone = os.environ.get("AWS_SNS_PHONE_NUMBER")
        ses_email = os.environ.get("AWS_SES_EMAIL")

        if not (sns_arn or sns_phone or ses_email):
            return

        try:
            import boto3
            region = os.environ.get("AWS_REGION", "us-east-1")
            msg = f"[Spot-a-Spoof] Security Verification OTP: {otp_code}. Valid for 60 seconds."

            if sns_phone:
                sns = boto3.client("sns", region_name=region)
                sns.publish(PhoneNumber=sns_phone, Message=msg)
                log.info("[OTP SECURITY] OTP delivered to phone via AWS SNS")
            elif sns_arn:
                sns = boto3.client("sns", region_name=region)
                sns.publish(TopicArn=sns_arn, Message=msg, Subject="Spot-a-Spoof Verification OTP")
                log.info("[OTP SECURITY] OTP published to AWS SNS Topic")
            elif ses_email:
                ses = boto3.client("ses", region_name=region)
                ses.send_email(
                    Source=ses_email,
                    Destination={"ToAddresses": [ses_email]},
                    Message={
                        "Subject": {"Data": "Spot-a-Spoof Verification OTP"},
                        "Body": {"Text": {"Data": msg}},
                    },
                )
                log.info("[OTP SECURITY] OTP emailed via AWS SES")
        except Exception as e:
            log.warning("[OTP SECURITY] Cloud notification skipped or failed: %s", e)


# Global singleton instance
challenge_manager = ChallengeManager()
