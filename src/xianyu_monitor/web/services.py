"""Administrative operations shared by the web UI, the CLI and tests."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from xianyu_monitor import state_keys
from xianyu_monitor.db import get_state, set_state
from xianyu_monitor.detection.textnorm import normalize_name
from xianyu_monitor.models import (
    AuditLog,
    ConversationState,
    Customer,
    CustomerAlias,
    CustomerAvatar,
    Job,
    MessageEvent,
    ReplyIntent,
    ReviewItem,
)
from xianyu_monitor.queueing.jobs import enqueue
from xianyu_monitor.replies.planner import enqueue_reply_job
from xianyu_monitor.runtime_settings import RuntimeSettings, load_runtime
from xianyu_monitor.timeutil import iso, utcnow

CUSTOMER_STATUSES = ("baseline", "known", "new", "ignored", "system")


class AdminError(ValueError):
    pass


async def audit(session: AsyncSession, actor: str, action: str, detail: dict | None = None) -> None:
    session.add(AuditLog(at=utcnow(), actor=actor, action=action, detail=detail or {}))


async def find_by_alias(session: AsyncSession, name: str) -> list[Customer]:
    norm = normalize_name(name)
    rows = (await session.execute(select(Customer).join(CustomerAlias, CustomerAlias.customer_id == Customer.id)
                                  .where(CustomerAlias.alias_norm == norm, Customer.status != "merged"))).scalars().all()
    return list({c.id: c for c in rows}.values())


async def create_customer(session: AsyncSession, name: str, status: str, source: str = "manual",
                          avatar_hash: str | None = None) -> Customer:
    if status not in CUSTOMER_STATUSES:
        raise AdminError("invalid status")
    name = name.strip()
    if not name:
        raise AdminError("name is required")
    now = utcnow()
    customer = Customer(display_name=name[:255], status=status, is_baseline=status == "baseline",
                        first_observed_at=None, created_at=now, updated_at=now)
    session.add(customer)
    await session.flush()
    session.add(CustomerAlias(customer_id=customer.id, alias_norm=normalize_name(name)[:191],
                              display_name=name[:255], source=source, created_at=now))
    if avatar_hash:
        session.add(CustomerAvatar(customer_id=customer.id, avatar_hash=avatar_hash, created_at=now))
    return customer


async def import_baseline(session: AsyncSession, names: list[str], actor: str) -> dict[str, int]:
    created = skipped = 0
    for raw in names:
        name = raw.strip()
        if not name:
            continue
        if await find_by_alias(session, name):
            skipped += 1
            continue
        await create_customer(session, name, "baseline", source="import")
        created += 1
    await audit(session, actor, "baseline_import", {"created": created, "skipped": skipped})
    return {"created": created, "skipped": skipped}


async def add_alias(session: AsyncSession, customer_id: str, name: str, actor: str) -> None:
    customer = await session.get(Customer, customer_id)
    if customer is None:
        raise AdminError("customer not found")
    norm = normalize_name(name)
    if not norm:
        raise AdminError("alias is empty")
    exists = (await session.execute(select(CustomerAlias).where(CustomerAlias.customer_id == customer_id,
                                                                CustomerAlias.alias_norm == norm))).scalar_one_or_none()
    if exists is None:
        session.add(CustomerAlias(customer_id=customer_id, alias_norm=norm[:191], display_name=name.strip()[:255],
                                  source="manual", created_at=utcnow()))
    await audit(session, actor, "alias_add", {"customer_id": customer_id})


async def remove_alias(session: AsyncSession, alias_id: int, actor: str) -> None:
    alias = await session.get(CustomerAlias, alias_id)
    if alias is None:
        raise AdminError("alias not found")
    count = (await session.execute(select(func.count(CustomerAlias.id))
                                   .where(CustomerAlias.customer_id == alias.customer_id))).scalar_one()
    if count <= 1:
        raise AdminError("a customer must keep at least one alias")
    await session.delete(alias)
    await audit(session, actor, "alias_remove", {"customer_id": alias.customer_id})


async def set_customer_status(session: AsyncSession, customer_id: str, status: str, notes: str | None,
                              actor: str) -> None:
    customer = await session.get(Customer, customer_id)
    if customer is None:
        raise AdminError("customer not found")
    if status not in CUSTOMER_STATUSES:
        raise AdminError("invalid status")
    customer.status = status
    customer.is_baseline = status == "baseline" or customer.is_baseline
    if notes is not None:
        customer.notes = notes[:2000]
    customer.updated_at = utcnow()
    await audit(session, actor, "customer_status", {"customer_id": customer_id, "status": status})


async def merge_customers(session: AsyncSession, source_id: str, target_id: str, actor: str) -> None:
    """Owner-confirmed identity merge: aliases, avatars and history move to the target."""
    if source_id == target_id:
        raise AdminError("cannot merge a customer into itself")
    source = await session.get(Customer, source_id)
    target = await session.get(Customer, target_id)
    if source is None or target is None or target.status == "merged":
        raise AdminError("customer not found")
    target_aliases = set((await session.execute(select(CustomerAlias.alias_norm)
                                                .where(CustomerAlias.customer_id == target_id))).scalars().all())
    for alias in (await session.execute(select(CustomerAlias).where(CustomerAlias.customer_id == source_id))).scalars():
        if alias.alias_norm in target_aliases:
            await session.delete(alias)
        else:
            alias.customer_id = target_id
    target_avatars = set((await session.execute(select(CustomerAvatar.avatar_hash)
                                                .where(CustomerAvatar.customer_id == target_id))).scalars().all())
    for avatar in (await session.execute(select(CustomerAvatar).where(CustomerAvatar.customer_id == source_id))).scalars():
        if avatar.avatar_hash in target_avatars:
            await session.delete(avatar)
        else:
            avatar.customer_id = target_id
    await session.execute(update(MessageEvent).where(MessageEvent.customer_id == source_id)
                          .values(customer_id=target_id))
    await session.execute(update(ReplyIntent).where(ReplyIntent.customer_id == source_id)
                          .values(customer_id=target_id))
    state = await session.get(ConversationState, source_id)
    if state is not None:
        await session.delete(state)
    if target.first_observed_at is None or (source.first_observed_at and source.first_observed_at < target.first_observed_at):
        target.first_observed_at = source.first_observed_at
    target.is_baseline = target.is_baseline or source.is_baseline
    source.status = "merged"
    source.merged_into = target_id
    source.updated_at = target.updated_at = utcnow()
    await audit(session, actor, "customer_merge", {"source": source_id, "target": target_id})


async def notify_customer(session: AsyncSession, customer_id: str, rt: RuntimeSettings, actor: str) -> tuple[int, bool]:
    customer = await session.get(Customer, customer_id)
    if customer is None:
        raise AdminError("customer not found")
    state = await session.get(ConversationState, customer_id)
    payload = {"type": "new_customer", "customer_id": customer.id, "display_name": customer.display_name,
               "preview": state.preview_raw if state else "", "observed_at": iso(customer.first_observed_at),
               "signals": ["owner_confirmed"], "name_conf": 1.0, "preview_conf": state.preview_conf if state else 0.0}
    job, created = await enqueue(session, "email", f"new_customer:{customer.id}", payload, priority=5,
                                 max_attempts=int(rt.email_max_attempts))
    await audit(session, actor, "notify_customer", {"customer_id": customer_id, "job_id": job.id, "created": created})
    return job.id, created


async def resolve_review(session: AsyncSession, item_id: int, action: str, actor: str,
                         customer_id: str | None = None) -> None:
    item = await session.get(ReviewItem, item_id)
    if item is None:
        raise AdminError("review item not found")
    if item.status != "open":
        raise AdminError("review item already resolved")
    rt = await load_runtime(session)
    target_id = customer_id or item.customer_id
    if action == "link":
        if not customer_id:
            raise AdminError("choose a customer to link")
        await add_alias(session, customer_id, item.observed_name, actor)
        if item.avatar_hash:
            exists = (await session.execute(select(CustomerAvatar).where(
                CustomerAvatar.customer_id == customer_id, CustomerAvatar.avatar_hash == item.avatar_hash))).first()
            if exists is None:
                session.add(CustomerAvatar(customer_id=customer_id, avatar_hash=item.avatar_hash, created_at=utcnow()))
        if item.customer_id and item.customer_id != customer_id:
            await merge_customers(session, item.customer_id, customer_id, actor)
    elif action in {"baseline", "ignore", "system", "new_customer"}:
        status = {"baseline": "baseline", "ignore": "ignored", "system": "system", "new_customer": "new"}[action]
        if target_id:
            await set_customer_status(session, target_id, status, None, actor)
        else:
            customer = await create_customer(session, item.observed_name, status, "review", item.avatar_hash)
            target_id = customer.id
            customer.first_observed_at = item.created_at
        if action == "new_customer":
            await notify_customer(session, target_id, rt, actor)
    elif action != "dismiss":
        raise AdminError("unknown action")
    item.status = "resolved" if action != "dismiss" else "dismissed"
    item.resolution = action
    item.resolved_by = actor
    item.resolved_at = utcnow()
    item.customer_id = target_id
    await audit(session, actor, "review_resolve", {"item": item_id, "action": action})


async def set_pause(session: AsyncSession, key: str, paused: bool, actor: str, reason: str = "manual") -> None:
    await set_state(session, key, {"paused": paused, "reason": reason if paused else None, "by": actor,
                                   "at": iso(utcnow())})
    await audit(session, actor, f"{key}:{'pause' if paused else 'resume'}", {"reason": reason})


async def approve_intent(session: AsyncSession, intent_id: int, actor: str, reply_text: str | None) -> str:
    intent = await session.get(ReplyIntent, intent_id)
    if intent is None:
        raise AdminError("reply not found")
    if intent.status not in {"review", "approved", "dry_run"}:
        raise AdminError(f"reply in status {intent.status} cannot be approved")
    if reply_text is not None:
        text = reply_text.strip()
        if not text or len(text) > 500:
            raise AdminError("reply text must be 1-500 characters")
        intent.reply_text = text
    rt = await load_runtime(session)
    paused = (await get_state(session, state_keys.REPLY_PAUSE)).get("paused", False)
    intent.decided_by = actor
    intent.updated_at = utcnow()
    if rt.reply_send_enabled and not paused and rt.reply_mode != "off":
        await enqueue_reply_job(session, intent)
    else:
        intent.status = "approved"
    await audit(session, actor, "reply_approve", {"intent": intent_id, "status": intent.status})
    return intent.status


async def reject_intent(session: AsyncSession, intent_id: int, actor: str) -> None:
    intent = await session.get(ReplyIntent, intent_id)
    if intent is None:
        raise AdminError("reply not found")
    if intent.status not in {"review", "approved", "planned", "dry_run"}:
        raise AdminError(f"reply in status {intent.status} cannot be rejected")
    intent.status = "rejected"
    intent.decided_by = actor
    intent.updated_at = utcnow()
    await audit(session, actor, "reply_reject", {"intent": intent_id})


async def dashboard_counts(session: AsyncSession) -> dict:
    def grouped(rows) -> dict:
        return {k: int(v) for k, v in rows}

    customers = grouped((await session.execute(select(Customer.status, func.count()).group_by(Customer.status))).all())
    reviews = int((await session.execute(select(func.count(ReviewItem.id)).where(ReviewItem.status == "open"))).scalar_one())
    jobs = grouped((await session.execute(select(Job.status, func.count()).group_by(Job.status))).all())
    intents = grouped((await session.execute(select(ReplyIntent.status, func.count())
                                             .group_by(ReplyIntent.status))).all())
    return {"customers": customers, "open_reviews": reviews, "jobs": jobs, "intents": intents}
