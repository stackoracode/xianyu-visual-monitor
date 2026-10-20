"""MySQL-specific behavior (runs only when XVM_TEST_MYSQL_URL is set, e.g. in CI)."""

import asyncio

import pytest
from sqlalchemy import select, text

from conftest import MYSQL_URL
from xianyu_monitor.models import Job
from xianyu_monitor.queueing.jobs import JobQueue, enqueue

pytestmark = [pytest.mark.mysql, pytest.mark.skipif(not MYSQL_URL, reason="XVM_TEST_MYSQL_URL not set")]


async def test_utf8mb4_roundtrip_of_source_language_text(db):
    async with db.transaction() as s:
        await enqueue(s, "email", "k", {"name": "买家😀", "preview": "还在吗？"})
    async with db.session() as s:
        job = (await s.execute(select(Job))).scalar_one()
        charset = (await s.execute(text("SELECT @@character_set_database"))).scalar_one()
    assert job.payload["name"] == "买家😀" and charset.startswith("utf8mb4")


async def test_outbox_under_parallel_workers_on_mysql(db):
    async with db.transaction() as s:
        for i in range(40):
            await enqueue(s, "email", f"k{i}", {"i": i})
    queues = [JobQueue(db, f"w{i}") for i in range(4)]
    batches = await asyncio.gather(*[q.claim(["email"], limit=15) for q in queues])
    ids = [job.id for batch in batches for job in batch]
    assert len(ids) == len(set(ids)) == 40


async def test_concurrent_enqueue_same_key_creates_one_job(db):
    async def add():
        async with db.transaction() as s:
            await enqueue(s, "email", "same", {})

    results = await asyncio.gather(*[add() for _ in range(5)], return_exceptions=True)
    assert all(not isinstance(r, Exception) for r in results), results
    async with db.session() as s:
        assert len((await s.execute(select(Job))).scalars().all()) == 1
