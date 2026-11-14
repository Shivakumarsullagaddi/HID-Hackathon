"""Decision outbox records: canonical form and hash chain.

record_hash = sha256(prev_hash + canonical_json(record)), where record is the
outbox row without seq, prev_hash, record_hash and synced_at. The first row
uses prev_hash = 64 zeros. Central recomputes the chain on intake.
The same functions are used by central (central/intake.py).
"""
import hashlib
import json
from datetime import datetime

from sqlalchemy import select

GENESIS = "0" * 64
HASHED_FIELDS = (
    "record_id", "request_id", "stepup_of", "ts", "room_id", "user_id", "bio_decision",
    "final_decision", "reason", "kiosk_state", "snapshot_version", "liveness",
    "similarity", "latency_ms", "liveness_mode",
)
WIRE_FIELDS = ("seq",) + HASHED_FIELDS + ("prev_hash", "record_hash")


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _wire_value(v):
    if isinstance(v, datetime):
        return v.isoformat(timespec="microseconds")
    if isinstance(v, float):
        return round(v, 6)
    return v


def hashed_view(fields):
    """fields: mapping with at least HASHED_FIELDS (datetimes allowed)."""
    return {k: _wire_value(fields.get(k)) for k in HASHED_FIELDS}


def record_hash(prev_hash, fields):
    return hashlib.sha256((prev_hash + canonical_json(hashed_view(fields))).encode()).hexdigest()


def row_fields(row):
    return {k: getattr(row, k) for k in WIRE_FIELDS}


def to_wire(row):
    """Outbox row -> JSON-safe dict sent to central."""
    return {k: _wire_value(v) for k, v in row_fields(row).items()}


def chain_row(session, row):
    """Set prev_hash and record_hash on a new outbox row (call inside the
    transaction that inserts it, so the chain cannot fork)."""
    from .kiosk_models import DecisionOutbox
    last = session.scalars(select(DecisionOutbox.record_hash)
                           .order_by(DecisionOutbox.seq.desc()).limit(1)).first()
    row.prev_hash = last or GENESIS
    row.record_hash = record_hash(row.prev_hash, {k: getattr(row, k) for k in HASHED_FIELDS})
