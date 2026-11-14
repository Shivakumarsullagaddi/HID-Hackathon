"""Applying a signed snapshot from central to the kiosk cache.

Checks, in order: algorithm and HMAC signature, supported schema_version,
addressed to this kiosk, newer than what we hold (a same-version snapshot
with a later issued_at is a freshness refresh). Then ONE transaction
replaces users_cache, templates, schedule_rules and exam_windows and
updates snapshot_meta. Any failure rolls back and keeps the old snapshot.
Templates made by another face model are refused (model_id check).
"""
import json
import logging
import os
from datetime import datetime

import numpy as np
from sqlalchemy import delete

from . import config
from .kiosk_models import ExamWindow, ScheduleRule, SnapshotMeta, Template, UserCache
from .outbox import canonical_json
from .signing import ALG, signature_ok, unb64_embedding

log = logging.getLogger("spoof.snapshot")


class SnapshotRejected(Exception):
    pass


def _dt(text):
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise SnapshotRejected("naive timestamp in snapshot")
    return dt


def check(payload, key, kiosk_id, current):
    """Returns the body if it should be applied, None if it is not newer.
    Raises SnapshotRejected if it must not be trusted."""
    if not isinstance(payload, dict) or payload.get("alg") != ALG:
        raise SnapshotRejected("unknown signature algorithm")
    body, sig = payload.get("body"), payload.get("signature")
    if not isinstance(body, dict) or not signature_ok(body, sig, key):
        raise SnapshotRejected("invalid signature")
    if body.get("schema_version") not in config.SUPPORTED_SCHEMA_VERSIONS:
        raise SnapshotRejected(f"unsupported schema_version {body.get('schema_version')}")
    if body.get("kiosk_id") != kiosk_id:
        raise SnapshotRejected(f"snapshot is for kiosk {body.get('kiosk_id')}")
    issued = _dt(body["issued_at"])
    if current is not None:
        if body["version"] < current.version:
            return None
        if body["version"] == current.version and issued <= current.issued_at:
            return None
    return body


def apply(store, payload, key, kiosk_id, now, fail_hook=None):
    """Verify and apply. Returns {"applied": bool, "reason": str, ...}."""
    current = store.snapshot_meta()
    try:
        body = check(payload, key, kiosk_id, current)
    except (SnapshotRejected, KeyError, TypeError, ValueError) as e:
        reason = str(e) if isinstance(e, SnapshotRejected) else f"malformed snapshot: {e!r}"
        log.warning("snapshot rejected: %s (kept version %s)", reason,
                    current.version if current else None)
        return {"applied": False, "rejected": True, "reason": reason}
    if body is None:
        return {"applied": False, "rejected": False, "reason": "not newer"}

    refused = 0
    try:
        with store.transaction() as s:
            s.execute(delete(Template))
            s.execute(delete(UserCache))
            s.execute(delete(ScheduleRule))
            s.execute(delete(ExamWindow))
            if fail_hook:
                fail_hook("after_delete")
            if os.environ.get("SPOOF_FAULT_APPLY") == "crash":
                # Fault injection for acceptance check 10: die mid-transaction.
                log.error("SPOOF_FAULT_APPLY=crash: exiting during snapshot apply")
                os._exit(3)
            for u in body["users"]:
                s.add(UserCache(user_id=u["user_id"], name=u["name"], standing=u["standing"],
                                pin_salt=u.get("pin_salt"), pin_hash=u.get("pin_hash")))
            s.flush()
            for t in body["templates"]:
                blob = unb64_embedding(t["embedding_b64"])
                if t["model_id"] != config.FACE_MODEL_ID or len(blob) != config.EMBEDDING_DIM * 4:
                    refused += 1
                    continue
                if not np.isfinite(np.frombuffer(blob, dtype=np.float32)).all():
                    refused += 1
                    continue
                s.add(Template(user_id=t["user_id"], embedding=blob, model_id=t["model_id"]))
            for r in body["schedule_rules"]:
                s.add(ScheduleRule(id=r["id"], user_id=r["user_id"], room_id=r["room_id"],
                                   days_mask=r["days_mask"], start_min=r["start_min"],
                                   end_min=r["end_min"]))
            for w in body["exam_windows"]:
                s.add(ExamWindow(id=w["id"], room_id=w["room_id"], starts_at=_dt(w["starts_at"]),
                                 ends_at=_dt(w["ends_at"])))
            if fail_hook:
                fail_hook("before_meta")
            meta = s.get(SnapshotMeta, 1) or SnapshotMeta(id=1)
            meta.version = body["version"]
            meta.schema_version = body["schema_version"]
            meta.issued_at = _dt(body["issued_at"])
            meta.received_at = now
            meta.ttl_s = int(body["ttl_s"])
            meta.kiosk_id = body["kiosk_id"]
            meta.room_id = body["room_id"]
            meta.signature = payload["signature"]
            meta.body_json = canonical_json(body)
            s.add(meta)
    except Exception as e:  # noqa: BLE001 - rolled back, old snapshot kept
        log.error("snapshot apply failed, rolled back: %s: %s", type(e).__name__, e)
        store.reload_cache()
        return {"applied": False, "rejected": True,
                "reason": f"apply failed: {type(e).__name__}: {e}"}
    store.reload_cache()
    if refused:
        log.warning("refused %d template(s) from another face model or malformed", refused)
    return {"applied": True, "rejected": False, "reason": "applied",
            "version": body["version"], "templates_refused": refused}


def stored_signature_ok(store, key):
    """Re-verify the snapshot we hold (startup check). None if there is none."""
    meta = store.snapshot_meta()
    if meta is None:
        return None
    try:
        return signature_ok(json.loads(meta.body_json), meta.signature, key)
    except ValueError:
        return False
