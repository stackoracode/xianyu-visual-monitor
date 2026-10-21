"""SQLAlchemy ORM models. MySQL (utf8mb4, InnoDB) is the source of truth.

All timestamps are naive UTC. Keep this file in sync with ``migrations/versions``; the test
suite compares the migrated schema against these models.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from xianyu_monitor.timeutil import utcnow

UtcDateTime = DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
LongText = Text().with_variant(mysql.MEDIUMTEXT(), "mysql")
BigId = BigInteger().with_variant(sqlite.INTEGER(), "sqlite")


def new_uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class AdminUser(Base):
    __tablename__ = "admin_users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="en")
    failed_logins: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class SettingRow(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[object] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")


class SystemState(Base):
    __tablename__ = "system_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class CalibrationProfile(Base):
    __tablename__ = "calibration_profiles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")


class Customer(Base):
    """Internal customer identity. Display names are aliases, never the identity itself."""

    __tablename__ = "customers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # baseline | known | new | uncertain | ignored | system | merged
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    is_baseline: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    first_observed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    last_observed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    notified_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    merged_into: Mapped[str | None] = mapped_column(String(36), ForeignKey("customers.id"), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class CustomerAlias(Base):
    __tablename__ = "customer_aliases"
    __table_args__ = (UniqueConstraint("customer_id", "alias_norm", name="uq_alias_customer_norm"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String(36), ForeignKey("customers.id"), nullable=False, index=True)
    alias_norm: Mapped[str] = mapped_column(String(191), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="ocr")
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class CustomerAvatar(Base):
    __tablename__ = "customer_avatars"
    __table_args__ = (UniqueConstraint("customer_id", "avatar_hash", name="uq_avatar_customer_hash"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String(36), ForeignKey("customers.id"), nullable=False, index=True)
    avatar_hash: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class ConversationState(Base):
    """Last persisted visible state per customer row; the basis of restart-safe diffing."""

    __tablename__ = "conversation_states"
    customer_id: Mapped[str] = mapped_column(String(36), ForeignKey("customers.id"), primary_key=True)
    preview_raw: Mapped[str] = mapped_column(Text, nullable=False, default="")
    preview_norm: Mapped[str] = mapped_column(Text, nullable=False, default="")
    preview_conf: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    badge_present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    badge_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    badge_digest: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    time_text: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    time_age_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=-1)
    observed_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    digest: Mapped[str] = mapped_column(String(64), nullable=False, default="")


class Frame(Base):
    __tablename__ = "frames"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    captured_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    worker: Mapped[str] = mapped_column(String(64), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    height: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # ok | unchanged | layout_mismatch | challenge | capture_failed | blank | paused
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    changed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ocr_performed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    capture_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    ocr_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    overall_unread: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rows_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    image_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class MessageEvent(Base):
    """A detected change in a conversation row. Not a complete message log."""

    __tablename__ = "message_events"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String(36), ForeignKey("customers.id"), nullable=False, index=True)
    frame_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # incoming | possible | outgoing | read_elsewhere
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    preview_raw: Mapped[str] = mapped_column(Text, nullable=False, default="")
    preview_norm: Mapped[str] = mapped_column(Text, nullable=False, default="")
    preview_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    badge_before: Mapped[int | None] = mapped_column(Integer, nullable=True)
    badge_after: Mapped[int | None] = mapped_column(Integer, nullable=True)
    signals: Mapped[list | None] = mapped_column(JSON, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    dedup_key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    captured_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow, index=True)


class ReviewItem(Base):
    __tablename__ = "review_items"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    # identity_uncertain | duplicate_nickname | possible_rename | low_ocr_confidence |
    # new_without_recent_activity | baseline_conflict | message_uncertain | reply_review
    kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)
    dedup_key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    customer_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("customers.id"), nullable=True)
    observed_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    observed_preview: Mapped[str] = mapped_column(Text, nullable=False, default="")
    avatar_hash: Mapped[str | None] = mapped_column(String(16), nullable=True)
    frame_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reasons: Mapped[list | None] = mapped_column(JSON, nullable=True)
    candidates: Mapped[list | None] = mapped_column(JSON, nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    seen_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    last_seen_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class Job(Base):
    """Durable outbox / task queue row. Intent is persisted before any side effect."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_available", "status", "available_at"),)
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    dedup_key: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    # pending | leased | succeeded | failed | uncertain | cancelled
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    available_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    lease_owner: Mapped[str | None] = mapped_column(String(96), nullable=True)
    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    phase: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)


class JobAttempt(Base):
    __tablename__ = "job_attempts"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("jobs.id"), nullable=False, index=True)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    owner: Mapped[str] = mapped_column(String(96), nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    phase: Mapped[str] = mapped_column(String(32), nullable=False, default="started")
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class KeywordRule(Base):
    __tablename__ = "keyword_rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    # contains | exact | regex
    match_type: Mapped[str] = mapped_column(String(16), nullable=False, default="contains")
    # any | all
    match_mode: Mapped[str] = mapped_column(String(8), nullable=False, default="any")
    patterns: Mapped[list] = mapped_column(JSON, nullable=False)
    exclude_patterns: Mapped[list] = mapped_column(JSON, nullable=False)
    template: Mapped[str] = mapped_column(Text, nullable=False)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)
    # all | new | known
    scope: Mapped[str] = mapped_column(String(8), nullable=False, default="all")
    allow_sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    requires_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    trigger_type: Mapped[str] = mapped_column(String(24), nullable=False, default="keyword")
    send_image: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class ReplyIntent(Base):
    __tablename__ = "reply_intents"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    message_event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("message_events.id"), nullable=False, unique=True)
    customer_id: Mapped[str] = mapped_column(String(36), ForeignKey("customers.id"), nullable=False, index=True)
    rule_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("keyword_rules.id"), nullable=True)
    # review | planned | approved | queued | sending | sent_verified | sent_unverified |
    # dry_run | rejected | skipped | failed | cancelled
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    reasons: Mapped[list | None] = mapped_column(JSON, nullable=True)
    trigger_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reply_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    first_contact: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    send_image: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    image_status: Mapped[str] = mapped_column(String(24), nullable=False, default="none")
    image_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    job_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    verification: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    sent_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)


class WorkerLease(Base):
    """Cross-host guard so only one capture worker drives a given desktop."""

    __tablename__ = "worker_leases"
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(96), nullable=False)
    heartbeat_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
