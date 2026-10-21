"""Administrator-editable runtime settings stored in the ``settings`` table.

Safety-relevant defaults: the monitor starts in *baseline* mode, dry-run is on, email
notifications are off and keyword replies are off. Each must be changed deliberately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xianyu_monitor.models import SettingRow
from xianyu_monitor.timeutil import utcnow


@dataclass(frozen=True)
class SettingSpec:
    key: str
    kind: str  # int | float | bool | choice | list | str
    default: Any
    group: str
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = field(default_factory=tuple)


DEFAULT_SYSTEM_NAMES = ["闲小蜜", "闲鱼小助手", "闲鱼助手", "系统通知", "官方通知", "闲鱼官方"]
DEFAULT_CHALLENGE_KEYWORDS = [
    "安全验证", "请完成验证", "拖动滑块", "滑块验证", "验证码", "扫码登录", "请登录", "登录已过期",
    "账号异常", "captcha", "verify you are human",
]
DEFAULT_SENSITIVE_TERMS = [
    "价格", "多少钱", "便宜", "优惠", "包邮", "砍价", "最低", "退款", "退货", "退钱", "赔", "投诉",
    "地址", "电话", "手机号", "微信", "vx", "qq", "身份证", "银行卡", "支付宝", "转账", "定金",
    "price", "discount", "refund", "return", "address", "phone", "wechat",
]
DEFAULT_ATTACHMENT_MARKERS = ["[图片]", "[视频]", "[语音]", "[文件]", "[位置]", "[卡片消息]", "[链接]", "[表情]"]
DEFAULT_NEGATIONS = ["不", "没", "别", "无", "非", "不要", "不是", "不用", "not", "no", "don't", "never"]

SPECS: tuple[SettingSpec, ...] = (
    # Monitoring
    SettingSpec("monitor_mode", "choice", "baseline", "monitoring", choices=("baseline", "active")),
    SettingSpec("capture_interval_seconds", "float", 6.0, "monitoring", 1.0, 600.0),
    SettingSpec("full_validation_every_n_frames", "int", 10, "monitoring", 1, 1000),
    SettingSpec("ocr_min_confidence", "float", 0.80, "monitoring", 0.0, 1.0),
    SettingSpec("name_fuzzy_similarity", "float", 0.80, "monitoring", 0.5, 0.99),
    SettingSpec("avatar_match_distance", "int", 10, "monitoring", 0, 64),
    SettingSpec("avatar_conflict_distance", "int", 22, "monitoring", 0, 64),
    SettingSpec("preview_change_similarity", "float", 0.90, "monitoring", 0.5, 1.0),
    SettingSpec("new_customer_max_age_minutes", "int", 120, "monitoring", 0, 100000),
    SettingSpec("layout_failure_pause_after", "int", 3, "monitoring", 1, 1000),
    SettingSpec("capture_failure_pause_after", "int", 3, "monitoring", 1, 1000),
    SettingSpec("challenge_probe_every_n_frames", "int", 5, "monitoring", 1, 1000),
    SettingSpec("system_conversation_names", "list", DEFAULT_SYSTEM_NAMES, "monitoring"),
    SettingSpec("ignored_customer_names", "list", [], "monitoring"),
    SettingSpec("challenge_keywords", "list", DEFAULT_CHALLENGE_KEYWORDS, "monitoring"),
    # Notifications
    SettingSpec("dry_run", "bool", True, "notifications"),
    SettingSpec("notifications_enabled", "bool", False, "notifications"),
    SettingSpec("alert_emails_enabled", "bool", True, "notifications"),
    SettingSpec("reply_unverified_alert_emails_enabled", "bool", True, "notifications"),
    SettingSpec("email_language", "choice", "en", "notifications", choices=("en", "zh")),
    SettingSpec("email_max_attempts", "int", 5, "notifications", 1, 20),
    SettingSpec("email_retry_base_seconds", "int", 60, "notifications", 5, 86400),
    # Replies
    SettingSpec("reply_mode", "choice", "off", "replies", choices=("off", "review", "auto")),
    SettingSpec("reply_send_enabled", "bool", False, "replies"),
    SettingSpec("reply_min_confidence", "float", 0.90, "replies", 0.0, 1.0),
    SettingSpec("reply_debounce_seconds", "float", 3.0, "replies", 0.0, 30.0),
    SettingSpec("reply_verify_timeout_seconds", "float", 8.0, "replies", 1.0, 60.0),
    SettingSpec("reply_refresh_timeout_seconds", "float", 20.0, "replies", 1.0, 60.0),
    SettingSpec("reply_default_image_path", "str", "", "replies"),
    SettingSpec("reply_image_send_enabled", "bool", False, "replies"),
    SettingSpec("reply_image_timeout_seconds", "float", 180.0, "replies", 5.0, 600.0),
    SettingSpec("reply_image_verify_seconds", "float", 5.0, "replies", 1.0, 30.0),
    SettingSpec("reply_step_delay_seconds", "float", 1.0, "replies", 0.1, 10.0),
    SettingSpec("reply_global_min_interval_seconds", "int", 30, "replies", 0, 86400),
    SettingSpec("reply_max_per_customer_per_day", "int", 3, "replies", 0, 1000),
    SettingSpec("reply_text_match_similarity", "float", 0.85, "replies", 0.5, 1.0),
    SettingSpec("sensitive_terms", "list", DEFAULT_SENSITIVE_TERMS, "replies"),
    SettingSpec("attachment_markers", "list", DEFAULT_ATTACHMENT_MARKERS, "replies"),
    SettingSpec("negation_terms", "list", DEFAULT_NEGATIONS, "replies"),
    # Retention / storage
    SettingSpec("save_frames", "choice", "changes", "retention", choices=("none", "changes", "all")),
    SettingSpec("frames_max_count", "int", 200, "retention", 0, 100000),
    SettingSpec("frames_max_mb", "int", 300, "retention", 0, 100000),
    SettingSpec("frame_rows_retention_days", "int", 14, "retention", 1, 3650),
    SettingSpec("events_retention_days", "int", 90, "retention", 1, 3650),
    SettingSpec("jobs_retention_days", "int", 180, "retention", 1, 3650),
    SettingSpec("min_free_disk_mb", "int", 1024, "retention", 0, 10_000_000),
)

SPEC_BY_KEY = {spec.key: spec for spec in SPECS}
GROUPS = ("monitoring", "notifications", "replies", "retention")


class SettingError(ValueError):
    pass


def coerce(spec: SettingSpec, raw: Any) -> Any:
    """Validate and convert a submitted value (form string or JSON value)."""
    try:
        if spec.kind == "bool":
            if isinstance(raw, bool):
                value: Any = raw
            else:
                value = str(raw).strip().lower() in {"1", "true", "yes", "on"}
        elif spec.kind == "int":
            value = int(str(raw).strip())
        elif spec.kind == "float":
            value = float(str(raw).strip())
        elif spec.kind == "choice":
            value = str(raw).strip()
            if value not in spec.choices:
                raise SettingError(f"{spec.key}: must be one of {', '.join(spec.choices)}")
        elif spec.kind == "list":
            if isinstance(raw, list):
                items = raw
            else:
                items = str(raw).replace("\r", "").split("\n")
            value = [str(item).strip() for item in items if str(item).strip()]
        else:
            value = str(raw)
    except (TypeError, ValueError) as exc:
        if isinstance(exc, SettingError):
            raise
        raise SettingError(f"{spec.key}: invalid value") from exc
    if spec.kind in {"int", "float"}:
        if spec.minimum is not None and value < spec.minimum:
            raise SettingError(f"{spec.key}: must be >= {spec.minimum}")
        if spec.maximum is not None and value > spec.maximum:
            raise SettingError(f"{spec.key}: must be <= {spec.maximum}")
    return value


class RuntimeSettings:
    """Immutable snapshot of runtime settings with attribute access."""

    def __init__(self, values: dict[str, Any] | None = None):
        merged = {spec.key: spec.default for spec in SPECS}
        if values:
            for key, value in values.items():
                if key in SPEC_BY_KEY:
                    merged[key] = value
        self._values = merged

    def __getattr__(self, name: str) -> Any:
        try:
            return self._values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def as_dict(self) -> dict[str, Any]:
        return dict(self._values)

    def with_overrides(self, **overrides: Any) -> RuntimeSettings:
        return RuntimeSettings({**self._values, **overrides})

    @property
    def auto_reply_allowed(self) -> bool:
        """Automatic queuing of matched replies. In dry-run the job only runs prechecks."""
        return self.reply_mode == "auto" and bool(self.reply_send_enabled)


async def load_runtime(session: AsyncSession) -> RuntimeSettings:
    rows = (await session.execute(select(SettingRow))).scalars().all()
    stored = {}
    for row in rows:
        spec = SPEC_BY_KEY.get(row.key)
        if spec is None:
            continue
        try:
            stored[row.key] = coerce(spec, row.value)
        except SettingError:
            continue
    return RuntimeSettings(stored)


async def save_runtime(session: AsyncSession, changes: dict[str, Any], actor: str) -> dict[str, Any]:
    applied: dict[str, Any] = {}
    for key, raw in changes.items():
        spec = SPEC_BY_KEY.get(key)
        if spec is None:
            raise SettingError(f"unknown setting: {key}")
        value = coerce(spec, raw)
        row = await session.get(SettingRow, key)
        if row is None:
            session.add(SettingRow(key=key, value=value, updated_at=utcnow(), updated_by=actor))
        else:
            row.value = value
            row.updated_at = utcnow()
            row.updated_by = actor
        applied[key] = value
    return applied
