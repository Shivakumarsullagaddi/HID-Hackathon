"""central: initial schema

Revision ID: c0001
Revises:
Create Date: 2026-11-14
"""
import sqlalchemy as sa
from alembic import op

from backend.db import UTCDateTime

revision = "c0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("standing", sa.String(16), nullable=False),
        sa.Column("pin_salt", sa.String(64), nullable=True),
        sa.Column("pin_hash", sa.String(128), nullable=True),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("standing IN ('active', 'suspended')", name="ck_users_standing"),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
    )
    op.create_table(
        "rooms",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("risk_level", sa.String(16), nullable=False),
        sa.Column("snapshot_ttl_s", sa.Integer(), nullable=False),
        sa.CheckConstraint("snapshot_ttl_s > 0", name="ck_rooms_snapshot_ttl_s"),
        sa.PrimaryKeyConstraint("id", name="pk_rooms"),
    )
    op.create_table(
        "templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(32), nullable=False),
        sa.Column("embedding", sa.LargeBinary(), nullable=False),
        sa.Column("model_id", sa.String(64), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE",
                                name="fk_templates_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_templates"),
        sa.UniqueConstraint("user_id", "model_id", name="uq_templates_user_id_model_id"),
    )
    op.create_index("ix_templates_user_id", "templates", ["user_id"])
    op.create_table(
        "schedule_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(32), nullable=False),
        sa.Column("room_id", sa.String(32), nullable=False),
        sa.Column("days_mask", sa.Integer(), nullable=False),
        sa.Column("start_min", sa.Integer(), nullable=False),
        sa.Column("end_min", sa.Integer(), nullable=False),
        sa.CheckConstraint("days_mask >= 0 AND days_mask <= 127", name="ck_schedule_rules_days_mask"),
        sa.CheckConstraint("start_min >= 0 AND start_min < 1440", name="ck_schedule_rules_start_min"),
        sa.CheckConstraint("end_min >= 0 AND end_min <= 1440", name="ck_schedule_rules_end_min"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE",
                                name="fk_schedule_rules_user_id_users"),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"], ondelete="CASCADE",
                                name="fk_schedule_rules_room_id_rooms"),
        sa.PrimaryKeyConstraint("id", name="pk_schedule_rules"),
    )
    op.create_index("ix_schedule_rules_user_id", "schedule_rules", ["user_id"])
    op.create_index("ix_schedule_rules_room_id", "schedule_rules", ["room_id"])
    op.create_table(
        "exam_windows",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("room_id", sa.String(32), nullable=False),
        sa.Column("starts_at", UTCDateTime(), nullable=False),
        sa.Column("ends_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("ends_at > starts_at", name="ck_exam_windows_order"),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"], ondelete="CASCADE",
                                name="fk_exam_windows_room_id_rooms"),
        sa.PrimaryKeyConstraint("id", name="pk_exam_windows"),
    )
    op.create_index("ix_exam_windows_room_id", "exam_windows", ["room_id"])
    op.create_table(
        "standing_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(32), nullable=False),
        sa.Column("old_standing", sa.String(16), nullable=True),
        sa.Column("new_standing", sa.String(16), nullable=False),
        sa.Column("at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE",
                                name="fk_standing_events_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_standing_events"),
    )
    op.create_index("ix_standing_events_user_id_at", "standing_events", ["user_id", "at"])
    op.create_table(
        "kiosks",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("room_id", sa.String(32), nullable=False),
        sa.Column("api_key_hash", sa.String(64), nullable=False),
        sa.Column("last_seen_at", UTCDateTime(), nullable=True),
        sa.Column("last_seq", sa.Integer(), nullable=False),
        sa.Column("last_hash", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"], name="fk_kiosks_room_id_rooms"),
        sa.PrimaryKeyConstraint("id", name="pk_kiosks"),
        sa.UniqueConstraint("api_key_hash", name="uq_kiosks_api_key_hash"),
    )
    op.create_table(
        "decisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kiosk_id", sa.String(64), nullable=False),
        sa.Column("record_id", sa.String(36), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("stepup_of", sa.String(36), nullable=True),
        sa.Column("ts", UTCDateTime(), nullable=False),
        sa.Column("room_id", sa.String(32), nullable=False),
        sa.Column("user_id", sa.String(32), nullable=True),
        sa.Column("bio_decision", sa.String(24), nullable=False),
        sa.Column("final_decision", sa.String(24), nullable=False),
        sa.Column("reason", sa.String(120), nullable=False),
        sa.Column("kiosk_state", sa.String(12), nullable=False),
        sa.Column("snapshot_version", sa.Integer(), nullable=True),
        sa.Column("liveness", sa.Float(), nullable=True),
        sa.Column("similarity", sa.Float(), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("liveness_mode", sa.String(12), nullable=True),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("record_hash", sa.String(64), nullable=False),
        sa.Column("received_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["kiosk_id"], ["kiosks.id"], name="fk_decisions_kiosk_id_kiosks"),
        sa.PrimaryKeyConstraint("id", name="pk_decisions"),
        sa.UniqueConstraint("record_id", name="uq_decisions_record_id"),
    )
    op.create_index("ix_decisions_kiosk_id", "decisions", ["kiosk_id"])
    op.create_index("ix_decisions_request_id", "decisions", ["request_id"])
    op.create_index("ix_decisions_ts", "decisions", ["ts"])
    op.create_index("ix_decisions_user_id", "decisions", ["user_id"])
    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(48), nullable=False),
        sa.Column("record_id", sa.String(36), nullable=True),
        sa.Column("user_id", sa.String(32), nullable=True),
        sa.Column("kiosk_id", sa.String(64), nullable=True),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_alerts"),
        sa.UniqueConstraint("type", "record_id", name="uq_alerts_type_record_id"),
    )
    op.create_index("ix_alerts_created_at", "alerts", ["created_at"])
    op.create_table(
        "central_meta",
        sa.Column("key", sa.String(32), nullable=False),
        sa.Column("value", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("key", name="pk_central_meta"),
    )


def downgrade():
    op.drop_table("central_meta")
    op.drop_index("ix_alerts_created_at", table_name="alerts")
    op.drop_table("alerts")
    for ix in ("ix_decisions_user_id", "ix_decisions_ts", "ix_decisions_request_id",
               "ix_decisions_kiosk_id"):
        op.drop_index(ix, table_name="decisions")
    op.drop_table("decisions")
    op.drop_table("kiosks")
    op.drop_index("ix_standing_events_user_id_at", table_name="standing_events")
    op.drop_table("standing_events")
    op.drop_index("ix_exam_windows_room_id", table_name="exam_windows")
    op.drop_table("exam_windows")
    op.drop_index("ix_schedule_rules_room_id", table_name="schedule_rules")
    op.drop_index("ix_schedule_rules_user_id", table_name="schedule_rules")
    op.drop_table("schedule_rules")
    op.drop_index("ix_templates_user_id", table_name="templates")
    op.drop_table("templates")
    op.drop_table("rooms")
    op.drop_table("users")
