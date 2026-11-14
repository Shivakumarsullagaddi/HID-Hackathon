"""Reconciler: a suspension made during an outage is caught after sync."""
import pytest
from sqlalchemy import select

from backend.kiosk_models import DecisionOutbox
from backend.store import Store
from backend.sync import SyncAgent
from central_helpers import HMAC_KEY, Central, Clock
from test_central import emb
from test_sync import KIOSK, add_rows


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    central = Central(tmp_path / "c", clock=clock)
    store = Store(tmp_path / "k")
    agent = SyncAgent(store, central.client, "http://testserver", KIOSK, central.api_key,
                      HMAC_KEY, clock=clock)
    uid = central.add_user("Alice", emb(1))
    agent.pull()                                   # kiosk holds Alice as active
    yield central, store, agent, clock, uid
    central.close()


def alerts(central, type_="offline_allow_after_suspension"):
    return [a for a in central.client.get("/alerts").json() if a["type"] == type_]


def test_suspended_during_outage_then_offline_allow_raises_one_alert(env):
    central, store, agent, clock, uid = env
    clock.advance(minutes=1)
    central.client.patch(f"/users/{uid}", json={"standing": "suspended"})  # kiosk is offline
    clock.advance(minutes=1)
    [rid] = add_rows(store, clock, 1, state="DEGRADED", final="ALLOW", user_id=uid)
    agent.push()
    got = alerts(central)
    assert len(got) == 1 and got[0]["record_id"] == rid and got[0]["user_id"] == uid
    # re-sending the same record (a retry) does not raise a second alert
    with store.transaction() as s:
        for r in s.scalars(select(DecisionOutbox)):
            r.synced_at = None
    agent.push()
    assert len(alerts(central)) == 1


def test_allow_before_the_suspension_raises_nothing(env):
    central, store, agent, clock, uid = env
    add_rows(store, clock, 1, state="DEGRADED", final="ALLOW", user_id=uid)   # before
    clock.advance(minutes=1)
    central.client.patch(f"/users/{uid}", json={"standing": "suspended"})
    agent.push()
    assert alerts(central) == []


def test_online_allow_deny_and_reinstated_raise_nothing(env):
    central, store, agent, clock, uid = env
    clock.advance(minutes=1)
    central.client.patch(f"/users/{uid}", json={"standing": "suspended"})
    clock.advance(minutes=1)
    add_rows(store, clock, 1, state="ONLINE", final="ALLOW", user_id=uid)     # not offline
    add_rows(store, clock, 1, state="DEGRADED", final="DENY", user_id=uid)    # not an ALLOW
    clock.advance(minutes=1)
    central.client.patch(f"/users/{uid}", json={"standing": "active"})       # reinstated
    clock.advance(minutes=1)
    add_rows(store, clock, 1, state="DEGRADED", final="ALLOW", user_id=uid)   # allowed again
    agent.push()
    assert alerts(central) == []


def test_repeated_failed_attempts_raise_an_alert(env):
    central, store, agent, clock, _ = env
    rows = add_rows(store, clock, 1, final="DENY", user_id=None)
    with store.transaction() as s:
        row = s.scalars(select(DecisionOutbox).where(DecisionOutbox.record_id == rows[0])).one()
        row.reason = "Repeated failed attempts"
        from backend.outbox import HASHED_FIELDS, record_hash
        row.record_hash = record_hash(row.prev_hash, {k: getattr(row, k) for k in HASHED_FIELDS})
    agent.push()
    assert len(alerts(central, "repeated_failed_attempts")) == 1
