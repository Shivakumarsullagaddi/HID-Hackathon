"""Sync agent: push the decision outbox, then pull the snapshot.

Push: unsynced rows in seq order, batches of 100, to POST /decisions. Only
record_ids the server confirms (accepted or already-held duplicates) are
marked synced, so a retry after a partial failure is harmless. Rows synced
more than 24 h ago are deleted. Unsynced rows are never deleted by age; if
the outbox passes 50,000 rows the oldest SYNCED rows go first and a warning
shows on /api/state.

Pull: GET /snapshot?since=<version>. 304 = unchanged. Past half its TTL the
kiosk asks without `since` to get a freshly signed copy (a new issued_at).
A snapshot that fails verification is not applied and the sync counts as
failed, so a kiosk that cannot get trustworthy data drops to DEGRADED.

Order on reconnect: push first, then pull; the state manager sets ONLINE
only after both succeed.
"""
import logging

from sqlalchemy import delete, func, select, update

from . import config, snapshot
from .db import utcnow
from .kiosk_models import DecisionOutbox
from .outbox import to_wire

log = logging.getLogger("spoof.sync")


class SyncError(RuntimeError):
    pass


class SyncAgent:
    def __init__(self, store, http, central_url, kiosk_id, api_key, hmac_key, clock=None,
                 ttl_override_s=None, on_applied=None):
        self.store = store
        self.http = http
        self.base = central_url.rstrip("/")
        self.kiosk_id = kiosk_id
        self.api_key = api_key
        self.hmac_key = hmac_key
        self.clock = clock or utcnow
        self.ttl_override_s = ttl_override_s
        self.on_applied = on_applied
        self.warnings = []

    def _headers(self):
        return {"X-Kiosk-Key": self.api_key}

    # ---- push -------------------------------------------------------------
    def push(self, batch_size=None):
        batch_size = batch_size or config.OUTBOX_BATCH
        out = {"sent": 0, "accepted": 0, "duplicates": 0, "batches": 0}
        while True:
            with self.store.session() as s:
                rows = s.scalars(select(DecisionOutbox).where(DecisionOutbox.synced_at.is_(None))
                                 .order_by(DecisionOutbox.seq).limit(batch_size)).all()
            if not rows:
                return out
            ids = [r.record_id for r in rows]
            resp = self.http.post(f"{self.base}/decisions", headers=self._headers(),
                                  json={"records": [to_wire(r) for r in rows]})
            if resp.status_code != 200:
                raise SyncError(f"push failed: HTTP {resp.status_code} {resp.text[:200]}")
            body = resp.json()
            confirmed = (set(body.get("accepted", [])) | set(body.get("duplicate_ids", []))) & set(ids)
            if confirmed:
                with self.store.transaction() as s:
                    s.execute(update(DecisionOutbox)
                              .where(DecisionOutbox.record_id.in_(confirmed))
                              .values(synced_at=self.clock()))
            out["sent"] += len(rows)
            out["accepted"] += len(body.get("accepted", []))
            out["duplicates"] += int(body.get("duplicates", 0))
            out["batches"] += 1
            if len(confirmed) < len(ids):
                # Server did not confirm everything: stop, keep the rest unsynced.
                raise SyncError(f"server confirmed {len(confirmed)} of {len(ids)} records")

    def purge(self, now=None):
        now = now or self.clock()
        cutoff = now.timestamp() - config.OUTBOX_SYNCED_RETENTION_S
        from datetime import datetime, timezone
        cutoff_dt = datetime.fromtimestamp(cutoff, timezone.utc)
        with self.store.transaction() as s:
            # The newest row is always kept: it carries the hash chain head and
            # stops SQLite from restarting seq at 1 on an empty table.
            newest = s.scalar(select(func.max(DecisionOutbox.seq))) or 0
            purged = s.execute(delete(DecisionOutbox).where(
                DecisionOutbox.synced_at.is_not(None),
                DecisionOutbox.synced_at < cutoff_dt,
                DecisionOutbox.seq < newest)).rowcount
            total = s.scalar(select(func.count()).select_from(DecisionOutbox))
            over = total - config.OUTBOX_MAX_ROWS
            dropped = 0
            if over > 0:
                oldest = s.scalars(select(DecisionOutbox.seq)
                                   .where(DecisionOutbox.synced_at.is_not(None),
                                          DecisionOutbox.seq < newest)
                                   .order_by(DecisionOutbox.seq).limit(over)).all()
                if oldest:
                    dropped = s.execute(delete(DecisionOutbox)
                                        .where(DecisionOutbox.seq.in_(oldest))).rowcount
                total -= dropped
        self.warnings[:] = [w for w in self.warnings if not w.startswith("Outbox")]
        if total > config.OUTBOX_MAX_ROWS:
            self.warnings.append(f"Outbox has {total} rows, over the {config.OUTBOX_MAX_ROWS} "
                                 "limit, and none can be dropped: they are not synced yet")
        elif dropped:
            self.warnings.append(f"Outbox over its limit: dropped the oldest {dropped} synced rows")
        return {"purged": purged, "dropped_synced": dropped, "total": total}

    # ---- pull -------------------------------------------------------------
    def _wants_refresh(self, meta, now):
        ttl = meta.ttl_s
        if self.ttl_override_s:
            ttl = min(ttl, self.ttl_override_s)
        return (now - meta.issued_at).total_seconds() > ttl / 2

    def pull(self):
        now = self.clock()
        meta = self.store.snapshot_meta()
        params = {"kiosk_id": self.kiosk_id}
        if meta is not None and not self._wants_refresh(meta, now):
            params["since"] = meta.version
        resp = self.http.get(f"{self.base}/snapshot", params=params, headers=self._headers())
        if resp.status_code == 304:
            return {"status": "unchanged", "version": meta.version if meta else None}
        if resp.status_code != 200:
            raise SyncError(f"pull failed: HTTP {resp.status_code} {resp.text[:200]}")
        result = snapshot.apply(self.store, resp.json(), self.hmac_key, self.kiosk_id, now)
        if result.get("rejected"):
            raise SyncError(f"snapshot rejected: {result['reason']}")
        if result["applied"] and self.on_applied:
            self.on_applied()
        return {"status": "applied" if result["applied"] else result["reason"],
                "version": result.get("version", meta.version if meta else None),
                "templates_refused": result.get("templates_refused", 0)}

    # ---- both -------------------------------------------------------------
    def sync(self):
        """Push first, then pull. Raises on failure (the poll counts it)."""
        pushed = self.push()
        purged = self.purge()
        pulled = self.pull()
        summary = {"at": self.clock().isoformat(), "push": pushed, "purge": purged, "pull": pulled}
        if pushed["sent"] or pulled["status"] == "applied":
            log.info("sync: %s", summary)
        return summary

    # ---- enrolment ----------------------------------------------------------
    def enrol(self, name, embedding, pin):
        """Central owns users: enrol there, then pull the new snapshot."""
        resp = self.http.post(f"{self.base}/users", headers=self._headers(),
                              json={"name": name, "embedding": [float(x) for x in embedding],
                                    "pin": pin, "model_id": config.FACE_MODEL_ID})
        if resp.status_code != 200:
            raise SyncError(f"enrolment rejected: HTTP {resp.status_code} {resp.text[:200]}")
        body = resp.json()
        self.pull()
        return body
