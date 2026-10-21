"""Turn incoming message events into durable reply intents (and reply jobs when allowed)."""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from xianyu_monitor import state_keys
from xianyu_monitor.db import get_state
from xianyu_monitor.models import Customer, Job, JobAttempt, KeywordRule, MessageEvent, ReplyIntent
from xianyu_monitor.queueing.jobs import enqueue
from xianyu_monitor.replies.rules import Rule, evaluate
from xianyu_monitor.runtime_settings import RuntimeSettings
from xianyu_monitor.timeutil import utcnow

log = logging.getLogger(__name__)

SENT_STATUSES = ("queued", "sending", "sent_verified", "sent_unverified")
SAFE_PHASES = ("queued", "recovered", "started", "retry_wait", "precheck", "targeting", "verifying_target")


def rule_from_row(row: KeywordRule) -> Rule:
    return Rule(id=row.id, name=row.name, priority=row.priority, match_type=row.match_type,
                match_mode=row.match_mode, patterns=list(row.patterns or []),
                exclude_patterns=list(row.exclude_patterns or []), template=row.template,
                cooldown_seconds=row.cooldown_seconds, scope=row.scope, allow_sensitive=row.allow_sensitive,
                requires_review=row.requires_review, enabled=row.enabled,
                trigger_type=row.trigger_type, send_image=row.send_image)


async def load_rules(session: AsyncSession) -> list[Rule]:
    rows = (await session.execute(select(KeywordRule).where(KeywordRule.enabled.is_(True)))).scalars().all()
    return [rule_from_row(r) for r in rows]


async def _recent_count(session: AsyncSession, customer_id: str, since: dt.datetime,
                        rule_id: int | None = None) -> int:
    query = select(func.count(ReplyIntent.id)).where(
        ReplyIntent.customer_id == customer_id, ReplyIntent.status.in_(SENT_STATUSES),
        ReplyIntent.created_at >= since)
    if rule_id is not None:
        query = query.where(ReplyIntent.rule_id == rule_id)
    return int((await session.execute(query)).scalar_one())


async def enqueue_reply_job(session: AsyncSession, intent: ReplyIntent, delay_seconds: float = 0) -> None:
    key = f"reply:{intent.id}"
    if intent.job_id:
        previous = await session.get(Job, intent.job_id)
        if previous is not None and previous.status == "succeeded" and (
            (previous.result or {}).get("typed") is False
            or (previous.result or {}).get("returned_to_review")
            or (previous.result or {}).get("dry_run")
        ):
            key += f":after:{previous.id}"
    job, _ = await enqueue(session, "reply", key, {"intent_id": intent.id},
                           priority=10, max_attempts=3,
                           available_at=utcnow() + dt.timedelta(seconds=delay_seconds))
    intent.job_id = job.id
    intent.status = "queued"
    if intent.send_image:
        intent.image_status = "pending"
    intent.updated_at = utcnow()


async def supersede_unsent(session: AsyncSession, customer_id: str, event_id: int) -> None:
    """Coalesce a burst to its latest observed input, but never cancel typed/sent work."""
    candidates = (await session.execute(select(ReplyIntent).where(
        ReplyIntent.customer_id == customer_id, ReplyIntent.message_event_id < event_id,
        ReplyIntent.status.in_(["queued", "sending"])
    ))).scalars().all()
    for intent in candidates:
        result = await session.execute(update(Job).where(
            Job.id == intent.job_id, Job.status.in_(["pending", "leased"]), Job.phase.in_(SAFE_PHASES)
        ).values(status="cancelled", phase="superseded", lease_owner=None, lease_expires_at=None,
                 finished_at=utcnow(), updated_at=utcnow(), last_error="superseded_by_new_incoming")
          .execution_options(synchronize_session=False))
        if result.rowcount:
            intent.status = "superseded"
            intent.reasons = list(intent.reasons or []) + ["superseded_by_new_incoming"]
            intent.updated_at = utcnow()
            await session.execute(update(JobAttempt).where(
                JobAttempt.job_id == intent.job_id, JobAttempt.finished_at.is_(None)
            ).values(outcome="cancelled", phase="superseded", finished_at=utcnow(),
                     error="superseded_by_new_incoming"))


async def plan_reply(session: AsyncSession, event: MessageEvent, customer: Customer, rt: RuntimeSettings,
                     rules: list[Rule] | None = None) -> ReplyIntent | None:
    if rt.reply_mode == "off" or event.kind != "incoming" or event.direction != "incoming":
        return None
    if customer.status not in {"new", "known", "baseline"}:
        return None
    existing = (await session.execute(
        select(ReplyIntent).where(ReplyIntent.message_event_id == event.id))).scalar_one_or_none()
    if existing is not None:
        return existing
    await supersede_unsent(session, customer.id, event.id)
    await session.flush()
    prior_first = (await session.execute(select(ReplyIntent.id).where(
        ReplyIntent.customer_id == customer.id, ReplyIntent.first_contact.is_(True),
        ReplyIntent.status.notin_(["superseded", "rejected", "cancelled", "skipped", "failed"])
    ).limit(1))).scalar_one_or_none()
    first_contact = customer.status == "new" and not customer.is_baseline and prior_first is None
    rules = rules if rules is not None else await load_rules(session)
    decision = evaluate(event.preview_raw, rules, customer_status=customer.status,
                        customer_name=customer.display_name, sensitive_terms=rt.sensitive_terms,
                        attachment_markers=rt.attachment_markers, negations=rt.negation_terms,
                        truncated=event.preview_truncated, first_contact=first_contact)
    if decision.action == "none":
        return None
    reasons = list(decision.reasons)
    status = "review" if decision.action == "review" else "planned"
    now = utcnow()
    if event.confidence < float(rt.reply_min_confidence):
        reasons.append("low_detection_confidence")
        status = "review"
    if decision.rule is not None and status == "planned":
        since = now - dt.timedelta(seconds=max(0, decision.rule.cooldown_seconds))
        if await _recent_count(session, customer.id, since, decision.rule.id):
            reasons.append("cooldown_active")
            status = "skipped"
        elif int(rt.reply_max_per_customer_per_day) and await _recent_count(
                session, customer.id, now - dt.timedelta(days=1)) >= int(rt.reply_max_per_customer_per_day):
            reasons.append("daily_limit_reached")
            status = "skipped"
    paused = (await get_state(session, state_keys.REPLY_PAUSE)).get("paused", False)
    if status == "planned" and paused:
        reasons.append("replies_paused")
        status = "review"
    if status == "planned" and not rt.auto_reply_allowed:
        reasons.append("review_mode" if rt.reply_mode == "review" else "sending_not_enabled")
        status = "review"
    intent = ReplyIntent(message_event_id=event.id, customer_id=customer.id,
                         rule_id=decision.rule.id if decision.rule else None, status=status, reasons=reasons,
                         trigger_text=event.preview_raw, reply_text=decision.reply_text, created_at=now,
                         updated_at=now, first_contact=first_contact,
                         send_image=bool(decision.rule and decision.rule.send_image),
                         image_status="pending" if decision.rule and decision.rule.send_image else "none",
                         image_path=str(rt.reply_default_image_path) if decision.rule and decision.rule.send_image else None)
    session.add(intent)
    await session.flush()
    if status == "planned":
        await enqueue_reply_job(session, intent, float(rt.reply_debounce_seconds))
    return intent
