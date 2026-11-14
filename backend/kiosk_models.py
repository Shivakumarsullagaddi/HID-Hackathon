"""Kiosk database tables (data/kiosk.db).

Snapshot cache (written only by a signed snapshot from central):
users_cache, templates, schedule_rules, exam_windows, snapshot_meta.
Audit: decision_outbox (synced to central), decision_log (local diagnostics).

decision_outbox and decision_log never hold frames, images or embeddings.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import (CheckConstraint, Float, ForeignKey, Integer, LargeBinary, String,
                        Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .db import UTCDateTime, new_metadata


class KioskBase(DeclarativeBase):
    metadata = new_metadata()


class UserCache(KioskBase):
    __tablename__ = "users_cache"
    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    standing: Mapped[str] = mapped_column(String(16))
    pin_salt: Mapped[Optional[str]] = mapped_column(String(64))
    pin_hash: Mapped[Optional[str]] = mapped_column(String(128))
    __table_args__ = (CheckConstraint("standing IN ('active', 'suspended')", name="standing"),)


class Template(KioskBase):
    __tablename__ = "templates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users_cache.user_id", ondelete="CASCADE"), index=True)
    embedding: Mapped[bytes] = mapped_column(LargeBinary)  # 512 x float32
    model_id: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("user_id", "model_id"),)


class ScheduleRule(KioskBase):
    __tablename__ = "schedule_rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    room_id: Mapped[str] = mapped_column(String(32))
    days_mask: Mapped[int] = mapped_column(Integer)   # bit 0 = Monday ... bit 6 = Sunday
    start_min: Mapped[int] = mapped_column(Integer)   # minutes since local midnight
    end_min: Mapped[int] = mapped_column(Integer)
    __table_args__ = (
        CheckConstraint("days_mask >= 0 AND days_mask <= 127", name="days_mask"),
        CheckConstraint("start_min >= 0 AND start_min < 1440", name="start_min"),
        CheckConstraint("end_min >= 0 AND end_min <= 1440", name="end_min"),
    )


class ExamWindow(KioskBase):
    __tablename__ = "exam_windows"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_id: Mapped[str] = mapped_column(String(32), index=True)
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ends_at: Mapped[datetime] = mapped_column(UTCDateTime)


class SnapshotMeta(KioskBase):
    """Single row (id = 1). body_json and signature are kept so the stored
    snapshot can be re-verified at startup."""
    __tablename__ = "snapshot_meta"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    schema_version: Mapped[int] = mapped_column(Integer)
    issued_at: Mapped[datetime] = mapped_column(UTCDateTime)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ttl_s: Mapped[int] = mapped_column(Integer)
    kiosk_id: Mapped[str] = mapped_column(String(64))
    room_id: Mapped[str] = mapped_column(String(32))
    signature: Mapped[str] = mapped_column(String(128))
    body_json: Mapped[str] = mapped_column(Text)
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)


class DecisionOutbox(KioskBase):
    __tablename__ = "decision_outbox"
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    record_id: Mapped[str] = mapped_column(String(36), unique=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    stepup_of: Mapped[Optional[str]] = mapped_column(String(36))
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    room_id: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[Optional[str]] = mapped_column(String(32))
    bio_decision: Mapped[str] = mapped_column(String(24))
    final_decision: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(String(120))
    kiosk_state: Mapped[str] = mapped_column(String(12))
    snapshot_version: Mapped[Optional[int]] = mapped_column(Integer)
    liveness: Mapped[Optional[float]] = mapped_column(Float)
    similarity: Mapped[Optional[float]] = mapped_column(Float)
    latency_ms: Mapped[Optional[float]] = mapped_column(Float)
    liveness_mode: Mapped[Optional[str]] = mapped_column(String(12))
    prev_hash: Mapped[str] = mapped_column(String(64))
    record_hash: Mapped[str] = mapped_column(String(64))
    synced_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, index=True)


class DecisionLog(KioskBase):
    """Local diagnostic log (kiosk, lab and harness runs): the full record,
    scalar signals only. Replaces data/decisions.jsonl. Never synced."""
    __tablename__ = "decision_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    stage: Mapped[str] = mapped_column(String(16))
    ts: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(16))
    decision: Mapped[str] = mapped_column(String(24))
    payload: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("request_id", "stage", "ts"),)
