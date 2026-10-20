"""Capture-worker orchestration: recovery, single-worker lease, alerts, replies, shutdown."""

import asyncio

import pytest
from sqlalchemy import select

from synth import FakeDesktop, Harness, Scene, SBubble, SRow
from test_email import FakeSMTP, factory
from xianyu_monitor import state_keys
from xianyu_monitor.capture.backends import CallableCapture
from xianyu_monitor.db import get_state
from xianyu_monitor.models import CalibrationProfile, Customer, Job, KeywordRule, ReplyIntent
from xianyu_monitor.notify.emailer import EmailJobHandler
from xianyu_monitor.queueing.jobs import JobQueue, enqueue
from xianyu_monitor.runtime_settings import save_runtime
from xianyu_monitor.timeutil import utcnow
from xianyu_monitor.worker.service import MonitorService, WorkerAlreadyRunning


async def configure(db, h: Harness, **runtime):
    async with db.transaction() as s:
        s.add(CalibrationProfile(name="synthetic", data=h.profile_dict, active=True, created_by="test"))
        await save_runtime(s, {"full_validation_every_n_frames": 1000, "challenge_probe_every_n_frames": 1,
                               "capture_interval_seconds": 1.0, "reply_step_delay_seconds": 0.1,
                               "reply_verify_timeout_seconds": 1.0, "reply_global_min_interval_seconds": 0,
                               **runtime}, "test")


def make_service(settings, db, h, source, input_factory=None, behavior="ok"):
    return MonitorService(settings, db, CallableCapture(source), h.pool, input_factory=input_factory,
                          email_handler=EmailJobHandler(settings, factory(behavior)))


async def drain_jobs(service, kind):
    for job in await service.jobs.claim([kind], limit=10):
        await (service._run_email(job) if kind == "email" else service._run_reply(job))


async def test_baseline_then_new_customer_email_end_to_end(db, settings):
    h = Harness()
    await configure(db, h, monitor_mode="baseline")
    scenes = [Scene(rows=[SRow("老客户", "hello", "10-11", avatar=1)])]
    service = make_service(settings, db, h, lambda: h.render(scenes[-1]))
    await service.start(use_file_lock=False)
    await service.capture_once(service.rt)
    await service.handle_frame(await service.frames.get())
    async with db.transaction() as s:
        await save_runtime(s, {"monitor_mode": "active", "notifications_enabled": True, "dry_run": False}, "test")
    await service.runtime(max_age=0)
    scenes.append(Scene(rows=[SRow("新买家", "你好", "刚刚", badge=1, avatar=2), SRow("老客户", "hello", "10-11", avatar=1)]))
    await service.capture_once(service.rt)
    await service.handle_frame(await service.frames.get())
    FakeSMTP.instances.clear()
    await drain_jobs(service, "email")
    async with db.session() as s:
        job = (await s.execute(select(Job).where(Job.kind == "email"))).scalar_one()
        customer = (await s.execute(select(Customer).where(Customer.status == "new"))).scalar_one()
    assert job.status == "succeeded" and customer.notified_at is not None
    assert len(FakeSMTP.instances[-1].sent) == 1
    await service.publish_heartbeat()
    async with db.session() as s:
        heartbeat = await get_state(s, state_keys.HEARTBEAT)
    assert heartbeat["counters"]["frames"] == 2 and heartbeat["status"] == "ok"
    await service.release_lease()


async def test_restart_marks_interrupted_submission_uncertain_and_pauses_replies(db, settings):
    h = Harness()
    await configure(db, h)
    async with db.transaction() as s:
        await enqueue(s, "email", "new_customer:x", {"type": "new_customer"})
    old = JobQueue(db, f"{settings.worker_name}:host:1:old")
    (job,) = await old.claim()
    await old.set_phase(job, "submitting")
    service = make_service(settings, db, h, lambda: h.render(Scene()))
    await service.start(use_file_lock=False)
    async with db.session() as s:
        row = await s.get(Job, job.id)
        paused = await get_state(s, state_keys.REPLY_PAUSE)
    assert row.status == "uncertain"
    assert paused["paused"] and paused["reason"] == "recovered_uncertain_jobs"
    claimed = await service.jobs.claim(["email"], limit=10)
    assert job.id not in {c.id for c in claimed}  # only the alert job; the uncertain one is never retried
    await service.release_lease()


async def test_only_one_worker_per_desktop(db, settings):
    h = Harness()
    await configure(db, h)
    first = make_service(settings, db, h, lambda: h.render(Scene()))
    await first.start(use_file_lock=False)
    second = make_service(settings, db, h, lambda: h.render(Scene()))
    with pytest.raises(WorkerAlreadyRunning):
        await second.start(use_file_lock=False)
    await first.release_lease()
    await second.start(use_file_lock=False)
    await second.release_lease()


async def test_challenge_pauses_replies_and_alerts_once(db, settings):
    h = Harness()
    await configure(db, h, monitor_mode="active")
    scene = Scene(rows=[SRow("a", "b")], challenge="请完成安全验证")
    service = make_service(settings, db, h, lambda: h.render(scene))
    await service.start(use_file_lock=False)
    for _ in range(3):
        await service.capture_once(service.rt)
        await service.handle_frame(await service.frames.get())
    async with db.session() as s:
        paused = await get_state(s, state_keys.REPLY_PAUSE)
        challenge = await get_state(s, state_keys.CHALLENGE)
        alerts = (await s.execute(select(Job).where(Job.dedup_key.like("alert:challenge_detected:%")))).scalars().all()
    assert paused["paused"] and challenge["active"] and len(alerts) == 1
    scene.challenge = None
    await service.capture_once(service.rt)
    await service.handle_frame(await service.frames.get())
    async with db.session() as s:
        assert not (await get_state(s, state_keys.CHALLENGE))["active"]
        assert (await get_state(s, state_keys.REPLY_PAUSE))["paused"]  # owner must resume
    await service.release_lease()


async def test_capture_failures_pause_replies(db, settings):
    h = Harness()
    await configure(db, h, capture_failure_pause_after=2)
    service = make_service(settings, db, h, lambda: RuntimeError("no display"))
    await service.start(use_file_lock=False)
    for _ in range(2):
        await service.capture_once(service.rt)
    async with db.session() as s:
        health = await get_state(s, state_keys.HEALTH)
        paused = await get_state(s, state_keys.REPLY_PAUSE)
    assert health["status"] == "capture_failed" and paused["reason"] == "capture_failed"
    await service.release_lease()


async def test_automatic_keyword_reply_end_to_end_with_verification(db, settings):
    h = Harness()
    await configure(db, h, monitor_mode="active", reply_mode="auto", reply_send_enabled=True, dry_run=False)
    async with db.transaction() as s:
        s.add(KeywordRule(name="avail", enabled=True, priority=100, match_type="contains", match_mode="any",
                          patterns=["还在吗"], exclude_patterns=[], template="在的", cooldown_seconds=3600, scope="all",
                          allow_sensitive=False, requires_review=False, created_at=utcnow(), updated_at=utcnow()))
    rows = [SRow("老客户", "hello", "10-11", avatar=1)]
    desk = FakeDesktop(h, rows, {"老客户": [SBubble("incoming", "hello")]})
    service = make_service(settings, db, h, desk.capture, input_factory=lambda: desk)
    await service.start(use_file_lock=False)
    await service.capture_once(service.rt)
    await service.handle_frame(await service.frames.get())  # first sight: state only
    rows[0].badge, rows[0].preview, rows[0].time = 1, "还在吗", "刚刚"
    desk.history["老客户"].append(SBubble("incoming", "还在吗"))
    await service.capture_once(service.rt)
    await service.handle_frame(await service.frames.get())
    await drain_jobs(service, "reply")
    async with db.session() as s:
        intent = (await s.execute(select(ReplyIntent))).scalar_one()
        job = (await s.execute(select(Job).where(Job.kind == "reply"))).scalar_one()
    assert intent.status == "sent_verified" and job.status == "succeeded" and intent.sent_at is not None
    assert ("type", "在的") in desk.actions
    # the monitor then sees its own outgoing message: no new intent
    await service.capture_once(service.rt)
    await service.handle_frame(await service.frames.get())
    async with db.session() as s:
        assert len((await s.execute(select(ReplyIntent))).scalars().all()) == 1
    await service.release_lease()


async def test_reply_returns_to_review_when_paused_at_send_time(db, settings):
    h = Harness()
    await configure(db, h, monitor_mode="active", reply_mode="auto", reply_send_enabled=True, dry_run=False)
    async with db.transaction() as s:
        s.add(KeywordRule(name="avail", enabled=True, priority=100, match_type="contains", match_mode="any",
                          patterns=["还在吗"], exclude_patterns=[], template="在的", cooldown_seconds=3600, scope="all",
                          allow_sensitive=False, requires_review=False, created_at=utcnow(), updated_at=utcnow()))
    rows = [SRow("老客户", "hello", "10-11", avatar=1)]
    desk = FakeDesktop(h, rows, {"老客户": [SBubble("incoming", "hello")]})
    service = make_service(settings, db, h, desk.capture, input_factory=lambda: desk)
    await service.start(use_file_lock=False)
    await service.capture_once(service.rt)
    await service.handle_frame(await service.frames.get())
    rows[0].badge, rows[0].preview = 1, "还在吗"
    await service.capture_once(service.rt)
    await service.handle_frame(await service.frames.get())
    await service.pause_replies("manual_test")
    await drain_jobs(service, "reply")
    async with db.session() as s:
        intent = (await s.execute(select(ReplyIntent))).scalar_one()
    assert intent.status == "review" and desk.actions == []
    await service.release_lease()


async def test_run_and_graceful_shutdown(db, settings):
    h = Harness()
    await configure(db, h)
    service = make_service(settings, db, h, lambda: h.render(Scene(rows=[SRow("a", "b")])))
    task = asyncio.create_task(service.run())
    await asyncio.sleep(1.5)
    service.request_stop()
    await asyncio.wait_for(task, 20)
    assert service.counters["frames"] >= 1
    async with db.session() as s:
        heartbeat = await get_state(s, state_keys.HEARTBEAT)
    assert heartbeat["status"] == "stopped"
    other = make_service(settings, db, h, lambda: h.render(Scene()))
    await other.start()  # lease and file lock were released
    await other.release_lease()
    if other._lock:
        other._lock.release()
