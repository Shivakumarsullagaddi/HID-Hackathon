"""kiosk: initial schema (snapshot cache, decision outbox, decision log)

Revision ID: k0001
Revises:
Create Date: 2026-11-14
"""
import sqlalchemy as sa
from alembic import op

from backend.db import UTCDateTime

revision = "k0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users_cache",
        sa.Column("user_id", sa.String(32), nullable=False),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("standing", sa.String(16), nullable=False),
        sa.Column("pin_salt", sa.String(64), nullable=True),
        sa.Column("pin_hash", sa.String(128), nullable=True),
        sa.CheckConstraint("standing IN ('active', 'suspended')", name="ck_users_cache_standing"),
        sa.PrimaryKeyConstraint("user_id", name="pk_users_cache"),
    )
    op.create_table(
        "templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(32), nullable=False),
        sa.Column("embedding", sa.LargeBinary(), nullable=False),
        sa.Column("model_id", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users_cache.user_id"], ondelete="CASCADE",
                                name="fk_templates_user_id_users_cache"),
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
        sa.PrimaryKeyConstraint("id", name="pk_schedule_rules"),
    )
    op.create_index("ix_schedule_rules_user_id", "schedule_rules", ["user_id"])
    op.create_table(
        "exam_windows",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("room_id", sa.String(32), nullable=False),
        sa.Column("starts_at", UTCDateTime(), nullable=False),
        sa.Column("ends_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_exam_windows"),
    )
    op.create_index("ix_exam_windows_room_id", "exam_windows", ["room_id"])
    op.create_table(
        "snapshot_meta",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("issued_at", UTCDateTime(), nullable=False),
        sa.Column("received_at", UTCDateTime(), nullable=False),
        sa.Column("ttl_s", sa.Integer(), nullable=False),
        sa.Column("kiosk_id", sa.String(64), nullable=False),
        sa.Column("room_id", sa.String(32), nullable=False),
        sa.Column("signature", sa.String(128), nullable=False),
        sa.Column("body_json", sa.Text(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_snapshot_meta_single_row"),
        sa.PrimaryKeyConstraint("id", name="pk_snapshot_meta"),
    )
    op.create_table(
        "decision_outbox",
        sa.Column("seq", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("record_id", sa.String(36), nullable=False),
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
        sa.Column("synced_at", UTCDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("seq", name="pk_decision_outbox"),
        sa.UniqueConstraint("record_id", name="uq_decision_outbox_record_id"),
    )
    op.create_index("ix_decision_outbox_request_id", "decision_outbox", ["request_id"])
    op.create_index("ix_decision_outbox_synced_at", "decision_outbox", ["synced_at"])
    op.create_table(
        "decision_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("ts", sa.String(40), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("decision", sa.String(24), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_decision_log"),
        sa.UniqueConstraint("request_id", "stage", "ts", name="uq_decision_log_request_id_stage_ts"),
    )
    op.create_index("ix_decision_log_request_id", "decision_log", ["request_id"])


def downgrade():
    op.drop_index("ix_decision_log_request_id", table_name="decision_log")
    op.drop_table("decision_log")
    op.drop_index("ix_decision_outbox_synced_at", table_name="decision_outbox")
    op.drop_index("ix_decision_outbox_request_id", table_name="decision_outbox")
    op.drop_table("decision_outbox")
    op.drop_table("snapshot_meta")
    op.drop_index("ix_exam_windows_room_id", table_name="exam_windows")
    op.drop_table("exam_windows")
    op.drop_index("ix_schedule_rules_user_id", table_name="schedule_rules")
    op.drop_table("schedule_rules")
    op.drop_index("ix_templates_user_id", table_name="templates")
    op.drop_table("templates")
    op.drop_table("users_cache")
