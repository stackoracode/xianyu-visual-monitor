"""Retention cleanup and disk-space guard for frames, events and finished jobs."""

from __future__ import annotations

import datetime as dt
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select

from xianyu_monitor.db import Database
from xianyu_monitor.models import Frame, Job, JobAttempt, MessageEvent, ReplyIntent, ReviewItem
from xianyu_monitor.runtime_settings import RuntimeSettings
from xianyu_monitor.timeutil import utcnow

log = logging.getLogger(__name__)


@dataclass
class RetentionReport:
    files_deleted: int = 0
    bytes_deleted: int = 0
    frame_rows: int = 0
    event_rows: int = 0
    job_rows: int = 0
    free_mb: float = 0.0
    low_disk: bool = False


def free_mb(path: Path) -> float:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free / (1024 * 1024)


def prune_frame_files(frames_dir: Path, max_count: int, max_mb: int, protected: set[str]) -> tuple[int, int]:
    if not frames_dir.is_dir():
        return 0, 0
    files = sorted((p for p in frames_dir.rglob("*.png") if p.name != "latest.png"),
                   key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    limit_bytes = max_mb * 1024 * 1024
    deleted = deleted_bytes = 0
    remaining = len(files)
    for path in files:
        if remaining <= max_count and total <= limit_bytes:
            break
        if str(path) in protected:
            continue
        size = path.stat().st_size
        try:
            path.unlink()
        except OSError:
            continue
        deleted += 1
        deleted_bytes += size
        total -= size
        remaining -= 1
    for directory in sorted((d for d in frames_dir.iterdir() if d.is_dir()), reverse=True):
        try:
            directory.rmdir()  # only succeeds when empty
        except OSError:
            pass
    return deleted, deleted_bytes


async def run_retention(db: Database, frames_dir: Path, rt: RuntimeSettings) -> RetentionReport:
    report = RetentionReport()
    now = utcnow()
    async with db.session() as session:
        protected_rows = (await session.execute(
            select(Frame.image_path).join(ReviewItem, ReviewItem.frame_id == Frame.id)
            .where(ReviewItem.status == "open", Frame.image_path.is_not(None)))).scalars().all()
    protected = {p for p in protected_rows if p}
    report.files_deleted, report.bytes_deleted = prune_frame_files(
        frames_dir, int(rt.frames_max_count), int(rt.frames_max_mb), protected)

    async with db.transaction() as session:
        cutoff = now - dt.timedelta(days=int(rt.frame_rows_retention_days))
        res = await session.execute(delete(Frame).where(Frame.captured_at < cutoff))
        report.frame_rows = res.rowcount or 0
        cutoff = now - dt.timedelta(days=int(rt.events_retention_days))
        referenced = select(ReplyIntent.message_event_id)
        res = await session.execute(delete(MessageEvent).where(MessageEvent.created_at < cutoff,
                                                               MessageEvent.id.not_in(referenced)))
        report.event_rows = res.rowcount or 0
        cutoff = now - dt.timedelta(days=int(rt.jobs_retention_days))
        old_jobs = select(Job.id).where(Job.status.in_(["succeeded", "failed", "cancelled"]),
                                        Job.finished_at.is_not(None), Job.finished_at < cutoff,
                                        Job.kind != "email")  # email history kept: it backs dedup
        old_ids = list((await session.execute(old_jobs)).scalars().all())
        if old_ids:
            await session.execute(delete(JobAttempt).where(JobAttempt.job_id.in_(old_ids)))
            res = await session.execute(delete(Job).where(Job.id.in_(old_ids)))
            report.job_rows = res.rowcount or 0
    report.free_mb = free_mb(frames_dir)
    report.low_disk = report.free_mb < float(rt.min_free_disk_mb)
    if report.files_deleted or report.frame_rows or report.event_rows or report.job_rows:
        log.info("retention: %s", report)
    return report
