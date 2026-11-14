"""Central database tables (data/central.db today, PostgreSQL in production).

Central owns users, standing, schedule and the audit trail. standing_events
records when each standing change happened: that is what lets the
reconciler spot an offline ALLOW of someone already suspended.
decisions and alerts never hold frames, images or embeddings.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import (CheckConstraint, Float, ForeignKey, Index, Integer, LargeBinary,
                        String, Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from backend.db import UTCDateTime, new_metadata


class CentralBase(DeclarativeBase):
    metadata = new_metadata()


class User(CentralBase):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    standing: Mapped[str] = mapped_column(String(16))
    pin_salt: Mapped[Optional[str]] = mapped_column(String(64))
    pin_hash: Mapped[Optional[str]] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    __table_args__ = (CheckConstraint("standing IN ('active', 'suspended')", name="standing"),)


class Template(CentralBase):
    __tablename__ = "templates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    embedding: Mapped[bytes] = mapped_column(LargeBinary)
    model_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    __table_args__ = (UniqueConstraint("user_id", "model_id"),)


class Room(CentralBase):
    __tablename__ = "rooms"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    risk_level: Mapped[str] = mapped_column(String(16))
    snapshot_ttl_s: Mapped[int] = mapped_column(Integer)
    __table_args__ = (CheckConstraint("snapshot_ttl_s > 0", name="snapshot_ttl_s"),)


class ScheduleRule(CentralBase):
    __tablename__ = "schedule_rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    room_id: Mapped[str] = mapped_column(ForeignKey("rooms.id", ondelete="CASCADE"), index=True)
    days_mask: Mapped[int] = mapped_column(Integer)
    start_min: Mapped[int] = mapped_column(Integer)
    end_min: Mapped[int] = mapped_column(Integer)
    __table_args__ = (
        CheckConstraint("days_mask >= 0 AND days_mask <= 127", name="days_mask"),
        CheckConstraint("start_min >= 0 AND start_min < 1440", name="start_min"),
        CheckConstraint("end_min >= 0 AND end_min <= 1440", name="end_min"),
    )


class ExamWindow(CentralBase):
    __tablename__ = "exam_windows"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_id: Mapped[str] = mapped_column(ForeignKey("rooms.id", ondelete="CASCADE"), index=True)
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ends_at: Mapped[datetime] = mapped_column(UTCDateTime)
    __table_args__ = (CheckConstraint("ends_at > starts_at", name="order"),)


class StandingEvent(CentralBase):
    __tablename__ = "standing_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    old_standing: Mapped[Optional[str]] = mapped_column(String(16))
    new_standing: Mapped[str] = mapped_column(String(16))
    at: Mapped[datetime] = mapped_column(UTCDateTime)
    __table_args__ = (Index("ix_standing_events_user_id_at", "user_id", "at"),)


class Kiosk(CentralBase):
    __tablename__ = "kiosks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    room_id: Mapped[str] = mapped_column(ForeignKey("rooms.id"))
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 of the key
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    last_seq: Mapped[int] = mapped_column(Integer, default=0)
    last_hash: Mapped[Optional[str]] = mapped_column(String(64))   # outbox hash chain head


class Decision(CentralBase):
    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kiosk_id: Mapped[str] = mapped_column(ForeignKey("kiosks.id"), index=True)
    record_id: Mapped[str] = mapped_column(String(36), unique=True)
    seq: Mapped[int] = mapped_column(Integer)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    stepup_of: Mapped[Optional[str]] = mapped_column(String(36))
    ts: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    room_id: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[Optional[str]] = mapped_column(String(32), index=True)
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
    received_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Alert(CentralBase):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(48))
    record_id: Mapped[Optional[str]] = mapped_column(String(36))
    user_id: Mapped[Optional[str]] = mapped_column(String(32))
    kiosk_id: Mapped[Optional[str]] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    __table_args__ = (UniqueConstraint("type", "record_id"),)


class Meta(CentralBase):
    """Key-value settings; holds the global snapshot version counter."""
    __tablename__ = "central_meta"
    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[str] = mapped_column(String(64))
