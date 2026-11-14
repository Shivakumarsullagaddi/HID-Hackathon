"""State manager: link failures, snapshot freshness, signature, clock, overrides."""
import json
from datetime import timedelta

import pytest

from backend.decision import DEGRADED, LOCKED, ONLINE
from backend.snapshot import apply
from backend.state import StateManager
from backend.store import Store
from central_helpers import HMAC_KEY, T0, Clock
from test_snapshot import KIOSK, make_snapshot


class Health:
    def __init__(self):
        self.up = True
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if not self.up:
            raise ConnectionError("central down")
        return True


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path)
    apply(store, make_snapshot(1), HMAC_KEY, KIOSK, T0)
    clock, health = Clock(T0 + timedelta(seconds=10)), Health()
    syncs = []
    sm = StateManager(store, clock=clock, hmac_key=HMAC_KEY, health=health,
                      sync=lambda: syncs.append(clock()) or {"ok": True},
                      force_offline=False, ttl_override_s=0)
    return sm, store, clock, health, syncs


def test_starts_degraded_until_first_successful_poll(env):
    sm, *_ = env
    assert sm.current()[0] == DEGRADED
    assert sm.poll_once() == ONLINE


def test_three_failures_to_degraded_one_success_back_to_online(env):
    sm, _, clock, health, syncs = env
    sm.poll_once()
    health.up = False
    assert [sm.poll_once() for _ in range(3)] == [ONLINE, ONLINE, DEGRADED]
    health.up = True
    n = len(syncs)
    assert sm.poll_once() == ONLINE
    assert len(syncs) == n + 1                    # the success ran a sync first


def test_sync_failure_counts_as_a_failed_poll(env):
    sm, _, _, _, _ = env
    sm.sync = lambda: (_ for _ in ()).throw(RuntimeError("push 500"))
    for _ in range(3):
        sm.poll_once()
    assert sm.current()[0] == DEGRADED and "push 500" in sm.last_error


def test_snapshot_expiry_locks_even_when_online(env):
    sm, _, clock, _, _ = env
    sm.poll_once()
    clock.t = T0 + timedelta(seconds=600)
    assert sm.current()[0] == ONLINE                   # age == ttl: still valid
    clock.t = T0 + timedelta(seconds=601)
    state, reason, _ = sm.evaluate()
    assert state == LOCKED and "expired" in reason


def test_ttl_override_only_shortens(env):
    sm, _, clock, _, _ = env
    sm.poll_once()
    sm.ttl_override_s = 120
    clock.t = T0 + timedelta(seconds=121)
    assert sm.current()[0] == LOCKED
    sm.ttl_override_s = 10 ** 6
    assert sm.current()[0] == ONLINE   # override cannot extend the 600 s TTL...
    clock.t = T0 + timedelta(seconds=601)
    assert sm.current()[0] == LOCKED   # ...the snapshot's own TTL still applies


def test_missing_snapshot_locks(tmp_path):
    sm = StateManager(Store(tmp_path), clock=Clock(), hmac_key=HMAC_KEY, health=lambda: True,
                      force_offline=False, ttl_override_s=0)
    sm.poll_once()
    state, reason, _ = sm.evaluate()
    assert state == LOCKED and "No snapshot" in reason


def test_bad_stored_signature_locks(env, tmp_path):
    sm, store, clock, _, _ = env
    with store.transaction() as s:
        from backend.kiosk_models import SnapshotMeta
        meta = s.get(SnapshotMeta, 1)
        body = json.loads(meta.body_json)
        body["users"][0]["standing"] = "active"
        body["ttl_s"] = 99999
        meta.body_json = json.dumps(body)
    sm2 = StateManager(store, clock=clock, hmac_key=HMAC_KEY, health=lambda: True,
                       force_offline=False, ttl_override_s=0)
    sm2.poll_once()
    state, reason, _ = sm2.evaluate()
    assert state == LOCKED and "signature" in reason


def test_database_upgrade_failure_locks(env):
    sm, store, clock, health, _ = env
    sm2 = StateManager(store, clock=clock, hmac_key=HMAC_KEY, health=health,
                       force_offline=False, ttl_override_s=0, db_error="OperationalError: boom")
    sm2.poll_once()
    assert sm2.current()[0] == LOCKED


def test_clock_moved_backwards_locks(env):
    sm, _, clock, _, _ = env
    sm.poll_once()
    clock.t = T0 + timedelta(seconds=400)
    sm.current()
    clock.t = T0 + timedelta(seconds=50)            # 350 s back: more than 5 minutes
    state, reason, _ = sm.evaluate()
    assert state == LOCKED and "backwards" in reason
    clock.t = T0 + timedelta(seconds=60)
    assert sm.current()[0] == LOCKED                  # sticky until central is reached again


def test_small_clock_step_back_is_tolerated(env):
    sm, _, clock, _, _ = env
    sm.poll_once()
    clock.t = T0 + timedelta(seconds=200)
    sm.current()
    clock.t = T0 + timedelta(seconds=100)            # 100 s back: NTP-sized correction
    assert sm.current()[0] == ONLINE


def test_force_offline_skips_the_poll(env):
    sm, _, _, health, _ = env
    sm.force_offline = True
    calls = health.calls
    for _ in range(3):
        sm.poll_once()
    assert health.calls == calls and sm.current()[0] == DEGRADED
    assert "SIMULATED" in sm.evaluate()[1]


def test_simulated_outage_toggle(env):
    sm, _, _, health, _ = env
    sm.poll_once()
    sm.set_simulated_outage(True)
    calls = health.calls
    states = [sm.poll_once() for _ in range(3)]
    assert states[-1] == DEGRADED and health.calls == calls
    sm.set_simulated_outage(False)
    assert sm.poll_once() == ONLINE


def test_info_reports_age_time_left_and_history(env):
    sm, _, clock, _, _ = env
    sm.current()                      # DEGRADED before the first poll
    sm.poll_once()
    clock.t = T0 + timedelta(seconds=100)
    info = sm.info()
    assert info["state"] == ONLINE and info["snapshot_version"] == 1
    assert info["snapshot_age_s"] == 100.0 and info["time_left_s"] == 500.0
    assert info["outbox"] == {"total": 0, "unsynced": 0}
    assert [h["to"] for h in info["history"]][:2] == [ONLINE, DEGRADED]
