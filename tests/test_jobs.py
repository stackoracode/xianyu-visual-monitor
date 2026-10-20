import asyncio
import datetime as dt

from sqlalchemy import select

from xianyu_monitor.models import Job
from xianyu_monitor.queueing.jobs import JobQueue, admin_resolve, admin_retry, enqueue
from xianyu_monitor.timeutil import utcnow


async def test_enqueue_is_deduplicated(db):
    async with db.transaction() as s:
        _, created1 = await enqueue(s, "email", "new_customer:abc", {"x": 1})
        _, created2 = await enqueue(s, "email", "new_customer:abc", {"x": 2})
    async with db.transaction() as s:
        _, created3 = await enqueue(s, "email", "new_customer:abc", {"x": 3})
    assert (created1, created2, created3) == (True, False, False)
    async with db.session() as s:
        jobs = (await s.execute(select(Job))).scalars().all()
    assert len(jobs) == 1 and jobs[0].payload == {"x": 1}


async def test_concurrent_claims_never_double_claim(db):
    async with db.transaction() as s:
        for i in range(10):
            await enqueue(s, "email", f"k{i}", {"i": i})
    a, b = JobQueue(db, "worker-a"), JobQueue(db, "worker-b")
    ids: list[int] = []
    for _ in range(10):  # racing claimers may each get fewer than asked; drain until empty
        results = await asyncio.gather(*[q.claim(["email"], limit=3) for q in (a, b, a, b)])
        batch = [job.id for claimed in results for job in claimed]
        if not batch:
            break
        ids += batch
    assert len(ids) == len(set(ids)) == 10


async def test_lease_ownership_protects_completion(db):
    async with db.transaction() as s:
        await enqueue(s, "email", "k", {})
    a = JobQueue(db, "worker-a", lease_seconds=0.01)
    (job,) = await a.claim()
    await asyncio.sleep(0.05)
    b = JobQueue(db, "worker-b")
    assert (await b.recover())["requeued"] == 1
    (job_b,) = await b.claim()
    assert not await a.succeed(job)  # stale owner cannot complete
    assert await b.succeed(job_b)


async def test_retry_with_backoff_and_limit(db):
    async with db.transaction() as s:
        await enqueue(s, "email", "k", {}, max_attempts=2)
    q = JobQueue(db, "w")
    (job,) = await q.claim()
    assert await q.fail(job, "timeout", retryable=True, backoff_seconds=60) == "pending"
    assert await q.claim() == []  # not yet available
    async with db.transaction() as s:
        row = (await s.execute(select(Job))).scalar_one()
        assert row.available_at > utcnow() + dt.timedelta(seconds=50)
        row.available_at = utcnow()
    (job,) = await q.claim()
    assert job.attempts == 2
    assert await q.fail(job, "timeout", retryable=True) == "failed"


async def test_recovery_marks_irreversible_phase_uncertain(db):
    async with db.transaction() as s:
        await enqueue(s, "email", "a", {})
        await enqueue(s, "email", "b", {})
    old = JobQueue(db, "desk:old-process")
    j1, j2 = await old.claim(limit=2)
    await old.set_phase(j1, "submitting")
    await old.set_phase(j2, "connecting")
    new = JobQueue(db, "desk:new-process")
    counts = await new.recover(include_own_owner_prefix="desk:")
    assert counts == {"requeued": 1, "uncertain": 1}
    async with db.session() as s:
        rows = {r.dedup_key: r for r in (await s.execute(select(Job))).scalars()}
    assert rows["a"].status == "uncertain" and rows["b"].status == "pending"


async def test_owner_actions_on_uncertain_jobs(db):
    async with db.transaction() as s:
        await enqueue(s, "email", "a", {})
    q = JobQueue(db, "w")
    (job,) = await q.claim()
    await q.mark_uncertain(job, "timeout during DATA")
    async with db.transaction() as s:
        assert await admin_retry(s, job.id, "owner")
    (again,) = await q.claim()
    await q.mark_uncertain(again, "timeout during DATA")
    async with db.transaction() as s:
        assert await admin_resolve(s, job.id, "succeeded")
        assert (await s.get(Job, job.id)).status == "succeeded"
