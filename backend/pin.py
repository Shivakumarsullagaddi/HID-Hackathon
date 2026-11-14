"""PINs: scrypt with a per-user random salt, constant-time compare, and the
kiosk-local lockout counter. PINs are never logged or stored in clear.

Shared by the kiosk (verifying offline step-ups) and central (enrolment).
"""
import hashlib
import hmac
import re
import secrets
import threading
from collections import defaultdict, deque

from . import config

PIN_RE = re.compile(r"^\d{4,8}$")
_N, _R, _P, _DKLEN = 2 ** 14, 8, 1, 32


def valid_pin(pin):
    return bool(pin) and bool(PIN_RE.match(pin))


def hash_pin(pin, salt_hex=None):
    """Returns (salt_hex, hash_hex)."""
    if not valid_pin(pin):
        raise ValueError("PIN must be 4 to 8 digits")
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.scrypt(pin.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return salt.hex(), digest.hex()


def verify_pin(pin, salt_hex, hash_hex):
    if not (pin and salt_hex and hash_hex) or not valid_pin(pin):
        return False
    _, got = hash_pin(pin, salt_hex)
    return hmac.compare_digest(got, hash_hex)


class PinLockout:
    """3 wrong PINs for a user within 10 minutes lock PIN entry for that user
    for 10 minutes. In memory only: a kiosk restart resets it (documented)."""

    def __init__(self, max_failures=None, window_s=None, lockout_s=None):
        self.max_failures = max_failures or config.PIN_MAX_FAILURES
        self.window_s = window_s or config.PIN_FAILURE_WINDOW_S
        self.lockout_s = lockout_s or config.PIN_LOCKOUT_S
        self._failures = defaultdict(deque)
        self._locked_until = {}
        self._lock = threading.Lock()

    def is_locked(self, user_id, now_s):
        with self._lock:
            until = self._locked_until.get(user_id)
            if until is not None and now_s >= until:
                del self._locked_until[user_id]
                until = None
            return until is not None

    def record_failure(self, user_id, now_s):
        with self._lock:
            q = self._failures[user_id]
            q.append(now_s)
            while q and now_s - q[0] > self.window_s:
                q.popleft()
            if len(q) >= self.max_failures:
                self._locked_until[user_id] = now_s + self.lockout_s
                q.clear()

    def record_success(self, user_id):
        with self._lock:
            self._failures.pop(user_id, None)
