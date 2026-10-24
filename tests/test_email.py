import datetime as dt
import smtplib

from sqlalchemy import delete, select

from xianyu_monitor.models import Job, JobAttempt
from xianyu_monitor.notify.emailer import EmailJobHandler, SmtpConfig, build_message, render_new_customer
from xianyu_monitor.queueing.jobs import JobQueue, enqueue
from xianyu_monitor.runtime_settings import RuntimeSettings
from xianyu_monitor.timeutil import utcnow


class FakeSMTP:
    instances: list = []

    def __init__(self, behavior: str = "ok"):
        self.behavior = behavior
        self.sent: list = []
        FakeSMTP.instances.append(self)

    def ehlo(self):
        return 250, b"ok"

    def login(self, user, password):
        if self.behavior == "auth":
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    def send_message(self, message):
        if self.behavior == "data4xx":
            raise smtplib.SMTPDataError(451, b"try later")
        if self.behavior == "data5xx":
            raise smtplib.SMTPDataError(554, b"rejected")
        if self.behavior == "disconnect":
            raise smtplib.SMTPServerDisconnected("connection lost")
        if self.behavior == "timeout":
            raise TimeoutError("timed out")
        if self.behavior == "rcpt4xx":
            raise smtplib.SMTPRecipientsRefused({"owner@example.com": (450, b"mailbox busy")})
        self.sent.append(message)
        return {}

    def noop(self):
        return 250, b"ok"

    def quit(self):
        return 221, b"bye"

    def close(self):
        pass


def factory(behavior):
    def make(cfg):
        if behavior == "refused":
            raise ConnectionRefusedError("refused")
        return FakeSMTP(behavior)
    return make


PAYLOAD = {"type": "new_customer", "customer_id": "c-1", "display_name": "买家_甲", "preview": "还在吗？",
           "observed_at": "2026-10-20T21:00:00Z", "signals": ["absent_from_baseline", "incoming_badge"],
           "name_conf": 0.98, "preview_conf": 0.97}


async def run_job(db, settings, behavior, rt=None, payload=None):
    async with db.transaction() as s:
        await enqueue(s, "email", "new_customer:c-1", payload or PAYLOAD, max_attempts=3)
    queue = JobQueue(db, "w")
    (job,) = await queue.claim()
    handler = EmailJobHandler(settings, factory(behavior))
    outcome = await handler.handle(job, queue, rt or RuntimeSettings({"dry_run": False}))
    async with db.session() as s:
        row = (await s.execute(select(Job))).scalar_one()
    return outcome, row


async def test_success_records_acceptance_not_delivery(db, settings):
    FakeSMTP.instances.clear()
    outcome, row = await run_job(db, settings, "ok")
    assert outcome == "succeeded" and row.status == "succeeded"
    assert row.result["accepted_by_smtp_server"] is True
    message = FakeSMTP.instances[-1].sent[0]
    body = message.get_content()
    assert "买家_甲" in message["Subject"] and "还在吗？" in body
    assert "not proof of first-ever contact" in body


async def test_connection_refused_is_retried(db, settings):
    outcome, row = await run_job(db, settings, "refused")
    assert outcome == "pending" and row.status == "pending" and row.attempts == 1


async def test_auth_failure_is_not_retried(db, settings):
    settings = settings.model_copy(update={"smtp_username": "monitor@example.com", "smtp_password": "app-password"})
    outcome, row = await run_job(db, settings, "auth")
    assert outcome == "failed" and "authentication" in row.last_error


async def test_explicit_temporary_rejection_is_retried(db, settings):
    outcome, row = await run_job(db, settings, "data4xx")
    assert outcome == "pending"
    outcome, row2 = await run_job_again(db, settings, "rcpt4xx")
    assert row2.status in {"pending", "failed"}


async def run_job_again(db, settings, behavior):
    async with db.transaction() as s:
        row = (await s.execute(select(Job))).scalar_one()
        from xianyu_monitor.timeutil import utcnow
        row.available_at = utcnow()
    queue = JobQueue(db, "w2")
    (job,) = await queue.claim()
    outcome = await EmailJobHandler(settings, factory(behavior)).handle(job, queue, RuntimeSettings({"dry_run": False}))
    async with db.session() as s:
        return outcome, (await s.execute(select(Job))).scalar_one()


async def test_permanent_rejection_fails(db, settings):
    outcome, row = await run_job(db, settings, "data5xx")
    assert outcome == "failed" and row.status == "failed"


async def test_disconnect_during_submission_is_uncertain_and_not_retried(db, settings):
    for behavior in ("disconnect", "timeout"):
        async with db.transaction() as s:
            await s.execute(delete(JobAttempt))
            await s.execute(delete(Job))
        outcome, row = await run_job(db, settings, behavior)
        assert outcome == "uncertain" and row.status == "uncertain"
        assert await JobQueue(db, "w3").claim() == []


async def test_dry_run_never_connects(db, settings):
    FakeSMTP.instances.clear()
    outcome, row = await run_job(db, settings, "ok", RuntimeSettings({"dry_run": True}))
    assert outcome == "dry_run" and row.status == "succeeded" and row.result["dry_run"] is True
    assert FakeSMTP.instances == []


async def test_missing_recipient_fails_without_retry(db, settings):
    settings = settings.model_copy(update={"notify_to": []})
    outcome, row = await run_job(db, settings, "ok")
    assert outcome == "failed" and "recipient" in row.last_error


def test_message_id_is_deterministic_per_notification():
    cfg = SmtpConfig("h", 25, "none", "", "", "monitor@example.com", 5)
    a = build_message(cfg, ["owner@example.com"], "s", "b", "new_customer:c-1")
    b = build_message(cfg, ["owner@example.com"], "s", "b", "new_customer:c-1")
    c = build_message(cfg, ["owner@example.com"], "s", "b", "new_customer:c-2")
    assert a["Message-ID"] == b["Message-ID"] != c["Message-ID"]


def test_chinese_email_rendering_preserves_source_text():
    subject, body = render_new_customer(PAYLOAD, "zh")
    assert "新客户" in subject and "买家_甲" in subject and "还在吗？" in body


async def test_offline_window_preserves_attempt_budget_then_sends_once(db, settings):
    rt = RuntimeSettings({'dry_run': False, 'email_network_wait_hours': 24})
    outcome, row = await run_job(db, settings, 'refused', rt)
    queue = JobQueue(db, 'network-test')
    assert outcome == 'pending' and row.phase == 'network_wait' and row.attempts == 0
    for _ in range(5):
        async with db.transaction() as s:
            current = await s.get(Job, row.id)
            current.available_at = utcnow() - dt.timedelta(seconds=1)
        (job,) = await queue.claim(['email'])
        assert await EmailJobHandler(settings, factory('refused')).handle(job, queue, rt) == 'pending'
    async with db.transaction() as s:
        current = await s.get(Job, row.id)
        current.available_at = utcnow() - dt.timedelta(seconds=1)
    (job,) = await queue.claim(['email'])
    FakeSMTP.instances.clear()
    assert await EmailJobHandler(settings, factory('ok')).handle(job, queue, rt) == 'succeeded'
    assert len(FakeSMTP.instances[-1].sent) == 1
    assert await queue.claim(['email']) == []
    async with db.session() as s:
        attempts = (await s.execute(select(JobAttempt))).scalars().all()
        assert len(attempts) == 7 and sum(a.phase == 'network_wait' for a in attempts) == 6


async def test_offline_window_deadline_survives_restart(db, settings):
    rt = RuntimeSettings({'dry_run': False, 'email_network_wait_hours': 1})
    _, row = await run_job(db, settings, 'refused', rt)
    async with db.transaction() as s:
        current = await s.get(Job, row.id)
        current.created_at = utcnow() - dt.timedelta(hours=2)
        current.available_at = utcnow() - dt.timedelta(seconds=1)
    queue = JobQueue(db, 'restarted')
    (job,) = await queue.claim()
    assert await EmailJobHandler(settings, factory('refused')).handle(job, queue, rt) == 'failed'
    assert await queue.claim() == []


async def test_offline_window_does_not_retry_uncertain_submission(db, settings):
    rt = RuntimeSettings({'dry_run': False, 'email_network_wait_hours': 24})
    outcome, row = await run_job(db, settings, 'timeout', rt)
    assert outcome == 'uncertain' and row.status == 'uncertain'
    assert await JobQueue(db, 'restarted').claim() == []
