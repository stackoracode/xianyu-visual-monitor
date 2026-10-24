"""The capture worker: one process per desktop, separate from the HTTP workers.

Tasks (asyncio):

* capture loop     - takes a screenshot every ``capture_interval_seconds``; frames go through a
                     small bounded queue with *block* backpressure (frames are never dropped
                     silently; capture is delayed instead and the lag is reported).
* analysis loop    - analyzes frames (OCR in a process pool) and persists results.
* dispatcher       - claims due jobs from MySQL into bounded per-kind queues.
* email/reply runners - execute jobs; replies hold the desktop lock.
* heartbeat        - renews the single-worker lease and publishes health.
* retention        - periodic cleanup and disk guard.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import fcntl
import logging
import os
import socket
import statistics
import time
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path

from PIL import Image
from sqlalchemy import select

from xianyu_monitor import state_keys
from xianyu_monitor.capture.backends import CaptureBackend, CapturedFrame, CaptureError
from xianyu_monitor.db import Database, get_state, set_state
from xianyu_monitor.models import (
    CalibrationProfile,
    Customer,
    CustomerAlias,
    CustomerAvatar,
    Job,
    ReplyIntent,
    WorkerLease,
)
from xianyu_monitor.notify.emailer import EmailJobHandler
from xianyu_monitor.ocr.pool import OcrPool
from xianyu_monitor.queueing.bounded import BoundedQueue
from xianyu_monitor.queueing.jobs import ClaimedJob, JobQueue, enqueue
from xianyu_monitor.replies.executor import ReplyExecutor, ReplyTarget
from xianyu_monitor.replies.images import ImageOutcome, ImageReplyExecutor
from xianyu_monitor.replies.input import InputAdapter
from xianyu_monitor.runtime_settings import RuntimeSettings, load_runtime
from xianyu_monitor.settings import Settings
from xianyu_monitor.timeutil import iso, utcnow
from xianyu_monitor.vision.analyzer import FrameAnalyzer, FrameObservation
from xianyu_monitor.vision.profile import Profile, load_profile, profile_from_dict
from xianyu_monitor.worker.pipeline import FrameProcessor
from xianyu_monitor.worker.retention import run_retention

log = logging.getLogger(__name__)


class WorkerAlreadyRunning(RuntimeError):
    pass


class FileLock:
    """Host-local single-instance guard (one capture worker per desktop name)."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")  # noqa: SIM115 - kept open for the lifetime of the lock
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise WorkerAlreadyRunning(f"another worker holds {self.path}") from exc
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None


async def load_active_profile(db: Database, settings: Settings) -> tuple[Profile | None, str]:
    async with db.session() as session:
        row = (await session.execute(select(CalibrationProfile).where(CalibrationProfile.active.is_(True))
                                     .order_by(CalibrationProfile.id.desc()).limit(1))).scalar_one_or_none()
    if row is not None:
        try:
            return profile_from_dict(row.data), f"db:{row.id}"
        except ValueError as exc:
            log.error("active calibration profile %s is invalid: %s", row.id, exc)
            return None, f"db:{row.id}:invalid"
    if settings.calibration_file and settings.calibration_file.is_file():
        return load_profile(settings.calibration_file), f"file:{settings.calibration_file.stat().st_mtime_ns}"
    return None, "none"


class MonitorService:
    FRAME_QUEUE_SIZE = 2
    JOB_QUEUE_SIZE = 4

    def __init__(self, settings: Settings, db: Database, capture: CaptureBackend, ocr: OcrPool,
                 input_factory: Callable[[], InputAdapter] | None = None,
                 email_handler: EmailJobHandler | None = None, sleep: Callable[[float], object] = asyncio.sleep):
        self.settings = settings
        self.db = db
        self.capture_backend = capture
        self.ocr = ocr
        self.input_factory = input_factory
        self.sleep = sleep
        started = utcnow().strftime("%Y%m%dT%H%M%S")
        self.owner = f"{settings.worker_name}:{socket.gethostname()}:{os.getpid()}:{started}:{uuid.uuid4().hex[:8]}"
        self.jobs = JobQueue(db, self.owner)
        self.processor = FrameProcessor(db, settings.worker_name)
        self.email_handler = email_handler or EmailJobHandler(settings, on_success=self._email_succeeded)
        if self.email_handler.on_success is None:
            self.email_handler.on_success = self._email_succeeded
        self.frames: BoundedQueue[CapturedFrame] = BoundedQueue("frames", self.FRAME_QUEUE_SIZE, "block")
        self.email_jobs: BoundedQueue[ClaimedJob] = BoundedQueue("email_jobs", self.JOB_QUEUE_SIZE, "block")
        self.reply_jobs: BoundedQueue[ClaimedJob] = BoundedQueue("reply_jobs", 1, "block")
        self.desktop_lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.stopping = asyncio.Event()
        self.analyzer: FrameAnalyzer | None = None
        self.profile_source = "none"
        self.rt = RuntimeSettings()
        self._rt_loaded_at = 0.0
        self.counters = {"frames": 0, "unchanged": 0, "ocr_frames": 0, "capture_failures": 0,
                         "consecutive_capture_failures": 0, "consecutive_layout_failures": 0, "events": 0}
        self.last_frame_at: dt.datetime | None = None
        self.last_status = "starting"
        self.frame_watermark: dt.datetime | None = None
        self._last_idle_return_check = 0.0
        self.latencies: deque[float] = deque(maxlen=200)
        self.ocr_ms: deque[float] = deque(maxlen=200)
        self.low_disk = False
        self._lock: FileLock | None = None
        self._tasks: list[asyncio.Task] = []

    # ----- runtime settings / calibration -----
    async def runtime(self, max_age: float = 5.0) -> RuntimeSettings:
        if time.monotonic() - self._rt_loaded_at > max_age:
            async with self.db.session() as session:
                self.rt = await load_runtime(session)
            self._rt_loaded_at = time.monotonic()
        return self.rt

    async def refresh_profile(self) -> None:
        profile, source = await load_active_profile(self.db, self.settings)
        if source == self.profile_source:
            return
        self.profile_source = source
        if profile is None:
            self.analyzer = None
            log.warning("no valid calibration profile (%s); monitoring idle", source)
            return
        if self.analyzer is None:
            self.analyzer = FrameAnalyzer(profile, self.ocr)
        else:
            self.analyzer.reset(profile)
        log.info("calibration profile loaded from %s (%sx%s)", source, profile.frame_width, profile.frame_height)

    # ----- lifecycle -----
    async def acquire_lease(self) -> None:
        now = utcnow()
        async with self.db.transaction() as session:
            lease = await session.get(WorkerLease, self.settings.worker_name, with_for_update=self.db.is_mysql)
            if lease is not None and lease.expires_at > now and lease.owner != self.owner:
                raise WorkerAlreadyRunning(f"worker lease {self.settings.worker_name} held by {lease.owner}")
            if lease is None:
                session.add(WorkerLease(name=self.settings.worker_name, owner=self.owner, heartbeat_at=now,
                                        expires_at=now + dt.timedelta(seconds=60)))
            else:
                lease.owner, lease.heartbeat_at = self.owner, now
                lease.expires_at = now + dt.timedelta(seconds=60)

    async def renew_lease(self) -> bool:
        now = utcnow()
        async with self.db.transaction() as session:
            lease = await session.get(WorkerLease, self.settings.worker_name)
            if lease is None or lease.owner != self.owner:
                return False
            lease.heartbeat_at = now
            lease.expires_at = now + dt.timedelta(seconds=60)
        return True

    async def release_lease(self) -> None:
        async with self.db.transaction() as session:
            lease = await session.get(WorkerLease, self.settings.worker_name)
            if lease is not None and lease.owner == self.owner:
                await session.delete(lease)

    async def start(self, use_file_lock: bool = True) -> None:
        if use_file_lock:
            self._lock = FileLock(self.settings.run_dir / f"worker-{self.settings.worker_name}.lock")
            self._lock.acquire()
        await self.acquire_lease()
        recovered = await self.jobs.recover(include_own_owner_prefix=self.settings.worker_name + ":")
        await self._recover_reply_intents()
        if recovered["uncertain"]:
            await self.pause_replies("recovered_uncertain_jobs", f"{recovered['uncertain']} job(s) uncertain after restart")
        await self.refresh_profile()
        await self.runtime(max_age=0)
        log.info("worker %s started (recovered=%s)", self.owner, recovered)

    async def _recover_reply_intents(self) -> None:
        """Intents left in ``sending`` follow their job: requeued jobs (interrupted before any
        keystroke) send again later; uncertain jobs (interrupted while typing/verifying) are
        ``sent_unverified`` and never retried automatically."""
        async with self.db.transaction() as session:
            rows = (await session.execute(select(ReplyIntent).where(ReplyIntent.status == "sending"))).scalars().all()
            for intent in rows:
                job = await session.get(Job, intent.job_id) if intent.job_id else None
                if job is not None and job.status == "pending":
                    intent.status = "queued"
                else:
                    intent.status = "sent_unverified"
                    intent.reasons = list(intent.reasons or []) + ["worker_restarted_during_send"]
                intent.updated_at = utcnow()
            images = (await session.execute(select(ReplyIntent).where(
                ReplyIntent.send_image.is_(True), ReplyIntent.image_status.in_(["pending", "preparing"]),
                ReplyIntent.status == "sent_verified"
            ))).scalars().all()
            for intent in images:
                job = await session.get(Job, intent.job_id) if intent.job_id else None
                if job is not None and job.status == "uncertain":
                    intent.image_status = "uncertain"
                    intent.verification = {**(intent.verification or {}),
                                           "image": {"status": "uncertain", "reason": "worker_restarted_during_image"}}

    def request_stop(self) -> None:
        log.info("stop requested")
        self.stopping.set()
        self.wake.set()

    async def run(self, use_file_lock: bool = True) -> None:
        await self.start(use_file_lock)
        self._tasks = [
            asyncio.create_task(self.capture_loop(), name="capture"),
            asyncio.create_task(self.analysis_loop(), name="analysis"),
            asyncio.create_task(self.dispatcher_loop(), name="dispatcher"),
            asyncio.create_task(self.job_runner(self.email_jobs, self._run_email), name="email-runner"),
            asyncio.create_task(self.job_runner(self.reply_jobs, self._run_reply), name="reply-runner"),
            asyncio.create_task(self.heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self.retention_loop(), name="retention"),
        ]
        try:
            await self.stopping.wait()
        finally:
            await self.shutdown()

    async def shutdown(self, timeout: float = 30.0) -> None:
        self.stopping.set()
        by_name = {t.get_name(): t for t in self._tasks}
        for name in ("capture", "dispatcher", "retention", "heartbeat"):
            task = by_name.get(name)
            if task is not None:
                task.cancel()
        # Let in-flight frames and jobs finish (bounded), then cancel the rest.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.gather(self.frames.join(), self.email_jobs.join(),
                                                  self.reply_jobs.join()), timeout=timeout)
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await self.publish_heartbeat(status="stopped")
        with contextlib.suppress(Exception):
            await self.release_lease()
        self.ocr.close()
        if self._lock is not None:
            self._lock.release()
        log.info("worker stopped")

    # ----- pause / alerts -----
    async def pause_replies(self, reason: str, detail: str = "", alert: bool = True) -> None:
        async with self.db.transaction() as session:
            current = await get_state(session, state_keys.REPLY_PAUSE)
            if current.get("paused") and current.get("reason") == reason:
                return
            await set_state(session, state_keys.REPLY_PAUSE, {"paused": True, "reason": reason, "detail": detail,
                                                              "by": "worker", "at": iso(utcnow())})
        log.warning("automatic replies paused: %s %s", reason, detail)
        if alert:
            await self.raise_alert(reason, detail)

    async def raise_alert(self, reason: str, detail: str, episode: str | None = None) -> None:
        rt = await self.runtime()
        if not rt.alert_emails_enabled:
            return
        episode = episode or utcnow().strftime("%Y%m%dT%H%M")
        async with self.db.transaction() as session:
            await enqueue(session, "email", f"alert:{reason}:{episode}",
                          {"type": "alert", "reason": reason, "detail": detail[:500], "observed_at": iso(utcnow())},
                          priority=8, max_attempts=int(rt.email_max_attempts))
        self.wake.set()

    async def _set_health(self, status: str, **extra) -> None:
        async with self.db.transaction() as session:
            current = await get_state(session, state_keys.HEALTH)
            previous = current.get("status")
            current.update({"status": status, "updated_at": iso(utcnow()), **extra})
            if previous != status:
                current["since"] = iso(utcnow())
            await set_state(session, state_keys.HEALTH, current)

    # ----- capture & analysis -----
    async def _monitor_paused(self) -> bool:
        async with self.db.session() as session:
            return bool((await get_state(session, state_keys.MONITOR_PAUSE)).get("paused"))

    async def capture_loop(self) -> None:
        while not self.stopping.is_set():
            started = time.monotonic()
            rt = await self.runtime()
            try:
                if await self._monitor_paused():
                    self.last_status = "paused"
                    await self._set_health("paused")
                else:
                    await self.capture_once(rt)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the loop alive; health reports the failure
                log.exception("capture loop iteration failed")
            elapsed = time.monotonic() - started
            await self.sleep(max(0.2, float(rt.capture_interval_seconds) - elapsed))

    async def capture_once(self, rt: RuntimeSettings) -> None:
        try:
            async with self.desktop_lock:
                frame = await self.capture_backend.capture()
        except (CaptureError, OSError) as exc:
            self.counters["capture_failures"] += 1
            self.counters["consecutive_capture_failures"] += 1
            self.last_status = "capture_failed"
            await self._set_health("capture_failed", error=str(exc)[:300])
            if self.counters["consecutive_capture_failures"] == int(rt.capture_failure_pause_after):
                await self.pause_replies("capture_failed", str(exc)[:300])
            return
        self.counters["consecutive_capture_failures"] = 0
        await self.frames.put(frame)

    async def analysis_loop(self) -> None:
        while True:
            frame = await self.frames.get()
            try:
                await self.handle_frame(frame)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("frame analysis failed")
                with contextlib.suppress(Exception):
                    await self._set_health("analysis_error")
            finally:
                self.frames.task_done()

    async def handle_frame(self, frame: CapturedFrame) -> FrameObservation | None:
        async with self.desktop_lock:
            if await self._maintain_page(frame):
                return None
            obs = await self._handle_frame_locked(frame)
            await self._return_idle_to_list(obs)
            await self._maintain_page(frame, periodic=True)
            return obs

    async def _maintain_page(self, frame: CapturedFrame, periodic: bool = False) -> bool:
        from xianyu_monitor.worker.page_maintenance import PageMaintenance
        rt = await self.runtime()
        if (rt.dry_run or rt.monitor_mode != "active" or self.input_factory is None
                or not (rt.page_recovery_enabled or rt.page_refresh_enabled)
                or await self._monitor_paused()):
            return False
        if not periodic and self.frame_watermark is not None and frame.captured_at <= self.frame_watermark:
            return False
        await self.refresh_profile()
        if self.analyzer is None or self.analyzer.profile.page_recovery is None:
            return False
        async with self.db.session() as session:
            state = await get_state(session, "page_maintenance")
            pause = await get_state(session, state_keys.REPLY_PAUSE)
            if pause.get("paused") and (pause.get("reason") != "page_recovery" or pause.get("by") != "worker"):
                return False  # never clear manual, challenge, draft or uncertain-send pauses
            busy = (await session.execute(select(Job.id).where(
                Job.kind == "reply", Job.status.in_(["pending", "leased"])).limit(1))).scalar_one_or_none()
            if busy is not None:
                return bool(state.get("active"))

        last_phase = state.get("phase")

        async def persist(data):
            nonlocal last_phase
            if data.get("phase") != last_phase:
                log.info("page maintenance: %s (active=%s, blocked=%s)", data.get("phase"),
                         bool(data.get("active")), bool(data.get("blocked")))
                last_phase = data.get("phase")
            async with self.db.transaction() as session:
                await set_state(session, "page_maintenance", data)
                current = await get_state(session, state_keys.REPLY_PAUSE)
                if data.get("active") and not current.get("paused"):
                    await set_state(session, state_keys.REPLY_PAUSE,
                                    {"paused": True, "reason": "page_recovery", "by": "worker", "at": iso(utcnow())})
                elif (not data.get("active") and current.get("reason") == "page_recovery"
                      and current.get("by") == "worker"):
                    await set_state(session, state_keys.REPLY_PAUSE,
                                    {"paused": False, "reason": None, "by": "worker", "at": iso(utcnow())})

        async def online():
            async def check(host, port):
                try:
                    _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
                    writer.close()
                    return True
                except (OSError, asyncio.TimeoutError):
                    return False
            # SMTP reachability is independent of the browser recovery path.
            return await check("www.goofish.com", 443)

        async def capture():
            return (await self.capture_backend.capture()).image

        async def guard():
            current_rt = await self.runtime(max_age=0)
            if (current_rt.dry_run or current_rt.monitor_mode != "active" or await self._monitor_paused()
                    or (rt.page_recovery_enabled and not current_rt.page_recovery_enabled)
                    or (rt.page_refresh_enabled and not current_rt.page_refresh_enabled)
                    or (rt.page_quick_enter_enabled and not current_rt.page_quick_enter_enabled)):
                return False
            async with self.db.session() as session:
                current = await get_state(session, state_keys.REPLY_PAUSE)
                return not current.get("paused") or (current.get("reason") == "page_recovery"
                                                     and current.get("by") == "worker")

        maintenance = PageMaintenance(self.analyzer, capture, self.input_factory(), online, persist, guard=guard)
        handled = await maintenance.tick(rt, state, periodic=periodic)
        if handled:
            self.last_frame_at = utcnow()
            self.last_status = "page_recovery" if state.get("active") else "ok"
            self.frame_watermark = utcnow()
            self._save_latest(await capture())
            if state.get("active"):
                await self._set_health("page_recovery", phase=state.get("phase"))
        return handled

    async def _return_idle_to_list(self, obs: FrameObservation | None) -> None:
        """Called only by monitoring, under the desktop lock, never mid-send."""
        rt = await self.runtime()
        if (obs is None or not obs.usable or self.analyzer is None or self.input_factory is None
                or rt.monitor_mode != "active" or rt.dry_run
                or time.monotonic() - self._last_idle_return_check < 10):
            return
        rows = [r for r in obs.rows if r.present and r.selected and r.name_conf >= float(rt.ocr_min_confidence)]
        if len(rows) != 1:
            return
        from xianyu_monitor.detection.textnorm import normalize_name
        async with self.db.session() as session:
            pause = await get_state(session, state_keys.REPLY_PAUSE)
            if pause.get("paused") and pause.get("reason") in {"idle_return_not_verified", "reply_return_not_verified"}:
                return  # one failed refresh needs attention, not an endless F5 loop
            busy = (await session.execute(select(Job.id).where(
                Job.kind == "reply", Job.status.in_(["pending", "leased"])).limit(1))).scalar_one_or_none()
            if busy is not None:
                return
            customer_ids = list((await session.execute(select(CustomerAlias.customer_id).where(
                CustomerAlias.alias_norm == normalize_name(rows[0].name)))).scalars())
            if len(customer_ids) != 1:
                return
            aliases = set((await session.execute(select(CustomerAlias.alias_norm).where(
                CustomerAlias.customer_id == customer_ids[0]))).scalars())
        self._last_idle_return_check = time.monotonic()
        async def capture():
            return (await self.capture_backend.capture()).image
        async def before_leave(frame, observation):
            path = self._save_frame(frame, observation.captured_at, rt, observation)
            result = await self.processor.process(observation, rt, image_path=path)
            self.counters["events"] += len(result.events)
            if result.jobs or result.intents:
                self.wake.set()
        target = ReplyTarget(customer_ids[0], aliases, set(), "", "")
        returned = await ReplyExecutor(self.analyzer, capture, self.input_factory()).return_to_list(
            target, rt, before_leave=before_leave)
        async with self.db.transaction() as session:
            await set_state(session, "idle_return_to_list", {**returned, "at": iso(utcnow())})
        if returned.get("returned"):
            self.frame_watermark = utcnow()
            await self._handle_frame_locked(await self.capture_backend.capture(), force_full=True)
        elif returned.get("pause_replies"):
            await self.pause_replies("idle_return_not_verified", returned.get("reason", ""))

    async def _handle_frame_locked(self, frame: CapturedFrame, force_full: bool = False) -> FrameObservation | None:
        if self.frame_watermark is not None and frame.captured_at <= self.frame_watermark:
            obs = FrameObservation(frame.captured_at, frame.image.width, frame.image.height, "superseded",
                                   layout_issues=["queued_before_newer_observation_or_desktop_action"])
            self.counters["superseded_frames"] = self.counters.get("superseded_frames", 0) + 1
            await self.processor.process(obs, await self.runtime(), capture_ms=frame.capture_ms)
            return obs
        self.frame_watermark = frame.captured_at
        await self.refresh_profile()
        rt = await self.runtime()
        self.counters["frames"] += 1
        self.last_frame_at = frame.captured_at  # type: ignore[assignment]
        if self.analyzer is None:
            self.last_status = "not_calibrated"
            await self._set_health("not_calibrated")
            self._save_latest(frame.image)
            return None
        obs = await self.analyzer.analyze(frame.image, frame.captured_at, rt, force_full=force_full)
        pane_direction = None
        if obs.status == "ok" and obs.changed and any(r.selected for r in obs.rows) \
                and self.analyzer.profile.message_pane is not None:
            obs.pane = await self.analyzer.analyze_pane(frame.image)
            # Pipeline verifies the pane title against the selected customer's aliases.
        self.last_status = obs.status
        if obs.ocr_performed:
            self.counters["ocr_frames"] += 1
            self.ocr_ms.append(obs.ocr_ms)
        if not obs.changed and obs.status == "unchanged":
            self.counters["unchanged"] += 1
            return obs
        image_path = self._save_frame(frame.image, frame.captured_at, rt, obs)  # type: ignore[arg-type]
        if obs.status == "challenge":
            await self._enter_challenge(obs)
        elif obs.status in {"layout_mismatch", "blank"}:
            self.counters["consecutive_layout_failures"] += 1
            await self._set_health(obs.status, issues=obs.layout_issues[:10])
            if self.counters["consecutive_layout_failures"] == int(rt.layout_failure_pause_after):
                await self.pause_replies("layout_mismatch", ", ".join(obs.layout_issues[:5]))
        else:
            self.counters["consecutive_layout_failures"] = 0
            await self._leave_challenge()
        record = obs.status == "ok" or self.counters["consecutive_layout_failures"] <= 1 \
            or self.counters["consecutive_layout_failures"] % 20 == 0 or obs.status == "challenge"
        if record:
            result = await self.processor.process(obs, rt, capture_ms=frame.capture_ms, image_path=image_path,
                                                  pane_direction=pane_direction)
            self.counters["events"] += len(result.events)
            if result.jobs or result.intents:
                self.wake.set()
            if result.events:
                self.latencies.append((utcnow() - frame.captured_at).total_seconds() * 1000.0)  # type: ignore[operator]
        return obs

    async def _enter_challenge(self, obs: FrameObservation) -> None:
        async with self.db.transaction() as session:
            current = await get_state(session, state_keys.CHALLENGE)
            if current.get("active"):
                return
            episode = utcnow().strftime("%Y%m%dT%H%M%S")
            await set_state(session, state_keys.CHALLENGE, {"active": True, "since": iso(utcnow()),
                                                            "episode": episode, "hits": obs.challenge_hits})
        await self._set_health("challenge", hits=obs.challenge_hits)
        await self.pause_replies("challenge_detected", "login/verification prompt visible; owner action required",
                                 alert=False)
        await self.raise_alert("challenge_detected", ", ".join(obs.challenge_hits), episode)

    async def _leave_challenge(self) -> None:
        async with self.db.transaction() as session:
            current = await get_state(session, state_keys.CHALLENGE)
            if current.get("active"):
                await set_state(session, state_keys.CHALLENGE, {**current, "active": False, "cleared_at": iso(utcnow())})
                log.info("challenge indicators cleared; automatic replies stay paused until the owner resumes")

    def _save_latest(self, image: Image.Image) -> None:
        if self.low_disk:
            return
        target = self.settings.frames_dir / "latest.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        image.save(tmp, format="PNG")
        os.replace(tmp, target)

    def _save_frame(self, image: Image.Image, at: dt.datetime, rt: RuntimeSettings,
                    obs: FrameObservation) -> str | None:
        try:
            self._save_latest(image)
            if self.low_disk or rt.save_frames == "none":
                return None
            if rt.save_frames == "changes" and not (obs.changed or obs.status != "ok"):
                return None
            day_dir = self.settings.frames_dir / at.strftime("%Y%m%d")
            day_dir.mkdir(parents=True, exist_ok=True)
            path = day_dir / (at.strftime("%H%M%S_%f") + f"_{obs.status}.png")
            image.save(path, format="PNG")
            return str(path)
        except OSError as exc:
            log.warning("could not save diagnostic frame: %s", exc)
            return None

    # ----- jobs -----
    async def dispatcher_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                for kind, queue in (("email", self.email_jobs), ("reply", self.reply_jobs)):
                    free = queue.free()
                    if free <= 0:
                        continue
                    for job in await self.jobs.claim([kind], limit=free):
                        await queue.put(job)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("dispatcher iteration failed")
            self.wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.wake.wait(), timeout=3.0)

    async def job_runner(self, queue: BoundedQueue[ClaimedJob], handler) -> None:
        while True:
            job = await queue.get()
            try:
                await handler(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("job %s (%s) crashed", job.id, job.kind)
                with contextlib.suppress(Exception):
                    if job.kind == "reply":
                        await self.jobs.mark_uncertain(job, f"handler crashed: {type(exc).__name__}")
                        await self.pause_replies("reply_handler_crashed", type(exc).__name__)
                    else:
                        await self.jobs.fail(job, f"handler crashed: {type(exc).__name__}", retryable=True)
            finally:
                queue.task_done()

    async def _run_email(self, job: ClaimedJob) -> str:
        rt = await self.runtime()
        return await self.email_handler.handle(job, self.jobs, rt)

    async def _email_succeeded(self, job: ClaimedJob) -> None:
        if job.payload.get("type") != "new_customer":
            return
        async with self.db.transaction() as session:
            customer = await session.get(Customer, job.payload.get("customer_id"))
            if customer is not None and customer.notified_at is None:
                customer.notified_at = utcnow()

    async def _reply_target(self, intent: ReplyIntent) -> ReplyTarget:
        async with self.db.session() as session:
            aliases = (await session.execute(select(CustomerAlias.alias_norm)
                                             .where(CustomerAlias.customer_id == intent.customer_id))).scalars().all()
            avatars = (await session.execute(select(CustomerAvatar.avatar_hash)
                                             .where(CustomerAvatar.customer_id == intent.customer_id))).scalars().all()
        return ReplyTarget(intent.customer_id, set(aliases), set(avatars), intent.trigger_text, intent.reply_text)

    async def _update_intent(self, intent_id: int, status: str, reason: str | None = None,
                             verification: dict | None = None, sent: bool = False) -> None:
        async with self.db.transaction() as session:
            intent = await session.get(ReplyIntent, intent_id)
            if intent is None or intent.status == "superseded":
                return
            intent.status = status
            if reason:
                intent.reasons = list(intent.reasons or []) + [reason]
            if verification is not None:
                intent.verification = verification
            if sent:
                intent.sent_at = utcnow()
            intent.updated_at = utcnow()

    async def _run_reply(self, job: ClaimedJob) -> str:
        rt = await self.runtime(max_age=0)
        intent_id = int(job.payload.get("intent_id", 0))
        async with self.db.session() as session:
            intent = await session.get(ReplyIntent, intent_id)
            paused = (await get_state(session, state_keys.REPLY_PAUSE)).get("paused", False)
            challenge = (await get_state(session, state_keys.CHALLENGE)).get("active", False)
            last_sent = (await session.execute(select(ReplyIntent.sent_at).where(ReplyIntent.sent_at.is_not(None))
                                               .order_by(ReplyIntent.sent_at.desc()).limit(1))).scalar_one_or_none()
        if intent is not None and intent.status == "superseded":
            return "superseded"
        if intent is None or intent.status != "queued":
            await self.jobs.succeed(job, {"skipped": intent.status if intent else "missing"})
            return "skipped"
        if intent.first_contact:
            async with self.db.session() as session:
                prior = (await session.execute(select(ReplyIntent.id).where(
                    ReplyIntent.customer_id == intent.customer_id, ReplyIntent.first_contact.is_(True),
                    ReplyIntent.id != intent.id,
                    ReplyIntent.status.in_(["sent_verified", "sent_unverified", "sending"])
                ).limit(1))).scalar_one_or_none()
            if prior is not None:
                await self._update_intent(intent_id, "superseded", "first_contact_already_replied")
                await self.jobs.succeed(job, {"skipped": "first_contact_already_replied"})
                return "superseded"
        if paused or challenge or rt.reply_mode == "off" or not rt.reply_send_enabled:
            await self._update_intent(intent_id, "review", "replies_paused_or_disabled_at_send_time")
            await self.jobs.succeed(job, {"returned_to_review": True})
            return "review"
        if self.analyzer is None or self.input_factory is None:
            await self._update_intent(intent_id, "review", "desktop_not_ready")
            await self.jobs.succeed(job, {"returned_to_review": True})
            return "review"
        interval = int(rt.reply_global_min_interval_seconds)
        if last_sent is not None and (utcnow() - last_sent).total_seconds() < interval and not rt.dry_run:
            await self.jobs.fail(job, "global reply interval not elapsed", retryable=True, backoff_seconds=interval)
            return "deferred"
        target = await self._reply_target(intent)
        if not rt.dry_run:
            await self._update_intent(intent_id, "sending")

        async def capture_image() -> Image.Image:
            return (await self.capture_backend.capture()).image

        executor = ReplyExecutor(self.analyzer, capture_image, self.input_factory(), sleep=self.sleep)  # type: ignore[arg-type]
        async with self.desktop_lock:
            outcome = await executor.execute(target, rt, lambda phase: self.jobs.set_phase(job, phase),
                                             dry_run=bool(rt.dry_run))
            with_image = bool(intent.send_image and outcome.status == "sent_verified")
            details = await self._finish_reply(job, intent_id, outcome, complete_job=not with_image)
            if with_image:
                async def remember_asset(digest):
                    async with self.db.transaction() as session:
                        row = await session.get(ReplyIntent, intent_id)
                        row.image_sha256, row.image_status = digest, "preparing"

                async def observe_image_frame(frame, obs):
                    if any(r.selected for r in obs.rows):
                        obs.pane = await self.analyzer.analyze_pane(frame)
                    result = await self.processor.process(obs, rt, image_path=self._save_frame(frame, obs.captured_at, rt, obs))
                    self.counters["events"] += len(result.events)
                    if result.jobs or result.intents:
                        self.wake.set()

                try:
                    if not rt.reply_image_send_enabled and not job.payload.get("image_test_authorized", False):
                        image = ImageOutcome("cancelled", "image_sending_disabled")
                    else:
                        image = await ImageReplyExecutor(executor).execute(
                            target, intent.image_path, rt, lambda phase: self.jobs.set_phase(job, phase),
                            remember_asset, intent.image_sha256, on_observation=observe_image_frame)
                except Exception:
                    log.exception("image reply failed for intent %s", intent_id)
                    image = ImageOutcome("uncertain", "image_handler_crashed", pause_replies=True)
                details["image"] = {"status": image.status, "reason": image.reason,
                                    "submitted": image.submitted, **image.details}
                log.info("image result for intent %s: %s (%s), selection committed=%s, checks=%s",
                         intent_id, image.status, image.reason.replace("_", " "), image.submitted,
                         image.details.get("verification_checks", 0))
                async with self.db.transaction() as session:
                    row = await session.get(ReplyIntent, intent_id)
                    row.image_status, row.verification = image.status, details
                    record = await session.get(Job, job.id)
                    if record is not None:
                        record.result = details
                if image.status in {"sent_verified", "cancelled"}:
                    await self.jobs.succeed(job, details)
                elif image.status == "uncertain":
                    await self.jobs.mark_uncertain(job, image.reason, details)
                else:
                    await self.jobs.fail(job, image.reason, retryable=False)
                if image.pause_replies:
                    await self.pause_replies(f"reply_{image.reason}"[:64], f"intent {intent_id}")
            if outcome.clicked:
                try:
                    # before_leave persists fresh incoming evidence before F5; avoid a duplicate
                    # full-list scan here while the selected conversation auto-reads arrivals.
                    async def before_leave(frame, obs):
                        image_path = self._save_frame(frame, obs.captured_at, rt, obs)
                        result = await self.processor.process(obs, rt, image_path=image_path)
                        self.counters["events"] += len(result.events)
                        if result.jobs or result.intents:
                            self.wake.set()

                    returned = await executor.return_to_list(target, rt, before_leave=before_leave)
                    if returned.get("returned"):
                        await self._handle_frame_locked(await self.capture_backend.capture())
                    details["return_to_list"] = returned
                    async with self.db.transaction() as session:
                        row = await session.get(ReplyIntent, intent_id)
                        if row is not None:
                            row.verification = details
                        record = await session.get(Job, job.id)
                        if record is not None:
                            record.result = {**(record.result or {}), "return_to_list": returned}
                    if returned.get("pause_replies"):
                        await self.pause_replies("reply_return_not_verified", f"intent {intent_id}")
                except Exception:
                    log.exception("post-reply observation/refresh failed for intent %s", intent_id)
                    await self.pause_replies("post_reply_observation_failed", f"intent {intent_id}")
                finally:
                    # Already queued pre-action screenshots are explicitly recorded as superseded.
                    self.frame_watermark = utcnow()
        return outcome.status

    async def _finish_reply(self, job, intent_id, outcome, complete_job=True) -> dict:
        details = {"status": outcome.status, "reason": outcome.reason, "clicked": outcome.clicked,
                   "typed": outcome.typed, "submitted": outcome.submitted, **outcome.details}
        async with self.db.session() as session:
            intent = await session.get(ReplyIntent, intent_id)
            if intent is not None and intent.status == "superseded":
                return details
        if outcome.status == "dry_run":
            await self._update_intent(intent_id, "dry_run", outcome.reason, details)
            await self.jobs.succeed(job, details)
        elif outcome.status == "sent_verified":
            await self._update_intent(intent_id, "sent_verified", None, details, sent=True)
            if complete_job:
                await self.jobs.succeed(job, details)
        elif outcome.status == "sent_unverified":
            await self._update_intent(intent_id, "sent_unverified", outcome.reason, details, sent=True)
            await self.jobs.mark_uncertain(job, outcome.reason, details)
        elif outcome.status == "superseded":
            await self._update_intent(intent_id, "superseded", outcome.reason, details)
            await self.jobs.succeed(job, details)
        else:
            # A safe pre-send stop is an actionable review, not a failed delivery.
            await self._update_intent(intent_id, "draft_review" if outcome.typed else "review", outcome.reason, details)
            if outcome.typed:
                await self.jobs.mark_uncertain(job, outcome.reason, details)
            else:
                await self.jobs.succeed(job, details)
        if outcome.pause_replies:
            rt = await self.runtime()
            alert = outcome.status != "sent_unverified" or rt.reply_unverified_alert_emails_enabled
            await self.pause_replies(f"reply_{outcome.reason}"[:64], f"intent {intent_id}", alert=alert)
        if outcome.status != "sent_verified":
            async with self.db.transaction() as session:
                row = await session.get(ReplyIntent, intent_id)
                if row is not None and row.send_image:
                    row.image_status = "cancelled"
        return details

    # ----- heartbeat / retention -----
    async def publish_heartbeat(self, status: str | None = None) -> None:
        def p(values: deque[float], q: float) -> float | None:
            if not values:
                return None
            ordered = sorted(values)
            return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 1)

        payload = {
            "owner": self.owner, "at": iso(utcnow()), "status": status or self.last_status,
            "last_frame_at": iso(self.last_frame_at), "profile": self.profile_source,
            "counters": dict(self.counters), "low_disk": self.low_disk,
            "queues": [q.snapshot() for q in (self.frames, self.email_jobs, self.reply_jobs)],
            "ocr_ms_p50": round(statistics.median(self.ocr_ms), 1) if self.ocr_ms else None,
            "ocr_ms_p95": p(self.ocr_ms, 0.95),
            "detect_latency_ms_p50": round(statistics.median(self.latencies), 1) if self.latencies else None,
            "detect_latency_ms_p95": p(self.latencies, 0.95),
            "capture": self.capture_backend.describe(),
            "ocr": self.ocr.stats() if hasattr(self.ocr, "stats") else None,
            "safety_probe": dict(self.analyzer.probe_stats) if self.analyzer is not None else None,
        }
        async with self.db.transaction() as session:
            await set_state(session, state_keys.HEARTBEAT, payload)

    async def heartbeat_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                if not await self.renew_lease():
                    log.error("worker lease lost; stopping to avoid two workers on one desktop")
                    self.request_stop()
                    return
                await self.publish_heartbeat()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("heartbeat failed")
            await self.sleep(10.0)

    async def retention_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                rt = await self.runtime()
                report = await run_retention(self.db, self.settings.frames_dir, rt)
                if report.low_disk != self.low_disk:
                    self.low_disk = report.low_disk
                    if report.low_disk:
                        await self.raise_alert("low_disk", f"{report.free_mb:.0f} MB free")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("retention failed")
            await self.sleep(3600.0)
