"""Persist one analyzed frame: identities, state transitions, events, reviews, outbox jobs.

Everything derived from a frame is written in a single transaction, so a crash either
loses the whole frame (it will be re-observed) or none of it. Event and job dedup keys are
derived from persisted state, so re-processing the same screen after a restart yields no
duplicate events, notifications or replies.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xianyu_monitor import state_keys
from xianyu_monitor.db import Database, get_state, set_state
from xianyu_monitor.detection.identity import CustomerDirectory, DirectoryEntry, MatchOutcome, match_row
from xianyu_monitor.detection.textnorm import normalize, normalize_name
from xianyu_monitor.detection.timeparse import is_recent, parse_relative
from xianyu_monitor.detection.tracker import RowState, Transition, observe
from xianyu_monitor.models import (
    ConversationState,
    Customer,
    CustomerAlias,
    CustomerAvatar,
    Frame,
    Job,
    MessageEvent,
    ReviewItem,
)
from xianyu_monitor.queueing.jobs import enqueue
from xianyu_monitor.redact import pseudonym
from xianyu_monitor.replies.planner import load_rules, plan_reply
from xianyu_monitor.runtime_settings import RuntimeSettings
from xianyu_monitor.timeutil import iso, utcnow
from xianyu_monitor.vision.analyzer import FrameObservation, RowObservation
from xianyu_monitor.vision.pane import canonical_title

log = logging.getLogger(__name__)
MAX_AVATARS_PER_CUSTOMER = 5


@dataclass
class ProcessResult:
    frame_id: int | None = None
    events: list[int] = field(default_factory=list)
    new_customers: list[str] = field(default_factory=list)
    reviews: list[int] = field(default_factory=list)
    jobs: list[int] = field(default_factory=list)
    intents: list[int] = field(default_factory=list)


async def load_directory(session: AsyncSession) -> CustomerDirectory:
    customers = (await session.execute(select(Customer))).scalars().all()
    entries = {c.id: DirectoryEntry(c.id, c.display_name, c.status) for c in customers}
    for alias in (await session.execute(select(CustomerAlias))).scalars().all():
        if alias.customer_id in entries:
            entries[alias.customer_id].aliases.add(alias.alias_norm)
    for avatar in (await session.execute(select(CustomerAvatar))).scalars().all():
        if avatar.customer_id in entries:
            entries[avatar.customer_id].avatars.add(avatar.avatar_hash)
    return CustomerDirectory(list(entries.values()))


def state_from_row(row: ConversationState | None) -> RowState | None:
    if row is None:
        return None
    return RowState(row.preview_raw, row.preview_norm, row.preview_conf, row.badge_present, row.badge_count,
                    row.badge_digest, row.time_text, row.time_age_min, row.position, row.observed_at,
                    row.revision, row.digest)


def apply_state(target: ConversationState, state: RowState) -> None:
    target.preview_raw = state.preview_raw
    target.preview_norm = state.preview_norm
    target.preview_conf = state.preview_conf
    target.badge_present = state.badge_present
    target.badge_count = state.badge_count
    target.badge_digest = state.badge_digest
    target.time_text = state.time_text
    target.time_age_min = state.time_age_min
    target.position = state.position
    target.observed_at = state.observed_at or utcnow()
    target.revision = state.revision
    target.digest = state.digest


async def upsert_review(session: AsyncSession, kind: str, row: RowObservation, outcome: MatchOutcome | None,
                        frame_id: int | None, reasons: list[str], customer_id: str | None = None) -> ReviewItem:
    basis = f"{kind}|{normalize_name(row.name)}|{row.avatar_hash or '-'}|{customer_id or '-'}"
    key = "rv:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:64]
    item = (await session.execute(select(ReviewItem).where(ReviewItem.dedup_key == key))).scalar_one_or_none()
    now = utcnow()
    if item is not None:
        item.seen_count += 1
        item.last_seen_at = now
        item.observed_preview = row.preview or item.observed_preview
        item.frame_id = frame_id or item.frame_id
        return item
    item = ReviewItem(kind=kind, status="open", dedup_key=key, customer_id=customer_id or (outcome.customer_id if outcome else None),
                      observed_name=row.name[:255], observed_preview=row.preview, avatar_hash=row.avatar_hash,
                      frame_id=frame_id, reasons=reasons, candidates=(outcome.candidates if outcome else []),
                      created_at=now, last_seen_at=now)
    session.add(item)
    await session.flush()
    return item


def new_customer_payload(customer: Customer, row: RowObservation, signals: list[str], observed_at) -> dict:
    return {"type": "new_customer", "customer_id": customer.id, "display_name": customer.display_name,
            "preview": row.preview, "observed_at": iso(observed_at), "signals": signals,
            "name_conf": round(row.name_conf, 3), "preview_conf": round(row.preview_conf, 3)}


async def enqueue_new_customer_email(session: AsyncSession, customer: Customer, payload: dict,
                                     rt: RuntimeSettings) -> int:
    job, _ = await enqueue(session, "email", f"new_customer:{customer.id}", payload,
                           priority=5, max_attempts=int(rt.email_max_attempts))
    return job.id


class FrameProcessor:
    def __init__(self, db: Database, worker_name: str):
        self.db = db
        self.worker_name = worker_name

    async def _pane_evidence(self, session, obs, row, customer, rt):
        """Persist only title-bound, confident pane evidence; first samples aren't messages."""
        pane = obs.pane
        if (not row.selected or sum(r.selected for r in obs.rows) != 1 or pane is None
                or pane.title_conf < float(rt.ocr_min_confidence) or pane.send_failure):
            return None, False, False
        aliases = set((await session.execute(select(CustomerAlias.alias_norm).where(
            CustomerAlias.customer_id == customer.id))).scalars())
        if canonical_title(pane.title) not in aliases:
            return None, False, False
        last = pane.last_bubble
        if last is None or last.confidence < float(rt.ocr_min_confidence) or last.direction == "unknown":
            return None, False, False
        key = f"pane_evidence:{customer.id}"
        previous = await get_state(session, key)
        digest = hashlib.sha256(json.dumps([last.direction, normalize(last.text), last.box,
                                           len(pane.bubbles)], ensure_ascii=False).encode()).hexdigest()
        await set_state(session, key, {"digest": digest})
        concordant = (row.preview_conf >= float(rt.ocr_min_confidence)
                      and normalize(row.preview).replace(" ", "") == normalize(last.text).replace(" ", ""))
        return last.direction, bool(previous.get("digest") and previous["digest"] != digest and concordant), concordant

    async def record_frame(self, session: AsyncSession, obs: FrameObservation, capture_ms: float,
                           image_path: str | None) -> Frame:
        frame = Frame(captured_at=obs.captured_at, worker=self.worker_name, width=obs.width, height=obs.height,
                      status=obs.status, changed=obs.changed, ocr_performed=obs.ocr_performed,
                      capture_ms=capture_ms, ocr_ms=obs.ocr_ms, total_ms=capture_ms + obs.analysis_ms,
                      overall_unread=obs.overall_unread, rows_seen=sum(1 for r in obs.rows if r.present),
                      image_path=image_path,
                      summary={"issues": obs.layout_issues, "challenge": obs.challenge_hits,
                               "low_conf_rows": sum(1 for r in obs.rows if r.present and r.name_conf < 0.5)})
        session.add(frame)
        await session.flush()
        return frame

    async def process(self, obs: FrameObservation, rt: RuntimeSettings, *, capture_ms: float = 0.0,
                      image_path: str | None = None, pane_direction: str | None = None) -> ProcessResult:
        result = ProcessResult()
        async with self.db.transaction() as session:
            frame = await self.record_frame(session, obs, capture_ms, image_path)
            result.frame_id = frame.id
            if obs.status != "ok":
                return result
            directory = await load_directory(session)
            rules = await load_rules(session) if rt.reply_mode != "off" else []
            present = [r for r in obs.rows if r.present]
            name_counts = Counter(normalize_name(r.name) for r in present if r.name)
            for row in present:
                outcome = match_row(row, directory, rt)
                norm = normalize_name(row.name)
                if name_counts.get(norm, 0) > 1 and not (outcome.kind == "matched"
                                                         and "duplicate_name_avatar_resolved" in outcome.reasons):
                    if outcome.kind not in {"system", "ignored"}:
                        outcome = MatchOutcome("uncertain", None, ["duplicate_nickname_in_frame"],
                                               outcome.candidates, norm)
                await self._handle_row(session, row, outcome, directory, obs, frame.id, rt, rules, result,
                                       pane_direction if row.selected else None)
            await set_state(session, state_keys.HEALTH, {
                **(await get_state(session, state_keys.HEALTH)),
                "status": "ok", "last_ok_frame_at": iso(obs.captured_at), "rows_visible": len(present),
                "coverage": f"visible rows only ({len(present)}/{len(obs.rows)})",
            })
        return result

    async def _create_customer(self, session: AsyncSession, row: RowObservation, status: str,
                               directory: CustomerDirectory, now) -> Customer:
        customer = Customer(display_name=row.name[:255], status=status, is_baseline=status == "baseline",
                            first_observed_at=now, last_observed_at=now, created_at=now, updated_at=now)
        session.add(customer)
        await session.flush()
        norm = normalize_name(row.name)
        session.add(CustomerAlias(customer_id=customer.id, alias_norm=norm[:191], display_name=row.name[:255],
                                  source="ocr", created_at=now))
        entry = DirectoryEntry(customer.id, customer.display_name, status, {norm}, set())
        if row.avatar_hash:
            session.add(CustomerAvatar(customer_id=customer.id, avatar_hash=row.avatar_hash, created_at=now))
            entry.avatars.add(row.avatar_hash)
        directory.add(entry)
        return customer

    async def _maybe_add_avatar(self, session: AsyncSession, customer_id: str, row: RowObservation,
                                directory: CustomerDirectory) -> None:
        if not row.avatar_hash:
            return
        entry = directory.entries.get(customer_id)
        if entry is None or row.avatar_hash in entry.avatars or len(entry.avatars) >= MAX_AVATARS_PER_CUSTOMER:
            return
        if entry.avatars:
            return  # only the first avatar is learned automatically; changes need owner review
        session.add(CustomerAvatar(customer_id=customer_id, avatar_hash=row.avatar_hash, created_at=utcnow()))
        directory.add_avatar(customer_id, row.avatar_hash)

    async def _record_transition(self, session: AsyncSession, customer: Customer, row: RowObservation,
                                 transition: Transition, frame_id: int, obs: FrameObservation,
                                 state_row: ConversationState | None) -> MessageEvent | None:
        if transition.state_changed:
            if state_row is None:
                state_row = ConversationState(customer_id=customer.id)
                session.add(state_row)
            apply_state(state_row, transition.new_state)
        if transition.kind is None:
            return None
        existing = (await session.execute(
            select(MessageEvent).where(MessageEvent.dedup_key == transition.dedup_key))).scalar_one_or_none()
        if existing is not None:
            return None
        event = MessageEvent(customer_id=customer.id, frame_id=frame_id, kind=transition.kind,
                             direction=transition.direction, preview_raw=row.preview,
                             preview_norm=transition.new_state.preview_norm,
                             preview_truncated=transition.preview_truncated,
                             badge_before=transition.badge_before, badge_after=transition.badge_after,
                             signals=transition.signals, confidence=transition.confidence,
                             dedup_key=transition.dedup_key, captured_at=obs.captured_at, created_at=utcnow())
        session.add(event)
        await session.flush()
        log.info("event %s for %s signals=%s", transition.kind, pseudonym(customer.id, "cust"), transition.signals)
        return event

    async def _handle_row(self, session: AsyncSession, row: RowObservation, outcome: MatchOutcome,
                          directory: CustomerDirectory, obs: FrameObservation, frame_id: int,
                          rt: RuntimeSettings, rules: list, result: ProcessResult,
                          pane_direction: str | None) -> None:
        now = obs.captured_at
        if outcome.kind in {"system", "ignored"}:
            return
        if outcome.kind == "uncertain":
            reasons = list(outcome.reasons) + (["incoming_badge_visible"] if row.badge_present else [])
            kind = "low_ocr_confidence" if "low_ocr_confidence" in outcome.reasons else "identity_uncertain"
            item = await upsert_review(session, kind, row, outcome, frame_id, reasons)
            result.reviews.append(item.id)
            return

        if outcome.kind == "new":
            if rt.monitor_mode == "baseline":
                customer = await self._create_customer(session, row, "baseline", directory, now)
                await self._pane_evidence(session, obs, row, customer, rt)
                transition = observe(None, row, now, rt, customer.id)
                transition.kind = None  # baseline seeding never produces events
                await self._record_transition(session, customer, row, transition, frame_id, obs, None)
                return
            customer = await self._create_customer(session, row, "new", directory, now)
            await self._pane_evidence(session, obs, row, customer, rt)
            result.new_customers.append(customer.id)
            transition = observe(None, row, now, rt, customer.id, pane_direction)
            event = await self._record_transition(session, customer, row, transition, frame_id, obs, None)
            if event is not None:
                result.events.append(event.id)
            rel = parse_relative(row.time_text, now) if row.time_conf >= float(rt.ocr_min_confidence) else None
            recent = rel is None or rel.age_minutes is None or is_recent(rel, float(rt.new_customer_max_age_minutes))
            if row.badge_present and recent and row.name_conf >= float(rt.ocr_min_confidence):
                signals = ["absent_from_baseline", "incoming_badge"] + (["recent_time_label"] if rel and rel.age_minutes is not None else [])
                if rt.notifications_enabled:
                    job_id = await enqueue_new_customer_email(session, customer,
                                                              new_customer_payload(customer, row, signals, now), rt)
                    result.jobs.append(job_id)
                else:
                    customer.notes = "notification not sent: notifications disabled"
            else:
                reason = "new_without_incoming_evidence" if not row.badge_present else "new_with_old_activity"
                item = await upsert_review(session, reason, row, outcome, frame_id, [reason], customer.id)
                result.reviews.append(item.id)
            if event is not None and rt.monitor_mode == "active":
                intent = await plan_reply(session, event, customer, rt, rules)
                if intent is not None:
                    result.intents.append(intent.id)
            return

        assert outcome.customer_id is not None
        customer = await session.get(Customer, outcome.customer_id)
        if customer is None:
            return
        state_row = await session.get(ConversationState, customer.id)
        if state_row is not None and now < state_row.observed_at:
            return  # late queued screenshots cannot rewind persisted conversation state
        customer.last_observed_at = now
        await self._maybe_add_avatar(session, customer.id, row, directory)
        verified_direction, pane_changed, pane_verified = await self._pane_evidence(session, obs, row, customer, rt)
        transition = observe(state_from_row(state_row), row, now, rt, customer.id,
                             verified_direction or pane_direction, pane_changed, pane_verified)
        event = await self._record_transition(session, customer, row, transition, frame_id, obs, state_row)
        if event is None:
            return
        result.events.append(event.id)
        if rt.monitor_mode == "active" and event.kind == "incoming":
            # A customer first seen after a mobile read has no badge at discovery.
            # Keep eligibility until later, confident incoming evidence arrives.
            # The durable outbox key also covers restarts and uncertain SMTP sends.
            if customer.status == "new" and not customer.is_baseline and rt.notifications_enabled:
                rel = parse_relative(row.time_text, now) if row.time_conf >= float(rt.ocr_min_confidence) else None
                recent = rel is None or rel.age_minutes is None or is_recent(rel, float(rt.new_customer_max_age_minutes))
                existing = (await session.execute(select(Job.id).where(
                    Job.dedup_key == f"new_customer:{customer.id}"))).scalar_one_or_none()
                if recent and existing is None:
                    job_id = await enqueue_new_customer_email(session, customer, new_customer_payload(
                        customer, row, ["absent_from_baseline", "deferred_incoming_evidence", *transition.signals], now), rt)
                    result.jobs.append(job_id)
                    reviews = (await session.execute(select(ReviewItem).where(
                        ReviewItem.customer_id == customer.id,
                        ReviewItem.kind == "new_without_incoming_evidence",
                        ReviewItem.status == "open"))).scalars().all()
                    for review in reviews:
                        review.status = "resolved"
                        review.resolution = "incoming_evidence_observed"
                        review.resolved_at = now
            intent = await plan_reply(session, event, customer, rt, rules)
            if intent is not None:
                result.intents.append(intent.id)
