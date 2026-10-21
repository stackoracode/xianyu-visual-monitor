"""Static process settings loaded from environment variables (prefix ``XVM_``).

Secrets (database password, SMTP password, session key, recipient address) belong in a
private environment file such as ``/etc/xianyu-monitor/monitor.env`` that is referenced by
systemd ``EnvironmentFile=`` and never committed. Operational knobs that an administrator
may change at runtime live in the database instead (see ``runtime_settings``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ENV_PREFIX = "XVM_"


class Settings(BaseModel):
    env: Literal["production", "development", "test"] = "production"
    database_url: str = Field(default="mysql+aiomysql://xvm:CHANGE_ME@127.0.0.1:3306/xianyu_monitor?charset=utf8mb4",
                              repr=False)
    secret_key: str = Field(default="", repr=False)
    data_dir: Path = Path("/var/lib/xianyu-monitor")
    calibration_file: Path | None = None

    web_host: str = "127.0.0.1"
    web_port: int = 8080
    web_base_url: str = ""
    session_cookie_secure: bool = False
    session_max_age_seconds: int = 8 * 3600
    default_language: Literal["en", "zh"] = "en"

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_username: str = Field(default="", repr=False)
    smtp_password: str = Field(default="", repr=False)
    smtp_from: str = Field(default="", repr=False)
    smtp_timeout_seconds: float = 30.0
    notify_to: list[str] = Field(default_factory=list, repr=False)

    capture_backend: Literal["scrot", "command", "replay"] = "scrot"
    capture_display: str = "auto"
    capture_xauthority: str = "auto"
    capture_command: str = ""
    replay_dir: Path | None = None

    ocr_backend: Literal["rapidocr", "tesseract", "fake"] = "rapidocr"
    ocr_workers: int = 1
    ocr_use_processes: bool = True
    tesseract_lang: str = "chi_sim+eng"
    # "auto" pins OCR processes to the fastest CPU cluster (the A76 cores on RK3588) when the
    # CPUs differ; "none" disables pinning; or an explicit list such as "4-7".
    ocr_cpu_affinity: str = "auto"
    ocr_threads: int = 0  # ONNX Runtime intra-op threads; 0 = one per pinned CPU, else ORT default
    ocr_cache_size: int = 4096  # exact-pixel OCR result cache entries; 0 disables

    input_backend: Literal["xdotool", "fake"] = "xdotool"
    input_text_method: Literal["clipboard", "xdotool_type"] = "clipboard"

    worker_name: str = "desktop-main"
    log_level: str = "INFO"
    log_json: bool = True
    log_redact_names: bool = True

    @field_validator("notify_to", mode="before")
    @classmethod
    def _split_recipients(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(os.path.expanduser(value))
        return value

    @property
    def frames_dir(self) -> Path:
        return self.data_dir / "frames"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def run_dir(self) -> Path:
        return self.data_dir / "run"

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

    def redacted(self) -> dict:
        data = self.model_dump(mode="json")
        for key in ("secret_key", "smtp_password"):
            data[key] = "***" if data.get(key) else ""
        data["database_url"] = redact_url(self.database_url)
        data["notify_to"] = ["<redacted>" for _ in self.notify_to]
        data["smtp_username"] = "<redacted>" if self.smtp_username else ""
        data["smtp_from"] = "<redacted>" if self.smtp_from else ""
        return data


def redact_url(url: str) -> str:
    if "://" not in url or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.rsplit("@", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal KEY=VALUE env file (systemd EnvironmentFile compatible subset)."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_settings(environ: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if environ is None else environ)
    env_file = env.get(ENV_PREFIX + "ENV_FILE")
    if env_file and Path(env_file).is_file():
        file_values = parse_env_file(Path(env_file))
        # Real environment wins over the file.
        env = {**file_values, **env}
    fields = Settings.model_fields
    data: dict[str, object] = {}
    for name in fields:
        key = ENV_PREFIX + name.upper()
        if key in env:
            data[name] = env[key]
    return Settings(**data)
