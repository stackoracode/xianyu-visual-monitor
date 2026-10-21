"""MySQL-backed durable job queue (transactional outbox).

Guarantees:

* **Intent before side effect.** Jobs are inserted in the same transaction as the detection
  result that caused them. ``dedup_key`` is unique, so re-processing the same observation
  (after a crash or restart) cannot create a second job.
* **Leases.** Workers claim jobs with an optimistic conditional ``UPDATE`` (portable across
  MySQL and SQLite) and hold a time-limited lease. Completion is conditional on still owning
  the lease, so a stale worker cannot overwrite a newer attempt.
* **Phases.** Before an irreversible step (SMTP DATA, typing a reply) the phase is
  committed. Recovery treats a job that died after such a phase as ``uncertain`` and never
  retries it automatically. Exactly-once external delivery is *not* claimed.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from xianyu_monitor.db import Database
from xianyu_monitor.models import Job, JobAttempt
from xianyu_monitor.timeutil import utcnow

log = logging.getLogger(__name__)

TERMINAL = {"succeeded", "failed", "uncertain", "cancelled"}
# Phases after which an interrupted attempt may already have had an external effect.
IRREVERSIBLE_PHASES = {"submitting", "typing", "verifying_draft", "sending", "sent", "verifying_send"}
IRREVERSIBLE_PHASES |= {"image_preparing", "image_dialog", "image_path", "image_submitting", "image_verifying"}


@dataclass
class ClaimedJob:
    id: int
    kind: str
    dedup_key: str
    payload: dict
    attempts: int
    max_attempts: int
    owner: str
    attempt_id: int


async def enqueue(session: AsyncSession, kind: str, dedup_key: str, payload: dict, *, priority: int = 0,
                  max_attempts: int = 5, available_at: dt.datetime | None = None) -> tuple[Job, bool]:
    """Insert a job unless one with the same ``dedup_key`` exists. Returns (job, created)."""
    existing = (await session.execute(select(Job).where(Job.dedup_key == dedup_key))).scalar_one_or_none()
    if existing is not None:
        return existing, False
    now = utcnow()
    job = Job(kind=kind, dedup_key=dedup_key, payload=payload, priority=priority, max_attempts=max_attempts,
              status="pending", available_at=available_at or now, created_at=now, updated_at=now, phase="queued")
    try:
        async with session.begin_nested():
            session.add(job)
            await session.flush()
    except IntegrityError:
        # A concurrent transaction inserted the same key. A *shared* locking read sees the
        # latest committed row even under InnoDB REPEATABLE READ snapshots, and unlike
        # FOR UPDATE it does not deadlock with other waiters holding the same S lock.
        existing = (await session.execute(
            select(Job).where(Job.dedup_key == dedup_key).with_for_update(read=True))).scalar_one()
        return existing, False
    return job, True


class JobQueue:
    def __init__(self, db: Database, owner: str, lease_seconds: float = 120.0):
        self.db = db
        self.owner = owner
        self.lease_seconds = lease_seconds

    async def claim(self, kinds: list[str] | None = None, limit: int = 1) -> list[ClaimedJob]:
        if limit <= 0:
            return []
        now = utcnow()
        async with self.db.session() as session:
            query = (select(Job.id).where(Job.status == "pending", Job.available_at <= now)
                     .order_by(Job.priority.desc(), Job.available_at, Job.id).limit(limit * 3))
            if kinds:
                query = query.where(Job.kind.in_(kinds))
            candidate_ids = list((await session.execute(query)).scalars().all())
        claimed: list[ClaimedJob] = []
        for job_id in candidate_ids:
            if len(claimed) >= limit:
                break
            async with self.db.transaction() as session:
                lease_until = utcnow() + dt.timedelta(seconds=self.lease_seconds)
                result = await session.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.status == "pending")
                    .values(status="leased", lease_owner=self.owner, lease_expires_at=lease_until,
                            attempts=Job.attempts + 1, phase="started", updated_at=utcnow())
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    continue  # another worker won the race
                job = await session.get(Job, job_id)
                assert job is not None
                attempt = JobAttempt(job_id=job.id, attempt_no=job.attempts, owner=self.owner,
                                     started_at=utcnow(), phase="started")
                session.add(attempt)
                await session.flush()
                claimed.append(ClaimedJob(job.id, job.kind, job.dedup_key, dict(job.payload or {}),
                                          job.attempts, job.max_attempts, self.owner, attempt.id))
        return claimed

    async def _owned_update(self, job: ClaimedJob, values: dict, attempt_values: dict | None = None) -> bool:
        async with self.db.transaction() as session:
            result = await session.execute(
                update(Job).where(Job.id == job.id, Job.status == "leased", Job.lease_owner == job.owner)
                .values(updated_at=utcnow(), **values).execution_options(synchronize_session=False))
            if attempt_values:
                await session.execute(update(JobAttempt).where(JobAttempt.id == job.attempt_id)
                                      .values(**attempt_values).execution_options(synchronize_session=False))
            ok = result.rowcount == 1
        if not ok:
            log.warning("job %s lease lost before update %s", job.id, sorted(values))
        return ok

    async def set_phase(self, job: ClaimedJob, phase: str) -> bool:
        """Durably record progress; call *before* an irreversible step."""
        lease_until = utcnow() + dt.timedelta(seconds=self.lease_seconds)
        return await self._owned_update(job, {"phase": phase, "lease_expires_at": lease_until}, {"phase": phase})

    async def succeed(self, job: ClaimedJob, result: dict | None = None) -> bool:
        now = utcnow()
        return await self._owned_update(
            job, {"status": "succeeded", "phase": "done", "result": result or {}, "finished_at": now,
                  "lease_owner": None, "lease_expires_at": None, "last_error": None},
            {"outcome": "succeeded", "finished_at": now})

    async def fail(self, job: ClaimedJob, error: str, retryable: bool, backoff_seconds: float = 60.0) -> str:
        now = utcnow()
        if retryable and job.attempts < job.max_attempts:
            delay = backoff_seconds * (2 ** max(0, job.attempts - 1))
            await self._owned_update(
                job, {"status": "pending", "phase": "retry_wait", "last_error": error[:2000],
                      "available_at": now + dt.timedelta(seconds=min(delay, 6 * 3600)),
                      "lease_owner": None, "lease_expires_at": None},
                {"outcome": "retry", "error": error[:2000], "finished_at": now})
            return "pending"
        await self._owned_update(
            job, {"status": "failed", "phase": "done", "last_error": error[:2000], "finished_at": now,
                  "lease_owner": None, "lease_expires_at": None},
            {"outcome": "failed", "error": error[:2000], "finished_at": now})
        return "failed"

    async def mark_uncertain(self, job: ClaimedJob, error: str, result: dict | None = None) -> bool:
        now = utcnow()
        return await self._owned_update(
            job, {"status": "uncertain", "last_error": error[:2000], "finished_at": now, "result": result or {},
                  "lease_owner": None, "lease_expires_at": None},
            {"outcome": "uncertain", "error": error[:2000], "finished_at": now})

    async def recover(self, include_own_owner_prefix: str | None = None) -> dict[str, int]:
        """Release expired leases (and leases of a previous incarnation of this worker).

        Jobs interrupted after an irreversible phase become ``uncertain``; others return to
        ``pending`` without consuming an extra attempt budget beyond the one already counted.
        """
        now = utcnow()
        counts = {"requeued": 0, "uncertain": 0}
        async with self.db.transaction() as session:
            query = select(Job).where(Job.status == "leased")
            jobs = (await session.execute(query)).scalars().all()
            for job in jobs:
                expired = job.lease_expires_at is None or job.lease_expires_at <= now
                previous_self = bool(include_own_owner_prefix and job.lease_owner
                                     and job.lease_owner.startswith(include_own_owner_prefix)
                                     and job.lease_owner != self.owner)
                if not (expired or previous_self):
                    continue
                if job.phase in IRREVERSIBLE_PHASES:
                    job.status = "uncertain"
                    job.last_error = f"interrupted during phase {job.phase}; outcome unknown"
                    job.finished_at = now
                    counts["uncertain"] += 1
                else:
                    job.status = "pending"
                    job.phase = "recovered"
                    counts["requeued"] += 1
                job.lease_owner = None
                job.lease_expires_at = None
                job.updated_at = now
        if counts["requeued"] or counts["uncertain"]:
            log.warning("job recovery: %s", counts)
        return counts


async def admin_retry(session: AsyncSession, job_id: int, actor: str) -> bool:
    """Owner-initiated retry of a failed/uncertain/cancelled job (resets the attempt budget)."""
    job = await session.get(Job, job_id)
    if job is None or job.status not in {"failed", "uncertain", "cancelled"}:
        return False
    job.status = "pending"
    job.phase = f"manual_retry:{actor}"[:32]
    job.available_at = utcnow()
    job.max_attempts = job.attempts + 1  # exactly one more automatic attempt
    job.finished_at = None
    job.updated_at = utcnow()
    return True


async def admin_cancel(session: AsyncSession, job_id: int) -> bool:
    job = await session.get(Job, job_id)
    if job is None or job.status not in {"pending", "failed", "uncertain"}:
        return False
    job.status = "cancelled"
    job.finished_at = utcnow()
    job.updated_at = utcnow()
    return True


async def admin_resolve(session: AsyncSession, job_id: int, outcome: str) -> bool:
    """Owner confirms the real outcome of an ``uncertain`` job (e.g. email arrived)."""
    job = await session.get(Job, job_id)
    if job is None or job.status != "uncertain" or outcome not in {"succeeded", "failed"}:
        return False
    job.status = outcome
    job.result = {**(job.result or {}), "resolved_by_owner": True}
    job.updated_at = utcnow()
    return True
