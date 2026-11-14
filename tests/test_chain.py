"""Outbox hash chain: central raises outbox_tampered on a break, and a
healthy kiosk (including after purges) never raises one."""
import pytest
from sqlalchemy import delete, select

from backend.kiosk_models import DecisionOutbox
from backend.outbox import HASHED_FIELDS, record_hash
from backend.store import Store
from backend.sync import SyncAgent
from central_helpers import HMAC_KEY, Central, Clock
from test_sync import KIOSK, add_rows


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    central = Central(tmp_path / "c", clock=clock)
    store = Store(tmp_path / "k")
    agent = SyncAgent(store, central.client, "http://testserver", KIOSK, central.api_key,
                      HMAC_KEY, clock=clock)
    yield central, store, agent, clock
    central.close()


def tampered(central):
    return [a for a in central.client.get("/alerts").json() if a["type"] == "outbox_tampered"]


def test_healthy_chain_raises_nothing_and_tracks_head(env):
    central, store, agent, clock = env
    add_rows(store, clock, 3)
    agent.push()
    add_rows(store, clock, 2)
    agent.push()
    assert tampered(central) == []
    assert central.client.get("/kiosks").json()[0]["last_seq"] == 5


def test_edited_record_is_detected(env):
    central, store, agent, clock = env
    add_rows(store, clock, 2)
    agent.push()
    add_rows(store, clock, 2, final="DENY")
    with store.transaction() as s:    # change an unsynced DENY into an ALLOW, hash not updated
        row = s.scalars(select(DecisionOutbox).where(DecisionOutbox.seq == 3)).one()
        row.final_decision = "ALLOW"
    agent.push()
    got = tampered(central)
    assert len(got) == 1 and "does not match the record contents" in got[0]["detail"]


def test_edited_and_rehashed_record_breaks_the_next_link(env):
    central, store, agent, clock = env
    add_rows(store, clock, 1)
    agent.push()
    add_rows(store, clock, 2, final="DENY")
    with store.transaction() as s:    # attacker recomputes the edited row's own hash
        row = s.scalars(select(DecisionOutbox).where(DecisionOutbox.seq == 2)).one()
        row.final_decision = "ALLOW"
        row.record_hash = record_hash(row.prev_hash, {k: getattr(row, k) for k in HASHED_FIELDS})
    agent.push()
    got = tampered(central)
    assert len(got) == 1 and got[0]["record_id"] is not None
    assert "prev_hash does not match record 2" in got[0]["detail"]


def test_removed_record_is_detected(env):
    central, store, agent, clock = env
    add_rows(store, clock, 1)
    agent.push()
    add_rows(store, clock, 3)
    with store.transaction() as s:    # hide record 3
        s.execute(delete(DecisionOutbox).where(DecisionOutbox.seq == 3))
    agent.push()
    got = tampered(central)
    assert len(got) == 1 and "records 3 to 3 are missing" in got[0]["detail"]


def test_purge_keeps_the_chain_going(env):
    central, store, agent, clock = env
    add_rows(store, clock, 3)
    agent.push()
    clock.advance(hours=25)
    agent.purge()                                  # everything synced and old...
    with store.session() as s:
        left = s.scalars(select(DecisionOutbox.seq)).all()
    assert left == [3]                             # ...but the newest row stays
    add_rows(store, clock, 2)
    agent.push()
    with store.session() as s:
        seqs = s.scalars(select(DecisionOutbox.seq).order_by(DecisionOutbox.seq)).all()
    assert seqs == [3, 4, 5] and tampered(central) == []
