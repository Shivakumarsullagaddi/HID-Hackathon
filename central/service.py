"""Central service logic: registry, schedule, snapshot builder, decision
intake. Plain functions over a SQLAlchemy session so they are easy to test.
"""
import hashlib
import re
import secrets

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from backend.outbox import GENESIS, record_hash
from backend.pin import hash_pin
from backend.signing import ALG, b64_embedding, sign_body

from . import config
from .models import (Alert, Decision, ExamWindow, Kiosk, Meta, Room, ScheduleRule,
                     StandingEvent, Template, User)

VERSION_KEY = "snapshot_version"


class NotFound(Exception):
    pass


def key_hash(api_key):
    return hashlib.sha256(api_key.encode()).hexdigest()


def kiosk_for_key(s, api_key):
    if not api_key:
        return None
    return s.scalars(select(Kiosk).where(Kiosk.api_key_hash == key_hash(api_key))).first()


# ---- snapshot version ----------------------------------------------------

def current_version(s):
    row = s.get(Meta, VERSION_KEY)
    return int(row.value) if row else 0


def bump_version(s):
    row = s.get(Meta, VERSION_KEY)
    if row is None:
        row = Meta(key=VERSION_KEY, value="0")
        s.add(row)
    row.value = str(int(row.value) + 1)
    return int(row.value)


# ---- registry --------------------------------------------------------------

def next_user_id(s):
    ids = s.scalars(select(User.id)).all()
    n = max([int(i.split("_")[1]) for i in ids if re.fullmatch(r"u_\d+", i)] or [0]) + 1
    return f"u_{n:03d}"


def create_user(s, name, now, embedding=None, pin=None, standing="active",
                model_id=config.FACE_MODEL_ID, rules=(), user_id=None):
    user_id = user_id or next_user_id(s)
    salt = digest = None
    if pin:
        salt, digest = hash_pin(pin)
    s.add(User(id=user_id, name=name, standing=standing, pin_salt=salt, pin_hash=digest,
               updated_at=now))
    s.flush()
    if embedding is not None:
        arr = np.asarray(embedding, dtype=np.float32).reshape(-1)
        if arr.size != config.EMBEDDING_DIM:
            raise ValueError(f"embedding has {arr.size} values, expected {config.EMBEDDING_DIM}")
        s.add(Template(user_id=user_id, embedding=arr.tobytes(), model_id=model_id,
                       created_at=now))
    s.add(StandingEvent(user_id=user_id, old_standing=None, new_standing=standing, at=now))
    for r in rules:
        s.add(ScheduleRule(user_id=user_id, **r))
    bump_version(s)
    return user_id


def set_standing(s, user_id, standing, now):
    u = s.get(User, user_id)
    if u is None:
        raise NotFound(user_id)
    if u.standing != standing:
        s.add(StandingEvent(user_id=user_id, old_standing=u.standing, new_standing=standing,
                            at=now))
        u.standing = standing
        u.updated_at = now
        bump_version(s)
    return u


def add_rule(s, user_id, room_id, days_mask, start_min, end_min):
    if s.get(User, user_id) is None or s.get(Room, room_id) is None:
        raise NotFound(f"{user_id}/{room_id}")
    rule = ScheduleRule(user_id=user_id, room_id=room_id, days_mask=days_mask,
                        start_min=start_min, end_min=end_min)
    s.add(rule)
    bump_version(s)
    s.flush()
    return rule


def delete_rule(s, rule_id):
    rule = s.get(ScheduleRule, rule_id)
    if rule is None:
        raise NotFound(rule_id)
    s.delete(rule)
    bump_version(s)


def add_exam_window(s, room_id, starts_at, ends_at):
    if s.get(Room, room_id) is None:
        raise NotFound(room_id)
    w = ExamWindow(room_id=room_id, starts_at=starts_at, ends_at=ends_at)
    s.add(w)
    bump_version(s)
    s.flush()
    return w


def delete_exam_window(s, window_id):
    w = s.get(ExamWindow, window_id)
    if w is None:
        raise NotFound(window_id)
    s.delete(w)
    bump_version(s)


def upsert_room(s, room_id, name, risk_level, snapshot_ttl_s):
    room = s.get(Room, room_id)
    if room is None:
        s.add(Room(id=room_id, name=name, risk_level=risk_level, snapshot_ttl_s=snapshot_ttl_s))
        bump_version(s)
    return s.get(Room, room_id) or room


def create_kiosk(s, kiosk_id, room_id):
    """Returns the new API key. Only its hash is stored; show the key once."""
    api_key = secrets.token_urlsafe(32)
    s.add(Kiosk(id=kiosk_id, room_id=room_id, api_key_hash=key_hash(api_key), last_seq=0))
    return api_key


def rotate_kiosk_key(s, kiosk_id):
    k = s.get(Kiosk, kiosk_id)
    if k is None:
        raise NotFound(kiosk_id)
    api_key = secrets.token_urlsafe(32)
    k.api_key_hash = key_hash(api_key)
    return api_key


# ---- snapshot --------------------------------------------------------------

def _iso(dt):
    return dt.isoformat(timespec="microseconds")


def build_snapshot(s, kiosk, now, hmac_key):
    """Signed snapshot with only what this kiosk's room needs: users with a
    rule for the room plus suspended users (so "User suspended" can be shown),
    their templates and salted PIN hashes, the room's rules and current or
    upcoming exam windows."""
    room = s.get(Room, kiosk.room_id)
    rule_users = set(s.scalars(select(ScheduleRule.user_id)
                               .where(ScheduleRule.room_id == room.id)).all())
    suspended = set(s.scalars(select(User.id).where(User.standing == "suspended")).all())
    ids = sorted(rule_users | suspended)
    users = s.scalars(select(User).where(User.id.in_(ids)).order_by(User.id)).all()
    templates = s.scalars(select(Template).where(Template.user_id.in_(ids))
                          .order_by(Template.user_id, Template.model_id)).all()
    rules = s.scalars(select(ScheduleRule).where(ScheduleRule.room_id == room.id,
                                                 ScheduleRule.user_id.in_(ids))
                      .order_by(ScheduleRule.id)).all()
    windows = s.scalars(select(ExamWindow).where(ExamWindow.room_id == room.id,
                                                 ExamWindow.ends_at > now)
                        .order_by(ExamWindow.id)).all()
    body = {
        "schema_version": config.SCHEMA_VERSION,
        "version": current_version(s),
        "issued_at": _iso(now),
        "ttl_s": room.snapshot_ttl_s,
        "kiosk_id": kiosk.id,
        "room_id": room.id,
        "users": [{"user_id": u.id, "name": u.name, "standing": u.standing,
                   "pin_salt": u.pin_salt, "pin_hash": u.pin_hash} for u in users],
        "templates": [{"user_id": t.user_id, "model_id": t.model_id,
                       "embedding_b64": b64_embedding(t.embedding)} for t in templates],
        "schedule_rules": [{"id": r.id, "user_id": r.user_id, "room_id": r.room_id,
                            "days_mask": r.days_mask, "start_min": r.start_min,
                            "end_min": r.end_min} for r in rules],
        "exam_windows": [{"id": w.id, "room_id": w.room_id, "starts_at": _iso(w.starts_at),
                          "ends_at": _iso(w.ends_at)} for w in windows],
    }
    return {"alg": ALG, "body": body, "signature": sign_body(body, hmac_key)}


# ---- decision intake -------------------------------------------------------

DECISION_FIELDS = ("record_id", "seq", "request_id", "stepup_of", "ts", "room_id", "user_id",
                   "bio_decision", "final_decision", "reason", "kiosk_state",
                   "snapshot_version", "liveness", "similarity", "latency_ms",
                   "liveness_mode", "prev_hash", "record_hash")


def ingest(s, kiosk, records, now, on_new=None):
    """Insert a batch of outbox records. Duplicates (same record_id) are
    ignored, so a retry after a partial failure is harmless.

    records: dicts with DECISION_FIELDS (ts as aware datetime), seq order.
    on_new(session, kiosk, Decision) runs for each newly stored record.
    Returns (accepted_ids, duplicate_ids).
    """
    accepted, duplicates = [], []
    ids = [r["record_id"] for r in records]
    have = set(s.scalars(select(Decision.record_id).where(Decision.record_id.in_(ids))).all())
    for rec in sorted(records, key=lambda r: r["seq"]):
        if rec["record_id"] in have:
            duplicates.append(rec["record_id"])
            continue
        row = Decision(kiosk_id=kiosk.id, received_at=now,
                       **{k: rec.get(k) for k in DECISION_FIELDS})
        try:
            with s.begin_nested():
                s.add(row)
        except IntegrityError:   # raced with another push of the same record
            duplicates.append(rec["record_id"])
            continue
        have.add(rec["record_id"])
        accepted.append(rec["record_id"])
        if on_new is not None:
            on_new(s, kiosk, row)
    kiosk.last_seen_at = now
    return accepted, duplicates


def check_chain(s, kiosk, row):
    """Hash chain check for one new record. Writes an outbox_tampered alert
    when the record's own hash does not recompute, or the record does not
    follow on from the last record central holds from this kiosk (a record
    changed, removed, or the outbox reset). New records arrive in seq order.

    Limit: this catches edits and deletions relative to what central already
    has. Someone with write access to the kiosk database could rewrite the
    whole unsynced tail consistently; that needs a kiosk-held signing key
    (TPM) to stop, which this prototype does not have."""
    problems = []
    if record_hash(row.prev_hash, {k: getattr(row, k) for k in DECISION_FIELDS}) != row.record_hash:
        problems.append("record_hash does not match the record contents")
    last_seq, last_hash = kiosk.last_seq or 0, kiosk.last_hash
    if last_seq:
        if row.seq <= last_seq:
            problems.append(f"seq {row.seq} is not after {last_seq}: outbox reset or replayed")
        elif row.seq > last_seq + 1:
            problems.append(f"records {last_seq + 1} to {row.seq - 1} are missing")
        elif row.prev_hash != last_hash:
            problems.append(f"prev_hash does not match record {last_seq}: a record was changed or removed")
    elif row.seq == 1 and row.prev_hash != GENESIS:
        problems.append("first record does not start the chain")
    kiosk.last_seq, kiosk.last_hash = row.seq, row.record_hash
    if problems:
        add_alert(s, "outbox_tampered", row.received_at, record_id=row.record_id,
                  user_id=row.user_id, kiosk_id=kiosk.id, detail="; ".join(problems))


def add_alert(s, type_, now, record_id=None, user_id=None, kiosk_id=None, detail=""):
    exists = s.scalars(select(Alert.id).where(Alert.type == type_,
                                              Alert.record_id == record_id)).first()
    if exists is not None and record_id is not None:
        return None
    alert = Alert(type=type_, record_id=record_id, user_id=user_id, kiosk_id=kiosk_id,
                  detail=detail, created_at=now)
    s.add(alert)
    return alert


def counts(s):
    return {
        "users": s.scalar(select(func.count()).select_from(User)),
        "decisions": s.scalar(select(func.count()).select_from(Decision)),
        "alerts": s.scalar(select(func.count()).select_from(Alert)),
        "snapshot_version": current_version(s),
    }
