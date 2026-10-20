"""New-customer / system-alert email: message rendering, SMTP submission, outbox handling.

SMTP acceptance (``250`` after DATA) means the configured server accepted the message for
delivery; it is not proof that the recipient received it. A timeout or disconnect while the
message is being submitted leaves the outcome unknown; such jobs become ``uncertain`` and are
only resent by an explicit owner action.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import smtplib
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate

from xianyu_monitor.i18n import translate
from xianyu_monitor.queueing.jobs import ClaimedJob, JobQueue
from xianyu_monitor.runtime_settings import RuntimeSettings
from xianyu_monitor.settings import Settings

log = logging.getLogger(__name__)


class SmtpConfigError(RuntimeError):
    """Configuration problem; retrying without owner action will not help."""


class SmtpTransientError(RuntimeError):
    """The message was definitely not accepted; safe to retry later."""


class SmtpPermanentError(RuntimeError):
    """The server rejected the message permanently."""


class SmtpUncertainError(RuntimeError):
    """The connection failed while the message was being submitted; outcome unknown."""


@dataclass
class SmtpConfig:
    host: str
    port: int
    security: str
    username: str
    password: str
    sender: str
    timeout: float

    @classmethod
    def from_settings(cls, settings: Settings) -> SmtpConfig:
        return cls(settings.smtp_host, settings.smtp_port, settings.smtp_security, settings.smtp_username,
                   settings.smtp_password, settings.smtp_from, settings.smtp_timeout_seconds)


SmtpFactory = Callable[[SmtpConfig], smtplib.SMTP]


def default_smtp_factory(cfg: SmtpConfig) -> smtplib.SMTP:
    context = ssl.create_default_context()
    if cfg.security == "ssl":
        return smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=cfg.timeout, context=context)
    conn = smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout)
    if cfg.security == "starttls":
        conn.ehlo()
        conn.starttls(context=context)
    return conn


class SmtpSender:
    def __init__(self, cfg: SmtpConfig, factory: SmtpFactory | None = None):
        self.cfg = cfg
        self.factory = factory or default_smtp_factory

    def connect(self) -> smtplib.SMTP:
        if not self.cfg.host or not self.cfg.sender:
            raise SmtpConfigError("SMTP host/sender not configured")
        try:
            conn = self.factory(self.cfg)
            conn.ehlo()
            if self.cfg.username:
                conn.login(self.cfg.username, self.cfg.password)
            return conn
        except smtplib.SMTPAuthenticationError as exc:
            raise SmtpConfigError(f"SMTP authentication failed ({exc.smtp_code})") from exc
        except smtplib.SMTPNotSupportedError as exc:
            raise SmtpConfigError("SMTP server does not support the configured security mode") from exc
        except ssl.SSLCertVerificationError as exc:
            raise SmtpConfigError("SMTP TLS certificate verification failed") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise SmtpTransientError(f"SMTP connect failed: {type(exc).__name__}") from exc

    def submit(self, conn: smtplib.SMTP, message: EmailMessage) -> dict:
        try:
            refused = conn.send_message(message)
        except smtplib.SMTPRecipientsRefused as exc:
            codes = [code for code, _ in exc.recipients.values()]
            if all(400 <= code < 500 for code in codes):
                raise SmtpTransientError(f"recipients temporarily refused {codes}") from exc
            raise SmtpPermanentError(f"recipients refused {codes}") from exc
        except smtplib.SMTPSenderRefused as exc:
            if 400 <= exc.smtp_code < 500:
                raise SmtpTransientError(f"sender temporarily refused ({exc.smtp_code})") from exc
            raise SmtpPermanentError(f"sender refused ({exc.smtp_code})") from exc
        except smtplib.SMTPDataError as exc:
            # The server answered DATA with an explicit rejection: not accepted.
            if 400 <= exc.smtp_code < 500:
                raise SmtpTransientError(f"message temporarily rejected ({exc.smtp_code})") from exc
            raise SmtpPermanentError(f"message rejected ({exc.smtp_code})") from exc
        except (smtplib.SMTPException, OSError) as exc:  # disconnects, timeouts, resets, unexpected replies
            raise SmtpUncertainError(f"connection lost during submission: {type(exc).__name__}") from exc
        return {code_addr: list(map(str, value)) for code_addr, value in (refused or {}).items()}

    @staticmethod
    def close(conn: smtplib.SMTP) -> None:
        try:
            conn.quit()
        except Exception:  # noqa: BLE001 - quitting is best-effort after acceptance
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def test_connection(self) -> str:
        conn = self.connect()
        try:
            code, _ = conn.noop()
            return f"ok ({code})"
        finally:
            self.close(conn)


def _message_id(dedup_key: str, sender: str) -> str:
    domain = sender.rsplit("@", 1)[-1] if "@" in sender else "localhost"
    token = hashlib.sha256(dedup_key.encode("utf-8")).hexdigest()[:24]
    return f"<xvm.{token}@{domain}>"


def build_message(cfg: SmtpConfig, recipients: list[str], subject: str, body: str, dedup_key: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = cfg.sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False, usegmt=True)
    # Deterministic Message-ID: a manual resend of the same notification carries the same ID,
    # which lets many mail clients collapse duplicates.
    message["Message-ID"] = _message_id(dedup_key, cfg.sender)
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(body)
    return message


def render_new_customer(payload: dict, lang: str, base_url: str = "") -> tuple[str, str]:
    name = payload.get("display_name") or "?"
    subject = translate("email.new_customer.subject", lang, name=name)
    lines = [
        translate("email.new_customer.intro", lang),
        "",
        f"{translate('email.field.name', lang)}: {name}",
        f"{translate('email.field.preview', lang)}: {payload.get('preview') or '-'}",
        f"{translate('email.field.observed_at', lang)}: {payload.get('observed_at')}",
        f"{translate('email.field.evidence', lang)}: {', '.join(payload.get('signals') or []) or '-'}",
        f"{translate('email.field.confidence', lang)}: {payload.get('name_conf', 0):.2f} / {payload.get('preview_conf', 0):.2f}",
        f"{translate('email.field.customer_id', lang)}: {payload.get('customer_id')}",
        "",
        translate("email.new_customer.limitations", lang),
    ]
    if base_url and payload.get("customer_id"):
        lines += ["", f"{base_url.rstrip('/')}/customers/{payload['customer_id']}"]
    return subject, "\n".join(lines) + "\n"


def render_alert(payload: dict, lang: str, base_url: str = "") -> tuple[str, str]:
    reason = payload.get("reason", "unknown")
    subject = translate("email.alert.subject", lang, reason=reason)
    body = [translate("email.alert.intro", lang), "", f"{translate('email.field.reason', lang)}: {reason}",
            f"{translate('email.field.detail', lang)}: {payload.get('detail', '')}",
            f"{translate('email.field.observed_at', lang)}: {payload.get('observed_at')}"]
    if base_url:
        body += ["", base_url]
    return subject, "\n".join(body) + "\n"


def render(payload: dict, lang: str, base_url: str = "") -> tuple[str, str]:
    kind = payload.get("type")
    if kind == "new_customer":
        return render_new_customer(payload, lang, base_url)
    if kind == "alert":
        return render_alert(payload, lang, base_url)
    return payload.get("subject", "xianyu-visual-monitor"), payload.get("body", "")


class EmailJobHandler:
    def __init__(self, settings: Settings, factory: SmtpFactory | None = None,
                 on_success: Callable[[ClaimedJob], object] | None = None):
        self.settings = settings
        self.factory = factory
        self.on_success = on_success

    async def handle(self, job: ClaimedJob, queue: JobQueue, rt: RuntimeSettings) -> str:
        loop = asyncio.get_running_loop()
        payload = job.payload
        recipients = payload.get("to") or list(self.settings.notify_to)
        subject, body = render(payload, payload.get("lang") or rt.email_language, self.settings.web_base_url)
        if not recipients:
            await queue.fail(job, "no recipient configured (XVM_NOTIFY_TO)", retryable=False)
            return "failed"
        if rt.dry_run and not payload.get("force_send"):
            await queue.succeed(job, {"dry_run": True, "subject": subject, "recipients": len(recipients)})
            await self._after_success(job)
            return "dry_run"
        cfg = SmtpConfig.from_settings(self.settings)
        sender = SmtpSender(cfg, self.factory)
        message = build_message(cfg, recipients, subject, body, job.dedup_key)
        try:
            conn = await loop.run_in_executor(None, sender.connect)
        except SmtpConfigError as exc:
            await queue.fail(job, str(exc), retryable=False)
            return "failed"
        except SmtpTransientError as exc:
            return await queue.fail(job, str(exc), retryable=True, backoff_seconds=rt.email_retry_base_seconds)
        try:
            if not await queue.set_phase(job, "submitting"):
                sender.close(conn)
                return "lease_lost"
            try:
                refused = await loop.run_in_executor(None, sender.submit, conn, message)
            except SmtpTransientError as exc:
                await queue.set_phase(job, "rejected")
                return await queue.fail(job, str(exc), retryable=True, backoff_seconds=rt.email_retry_base_seconds)
            except SmtpPermanentError as exc:
                await queue.fail(job, str(exc), retryable=False)
                return "failed"
            except SmtpUncertainError as exc:
                await queue.mark_uncertain(job, str(exc))
                return "uncertain"
            await queue.succeed(job, {"accepted_by_smtp_server": True, "refused": refused,
                                      "message_id": message["Message-ID"]})
            await self._after_success(job)
            return "succeeded"
        finally:
            await loop.run_in_executor(None, sender.close, conn)

    async def _after_success(self, job: ClaimedJob) -> None:
        if self.on_success is not None:
            result = self.on_success(job)
            if asyncio.iscoroutine(result):
                await result
