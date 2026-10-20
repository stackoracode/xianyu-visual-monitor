"""Initial schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from xianyu_monitor.models import BigId, UtcDateTime

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

MYSQL = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"}


def upgrade() -> None:
    op.create_table(
        "admin_users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("language", sa.String(8), nullable=False),
        sa.Column("failed_logins", sa.Integer(), nullable=False),
        sa.Column("locked_until", UtcDateTime, nullable=True),
        sa.Column("created_at", UtcDateTime, nullable=False),
        sa.Column("last_login_at", UtcDateTime, nullable=True),
        **MYSQL,
    )
    op.create_table(
        "audit_log",
        sa.Column("id", BigId, primary_key=True, autoincrement=True),
        sa.Column("at", UtcDateTime, nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        **MYSQL,
    )
    op.create_index("ix_audit_log_at", "audit_log", ["at"])
    op.create_table(
        "settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("updated_at", UtcDateTime, nullable=False),
        sa.Column("updated_by", sa.String(64), nullable=False),
        **MYSQL,
    )
    op.create_table(
        "system_state",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("updated_at", UtcDateTime, nullable=False),
        **MYSQL,
    )
    op.create_table(
        "calibration_profiles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", UtcDateTime, nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        **MYSQL,
    )
    op.create_table(
        "customers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("is_baseline", sa.Boolean(), nullable=False),
        sa.Column("first_observed_at", UtcDateTime, nullable=True),
        sa.Column("last_observed_at", UtcDateTime, nullable=True),
        sa.Column("notified_at", UtcDateTime, nullable=True),
        sa.Column("merged_into", sa.String(36), sa.ForeignKey("customers.id"), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", UtcDateTime, nullable=False),
        sa.Column("updated_at", UtcDateTime, nullable=False),
        **MYSQL,
    )
    op.create_index("ix_customers_status", "customers", ["status"])
    op.create_table(
        "customer_aliases",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("alias_norm", sa.String(191), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("created_at", UtcDateTime, nullable=False),
        sa.UniqueConstraint("customer_id", "alias_norm", name="uq_alias_customer_norm"),
        **MYSQL,
    )
    op.create_index("ix_customer_aliases_customer_id", "customer_aliases", ["customer_id"])
    op.create_index("ix_customer_aliases_alias_norm", "customer_aliases", ["alias_norm"])
    op.create_table(
        "customer_avatars",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("avatar_hash", sa.String(16), nullable=False),
        sa.Column("created_at", UtcDateTime, nullable=False),
        sa.UniqueConstraint("customer_id", "avatar_hash", name="uq_avatar_customer_hash"),
        **MYSQL,
    )
    op.create_index("ix_customer_avatars_customer_id", "customer_avatars", ["customer_id"])
    op.create_table(
        "conversation_states",
        sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id"), primary_key=True),
        sa.Column("preview_raw", sa.Text(), nullable=False),
        sa.Column("preview_norm", sa.Text(), nullable=False),
        sa.Column("preview_conf", sa.Float(), nullable=False),
        sa.Column("badge_present", sa.Boolean(), nullable=False),
        sa.Column("badge_count", sa.Integer(), nullable=True),
        sa.Column("badge_digest", sa.String(32), nullable=False),
        sa.Column("time_text", sa.String(64), nullable=False),
        sa.Column("time_age_min", sa.Float(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("observed_at", UtcDateTime, nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        **MYSQL,
    )
    op.create_table(
        "frames",
        sa.Column("id", BigId, primary_key=True, autoincrement=True),
        sa.Column("captured_at", UtcDateTime, nullable=False),
        sa.Column("worker", sa.String(64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("changed", sa.Boolean(), nullable=False),
        sa.Column("ocr_performed", sa.Boolean(), nullable=False),
        sa.Column("capture_ms", sa.Float(), nullable=False),
        sa.Column("ocr_ms", sa.Float(), nullable=False),
        sa.Column("total_ms", sa.Float(), nullable=False),
        sa.Column("overall_unread", sa.Integer(), nullable=True),
        sa.Column("rows_seen", sa.Integer(), nullable=False),
        sa.Column("image_path", sa.String(255), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=True),
        **MYSQL,
    )
    op.create_index("ix_frames_captured_at", "frames", ["captured_at"])
    op.create_table(
        "message_events",
        sa.Column("id", BigId, primary_key=True, autoincrement=True),
        sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("frame_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("preview_raw", sa.Text(), nullable=False),
        sa.Column("preview_norm", sa.Text(), nullable=False),
        sa.Column("preview_truncated", sa.Boolean(), nullable=False),
        sa.Column("badge_before", sa.Integer(), nullable=True),
        sa.Column("badge_after", sa.Integer(), nullable=True),
        sa.Column("signals", sa.JSON(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("dedup_key", sa.String(80), nullable=False, unique=True),
        sa.Column("captured_at", UtcDateTime, nullable=False),
        sa.Column("created_at", UtcDateTime, nullable=False),
        **MYSQL,
    )
    op.create_index("ix_message_events_customer_id", "message_events", ["customer_id"])
    op.create_index("ix_message_events_kind", "message_events", ["kind"])
    op.create_index("ix_message_events_created_at", "message_events", ["created_at"])
    op.create_table(
        "review_items",
        sa.Column("id", BigId, primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("dedup_key", sa.String(80), nullable=False, unique=True),
        sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id"), nullable=True),
        sa.Column("observed_name", sa.String(255), nullable=False),
        sa.Column("observed_preview", sa.Text(), nullable=False),
        sa.Column("avatar_hash", sa.String(16), nullable=True),
        sa.Column("frame_id", sa.BigInteger(), nullable=True),
        sa.Column("reasons", sa.JSON(), nullable=True),
        sa.Column("candidates", sa.JSON(), nullable=True),
        sa.Column("resolution", sa.String(32), nullable=True),
        sa.Column("resolved_by", sa.String(64), nullable=True),
        sa.Column("resolved_at", UtcDateTime, nullable=True),
        sa.Column("seen_count", sa.Integer(), nullable=False),
        sa.Column("created_at", UtcDateTime, nullable=False),
        sa.Column("last_seen_at", UtcDateTime, nullable=False),
        **MYSQL,
    )
    op.create_index("ix_review_items_kind", "review_items", ["kind"])
    op.create_index("ix_review_items_status", "review_items", ["status"])
    op.create_table(
        "jobs",
        sa.Column("id", BigId, primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("dedup_key", sa.String(120), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", UtcDateTime, nullable=False),
        sa.Column("lease_owner", sa.String(96), nullable=True),
        sa.Column("lease_expires_at", UtcDateTime, nullable=True),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("created_at", UtcDateTime, nullable=False),
        sa.Column("updated_at", UtcDateTime, nullable=False),
        sa.Column("finished_at", UtcDateTime, nullable=True),
        **MYSQL,
    )
    op.create_index("ix_jobs_kind", "jobs", ["kind"])
    op.create_index("ix_jobs_status_available", "jobs", ["status", "available_at"])
    op.create_table(
        "job_attempts",
        sa.Column("id", BigId, primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.BigInteger(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("owner", sa.String(96), nullable=False),
        sa.Column("started_at", UtcDateTime, nullable=False),
        sa.Column("finished_at", UtcDateTime, nullable=True),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        **MYSQL,
    )
    op.create_index("ix_job_attempts_job_id", "job_attempts", ["job_id"])
    op.create_table(
        "keyword_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("match_type", sa.String(16), nullable=False),
        sa.Column("match_mode", sa.String(8), nullable=False),
        sa.Column("patterns", sa.JSON(), nullable=False),
        sa.Column("exclude_patterns", sa.JSON(), nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(8), nullable=False),
        sa.Column("allow_sensitive", sa.Boolean(), nullable=False),
        sa.Column("requires_review", sa.Boolean(), nullable=False),
        sa.Column("created_at", UtcDateTime, nullable=False),
        sa.Column("updated_at", UtcDateTime, nullable=False),
        **MYSQL,
    )
    op.create_table(
        "reply_intents",
        sa.Column("id", BigId, primary_key=True, autoincrement=True),
        sa.Column("message_event_id", sa.BigInteger(), sa.ForeignKey("message_events.id"), nullable=False,
                  unique=True),
        sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("rule_id", sa.Integer(), sa.ForeignKey("keyword_rules.id"), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=True),
        sa.Column("trigger_text", sa.Text(), nullable=False),
        sa.Column("reply_text", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.String(64), nullable=True),
        sa.Column("job_id", sa.BigInteger(), nullable=True),
        sa.Column("verification", sa.JSON(), nullable=True),
        sa.Column("created_at", UtcDateTime, nullable=False),
        sa.Column("updated_at", UtcDateTime, nullable=False),
        sa.Column("sent_at", UtcDateTime, nullable=True),
        **MYSQL,
    )
    op.create_index("ix_reply_intents_customer_id", "reply_intents", ["customer_id"])
    op.create_index("ix_reply_intents_status", "reply_intents", ["status"])
    op.create_table(
        "worker_leases",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("owner", sa.String(96), nullable=False),
        sa.Column("heartbeat_at", UtcDateTime, nullable=False),
        sa.Column("expires_at", UtcDateTime, nullable=False),
        **MYSQL,
    )


def downgrade() -> None:
    for table in ("worker_leases", "reply_intents", "keyword_rules", "job_attempts", "jobs", "review_items",
                  "message_events", "frames", "conversation_states", "customer_avatars", "customer_aliases",
                  "customers", "calibration_profiles", "system_state", "settings", "audit_log", "admin_users"):
        op.drop_table(table)
