import datetime as dt
import os
import time

from sqlalchemy import func, select

from xianyu_monitor.models import Frame, Job, MessageEvent, ReviewItem
from xianyu_monitor.runtime_settings import RuntimeSettings
from xianyu_monitor.timeutil import utcnow
from xianyu_monitor.web.services import create_customer
from xianyu_monitor.worker.retention import prune_frame_files, run_retention


def make_frames(directory, n, size=1000):
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(n):
        p = directory / f"{i:04d}.png"
        p.write_bytes(b"\0" * size)
        os.utime(p, (time.time() - (n - i), time.time() - (n - i)))
        paths.append(p)
    return paths


def test_prune_by_count_keeps_newest_and_protected(tmp_path):
    paths = make_frames(tmp_path / "20261020", 10)
    deleted, _ = prune_frame_files(tmp_path, max_count=4, max_mb=100, protected={str(paths[0])})
    remaining = sorted(p.name for p in (tmp_path / "20261020").iterdir())
    assert deleted == 6 and "0000.png" in remaining and "0009.png" in remaining and len(remaining) == 4


def test_prune_by_size(tmp_path):
    make_frames(tmp_path / "d", 5, size=400_000)
    prune_frame_files(tmp_path, max_count=100, max_mb=1, protected=set())
    total = sum(p.stat().st_size for p in (tmp_path / "d").iterdir())
    assert total <= 1024 * 1024


async def test_database_retention_keeps_recent_and_email_history(db, tmp_path):
    old = utcnow() - dt.timedelta(days=400)
    async with db.transaction() as s:
        customer = await create_customer(s, "c", "known")
        s.add(Frame(captured_at=old, worker="w", status="ok"))
        s.add(Frame(captured_at=utcnow(), worker="w", status="ok"))
        s.add(MessageEvent(customer_id=customer.id, kind="incoming", dedup_key="ev:old", captured_at=old, created_at=old))
        s.add(Job(kind="email", dedup_key="new_customer:c", status="succeeded", payload={}, finished_at=old))
        s.add(Job(kind="reply", dedup_key="reply:1", status="succeeded", payload={}, finished_at=old))
        s.add(ReviewItem(kind="identity_uncertain", dedup_key="rv:x", observed_name="n"))
    report = await run_retention(db, tmp_path / "frames", RuntimeSettings())
    assert report.frame_rows == 1 and report.event_rows == 1 and report.job_rows == 1
    async with db.session() as s:
        kinds = (await s.execute(select(Job.kind))).scalars().all()
        assert kinds == ["email"]
        assert (await s.execute(select(func.count(Frame.id)))).scalar_one() == 1
    assert report.free_mb > 0
