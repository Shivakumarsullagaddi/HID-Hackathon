"""Kiosk state: ONLINE, DEGRADED or LOCKED.

The kiosk decides locally in every state; the state only says how far it
trusts its snapshot.

- ONLINE: central answered the last poll and the sync after it succeeded.
- DEGRADED: 3 polls in a row failed (or the outage is simulated). Allowed
  people need blink + PIN.
- LOCKED, whatever the link says: no snapshot, snapshot older than its TTL,
  stored snapshot signature invalid, kiosk database upgrade failed, or the
  clock moved backwards by more than 5 minutes. Every attempt is DENY.

The kiosk starts DEGRADED (or LOCKED) and becomes ONLINE only after its
first successful poll and sync: it never assumes it is online.
"""
import logging
import threading
from collections import deque
from datetime import timedelta

from . import config
from .db import utcnow
from .decision import DEGRADED, LOCKED, ONLINE
from .snapshot import stored_signature_ok

log = logging.getLogger("spoof.state")


class StaticState:
    """Fixed state for tests and for a pipeline without a state manager.
    Defaults to LOCKED: without a state manager the kiosk cannot vouch for
    its data, so it never fails open."""

    def __init__(self, state=LOCKED, snapshot_version=None, reason="no state manager"):
        self.state = state
        self.snapshot_version = snapshot_version
        self.reason = reason

    def current(self, now=None):
        return self.state, self.snapshot_version

    def info(self, now=None):
        return {"state": self.state, "reason": self.reason,
                "snapshot_version": self.snapshot_version}


class StateManager:
    def __init__(self, store, clock=None, hmac_key=None, health=None, sync=None,
                 force_offline=None, ttl_override_s=None, db_error=None):
        self.store = store
        self.clock = clock or utcnow
        self.hmac_key = hmac_key or config.SNAPSHOT_HMAC_KEY
        self.health = health or (lambda: False)
        self.sync = sync
        self.force_offline = config.FORCE_OFFLINE if force_offline is None else force_offline
        self.ttl_override_s = (config.SNAPSHOT_TTL_OVERRIDE_S if ttl_override_s is None
                               else ttl_override_s)
        self.db_error = db_error
        self.simulated_outage = False
        self.failures = 0
        self.link_up = False
        self.clock_rewound = False
        self.signature_invalid = stored_signature_ok(store, self.hmac_key) is False
        self.last_poll_at = None
        self.last_sync_at = None
        self.last_error = None
        self.last_sync = None
        self.warnings = []
        self._high_water = None
        self._last_state = None
        self.history = deque(maxlen=50)
        self._lock = threading.RLock()
        if self.signature_invalid:
            log.error("stored snapshot fails its signature check: kiosk LOCKED")

    # ---- clock ----------------------------------------------------------
    def _observe_clock(self, now):
        if self._high_water is not None and \
                now < self._high_water - timedelta(seconds=config.CLOCK_BACKWARDS_MAX_S):
            if not self.clock_rewound:
                log.error("system clock moved backwards (%s -> %s): kiosk LOCKED",
                          self._high_water.isoformat(), now.isoformat())
            self.clock_rewound = True
        if self._high_water is None or now > self._high_water:
            self._high_water = now

    def effective_ttl(self, meta):
        ttl = meta.ttl_s
        if self.ttl_override_s:
            ttl = min(ttl, self.ttl_override_s)   # an override can only shorten
        return ttl

    # ---- evaluation -----------------------------------------------------
    def evaluate(self, now=None):
        """Returns (state, reason, meta)."""
        now = now or self.clock()
        with self._lock:
            self._observe_clock(now)
            meta = self.store.snapshot_meta()
            if self.db_error:
                result = LOCKED, f"Kiosk database upgrade failed: {self.db_error}"
            elif meta is None:
                result = LOCKED, "No snapshot from central yet"
            elif self.signature_invalid:
                result = LOCKED, "Stored snapshot signature is invalid"
            elif self.clock_rewound:
                result = LOCKED, "System clock moved backwards"
            else:
                age = (now - meta.issued_at).total_seconds()
                if age > self.effective_ttl(meta):
                    result = LOCKED, "Snapshot expired: offline too long"
                elif age < -config.CLOCK_BACKWARDS_MAX_S:
                    result = LOCKED, "Clock is behind the snapshot"
                elif self.link_up:
                    result = ONLINE, "Central reachable, snapshot fresh"
                elif self.simulated_outage or self.force_offline:
                    result = DEGRADED, "Central unreachable (SIMULATED outage)"
                else:
                    result = DEGRADED, "Central unreachable"
            self._record_transition(now, *result)
            return result[0], result[1], meta

    def current(self, now=None):
        state, _, meta = self.evaluate(now)
        return state, (meta.version if meta else None)

    def _record_transition(self, now, state, reason):
        if state != self._last_state:
            self.history.appendleft({"at": now.isoformat(), "from": self._last_state,
                                     "to": state, "reason": reason})
            log.info("kiosk state %s -> %s (%s)", self._last_state, state, reason)
            self._last_state = state

    # ---- polling --------------------------------------------------------
    def set_simulated_outage(self, on):
        with self._lock:
            self.simulated_outage = bool(on)
        return self.info()

    def snapshot_applied(self):
        """Called after a successful apply: the stored signature is valid now."""
        with self._lock:
            self.signature_invalid = False

    def poll_once(self):
        """One poll: health check, then sync (push, pull) on success."""
        now = self.clock()
        with self._lock:
            self._observe_clock(now)
            self.last_poll_at = now
            forced = self.force_offline or self.simulated_outage
        ok, error = False, None
        if forced:
            error = "outage simulated" if self.simulated_outage else "FORCE_OFFLINE=1"
        else:
            try:
                ok = bool(self.health())
                if not ok:
                    error = "health check failed"
            except Exception as e:  # noqa: BLE001
                error = f"{type(e).__name__}: {e}"
        if ok and self.sync is not None:
            try:
                self.last_sync = self.sync()
            except Exception as e:  # noqa: BLE001
                ok, error = False, f"sync failed: {type(e).__name__}: {e}"
        with self._lock:
            if ok:
                self.failures = 0
                self.link_up = True
                self.clock_rewound = False   # central reachable again; clock re-checked from here
                self._high_water = now
                self.last_sync_at = now
                self.last_error = None
            else:
                self.failures += 1
                self.last_error = error
                if self.failures >= config.FAILURES_TO_DEGRADED:
                    self.link_up = False
        return self.evaluate(now)[0]

    # ---- reporting ------------------------------------------------------
    def info(self, now=None):
        now = now or self.clock()
        state, reason, meta = self.evaluate(now)
        out = {
            "state": state,
            "reason": reason,
            "kiosk_id": config.KIOSK_ID,
            "room_id": config.KIOSK_ROOM_ID,
            "rooms": config.ROOM_CHOICES,
            "simulated_outage": self.simulated_outage,
            "force_offline": self.force_offline,
            "consecutive_failures": self.failures,
            "last_error": self.last_error,
            "last_poll_at": self.last_poll_at.isoformat() if self.last_poll_at else None,
            "last_sync_at": self.last_sync_at.isoformat() if self.last_sync_at else None,
            "last_sync": self.last_sync,
            "snapshot_version": None, "snapshot_issued_at": None, "snapshot_age_s": None,
            "ttl_s": None, "time_left_s": None,
            "outbox": self.store.outbox_counts(),
            "warnings": list(self.warnings),
            "history": list(self.history),
        }
        if meta is not None:
            age = (now - meta.issued_at).total_seconds()
            ttl = self.effective_ttl(meta)
            out.update(snapshot_version=meta.version, snapshot_issued_at=meta.issued_at.isoformat(),
                       snapshot_age_s=round(age, 1), ttl_s=ttl,
                       time_left_s=round(max(0.0, ttl - age), 1))
        return out
