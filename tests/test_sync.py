"""Sync agent against the real central app (TestClient as the transport)."""
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from backend import config
from backend.decision import DEGRADED, ONLINE
from backend.kiosk_models import DecisionOutbox
from backend.state import StateManager
from backend.store import Store
from backend.sync import SyncAgent, SyncError
from central_helpers import HMAC_KEY, Central, Clock
from test_central import emb

KIOSK = "kiosk_lab_a_1"


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    central = Central(tmp_path / "c", clock=clock)
    store = Store(tmp_path / "k")
    agent = SyncAgent(store, central.client, "http://testserver", KIOSK, central.api_key,
                      HMAC_KEY, clock=clock)
    yield central, store, agent, clock
    central.close()


def add_rows(store, clock, n, state="ONLINE", final="ALLOW", user_id="u_001"):
    ids = []
    for _ in range(n):
        rid = str(uuid.uuid4())
        row = DecisionOutbox(record_id=rid, request_id=uuid.uuid4().hex, ts=clock(),
                             room_id="lab_a", user_id=user_id, bio_decision="ALLOW",
                             final_decision=final, reason="x", kiosk_state=state,
                             snapshot_version=1, liveness=0.9, similarity=97.0,
                             latency_ms=300.0, liveness_mode="ensemble")
        store.append_access_record({"request_id": row.request_id, "ts": clock().isoformat(),
                                    "stage": "verify", "decision": "ALLOW"}, row)
        ids.append(rid)
    return ids


def unsynced(store):
    with store.session() as s:
        return s.scalars(select(DecisionOutbox.record_id)
                         .where(DecisionOutbox.synced_at.is_(None))).all()


def test_pull_applies_snapshot_then_304(env):
    central, store, agent, _ = env
    central.add_user("Alice", emb(1))
    assert agent.pull()["status"] == "applied"
    assert [u["name"] for u in store.users()] == ["Alice"]
    assert agent.pull()["status"] == "unchanged"


def test_push_is_idempotent(env):
    central, store, agent, clock = env
    add_rows(store, clock, 3)
    first = agent.push()
    assert (first["sent"], first["accepted"], first["duplicates"]) == (3, 3, 0)
    second = agent.push()
    assert (second["sent"], second["accepted"]) == (0, 0)          # nothing new to send
    assert len(central.client.get("/decisions").json()) == 3


def test_resending_synced_rows_reports_duplicates(env):
    central, store, agent, clock = env
    add_rows(store, clock, 2)
    agent.push()
    with store.transaction() as s:   # force a re-send of rows the server already has
        for r in s.scalars(select(DecisionOutbox)):
            r.synced_at = None
    again = agent.push()
    assert (again["accepted"], again["duplicates"]) == (0, 2)
    assert unsynced(store) == []


def test_push_in_batches_of_100_in_seq_order(env):
    central, store, agent, clock = env
    add_rows(store, clock, 250)
    out = agent.push()
    assert (out["sent"], out["accepted"], out["batches"]) == (250, 250, 3)
    seqs = [d["seq"] for d in central.client.get("/decisions", params={"limit": 500}).json()]
    assert sorted(seqs) == list(range(1, 251))


class LoseResponse:
    """Forwards to central, then loses the response (server stored it)."""
    def __init__(self, inner):
        self.inner, self.armed = inner, True

    def post(self, *a, **kw):
        r = self.inner.post(*a, **kw)
        if self.armed:
            self.armed = False
            raise ConnectionError("connection reset after send")
        return r

    def get(self, *a, **kw):
        return self.inner.get(*a, **kw)


def test_partial_failure_then_retry_is_harmless(env):
    central, store, agent, clock = env
    ids = add_rows(store, clock, 3)
    agent.http = LoseResponse(central.client)
    with pytest.raises(ConnectionError):
        agent.push()
    assert sorted(unsynced(store)) == sorted(ids)      # nothing marked: no confirmation
    out = agent.push()                                  # retry
    assert (out["accepted"], out["duplicates"]) == (0, 3)
    assert unsynced(store) == []
    assert len(central.client.get("/decisions").json()) == 3   # stored exactly once


class ConfirmSome:
    def __init__(self, keep):
        self.keep = keep

    def post(self, url, headers=None, json=None):
        ids = [r["record_id"] for r in json["records"]][: self.keep]

        class R:
            status_code = 200
            text = ""

            def json(self_inner):
                return {"accepted": ids, "duplicates": 0, "duplicate_ids": []}
        return R()


def test_only_confirmed_ids_are_marked_synced(env):
    _, store, agent, clock = env
    ids = add_rows(store, clock, 4)
    agent.http = ConfirmSome(keep=1)
    with pytest.raises(SyncError, match="confirmed 1 of 4"):
        agent.push()
    assert sorted(unsynced(store)) == sorted(ids[1:])


def test_purge_after_24h_never_touches_unsynced(env):
    _, store, agent, clock = env
    add_rows(store, clock, 2)
    agent.push()                                    # 2 synced now
    clock.advance(hours=1)
    keep_unsynced = add_rows(store, clock, 2)       # 2 never synced
    clock.advance(hours=23, minutes=1)              # synced rows are now > 24 h old
    out = agent.purge()
    assert out["purged"] == 2
    clock.advance(days=30)
    agent.purge()
    assert sorted(unsynced(store)) == sorted(keep_unsynced)


def test_outbox_over_limit_drops_oldest_synced_and_warns(env, monkeypatch):
    _, store, agent, clock = env
    monkeypatch.setattr(config, "OUTBOX_MAX_ROWS", 5)
    add_rows(store, clock, 4)
    agent.push()                                    # 4 synced
    add_rows(store, clock, 3)                       # 7 rows, 3 unsynced
    out = agent.purge()
    assert out["dropped_synced"] == 2 and out["total"] == 5
    assert any("dropped" in w for w in agent.warnings)
    add_rows(store, clock, 4)                       # 9 rows: 2 synced + 7 unsynced
    agent.purge()
    assert len(unsynced(store)) == 7                # unsynced never dropped
    assert any("none can be dropped" in w for w in agent.warnings)


def test_sync_pushes_before_pulling(env):
    central, store, agent, clock = env
    calls = []
    real = central.client

    class Recorder:
        def post(self, url, **kw):
            calls.append("push")
            return real.post(url, **kw)

        def get(self, url, **kw):
            calls.append("pull")
            return real.get(url, **kw)
    agent.http = Recorder()
    add_rows(store, clock, 1)
    agent.sync()
    assert calls == ["push", "pull"]


def test_tampered_snapshot_fails_the_sync_and_keeps_old(env):
    central, store, agent, _ = env
    central.add_user("Alice", emb(1))
    agent.pull()
    held = store.snapshot_meta().version
    central.client.patch("/users/u_001", json={"standing": "suspended"})
    real = central.client

    class Tamper:
        def post(self, *a, **kw):
            return real.post(*a, **kw)

        def get(self, url, **kw):
            r = real.get(url, **kw)
            data = r.json()
            data["body"]["users"][0]["standing"] = "active"   # undo the suspension in transit

            class R:
                status_code = 200
                text = ""

                def json(self_inner):
                    return data
            return R()
    agent.http = Tamper()
    with pytest.raises(SyncError, match="invalid signature"):
        agent.pull()
    assert store.snapshot_meta().version == held                 # old snapshot kept
    assert store.get_user("u_001")["standing"] == "active"


def test_refresh_after_half_ttl_gets_new_issued_at(env):
    central, store, agent, clock = env
    central.add_user("Alice", emb(1))
    agent.pull()
    first = store.snapshot_meta().issued_at
    clock.advance(seconds=200)
    assert agent.pull()["status"] == "unchanged"               # under half of 600 s
    clock.advance(seconds=150)                                  # 350 s: past half
    assert agent.pull()["status"] == "applied"
    assert store.snapshot_meta().issued_at > first


def test_state_manager_with_real_sync_reconnect_order(env):
    central, store, agent, clock = env
    central.add_user("Alice", emb(1))
    health = {"up": True}
    sm = StateManager(store, clock=clock, hmac_key=HMAC_KEY,
                      health=lambda: health["up"], sync=agent.sync, force_offline=False,
                      ttl_override_s=0)
    agent.on_applied = sm.snapshot_applied
    assert sm.poll_once() == ONLINE                 # first pull gives the snapshot
    health["up"] = False
    for _ in range(3):
        sm.poll_once()
    assert sm.current()[0] == DEGRADED
    add_rows(store, clock, 2, state="DEGRADED")
    health["up"] = True
    assert sm.poll_once() == ONLINE
    assert unsynced(store) == [] and sm.last_sync["push"]["accepted"] == 2
