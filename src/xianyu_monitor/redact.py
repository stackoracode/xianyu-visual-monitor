"""Redaction helpers for logs, diagnostics and exports."""

from __future__ import annotations

import hashlib
import re
from typing import Any

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
SECRET_KV_RE = re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization)\b(\s*[=:]\s*)(\S+)")
URL_CREDS_RE = re.compile(r"(\w+://[^:/\s]+:)([^@\s]+)(@)")
LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")

SECRET_KEYS = {"password", "smtp_password", "secret_key", "token", "authorization", "cookie"}


def redact_text(text: str) -> str:
    text = URL_CREDS_RE.sub(r"\1***\3", text)
    text = SECRET_KV_RE.sub(r"\1\2***", text)
    text = EMAIL_RE.sub("<email>", text)
    text = IPV4_RE.sub("<ip>", text)
    text = LONG_TOKEN_RE.sub("<token>", text)
    return text


def pseudonym(value: str | None, prefix: str = "name") -> str:
    """Stable, non-reversible short label for a display name (for logs/exports)."""
    if not value:
        return f"{prefix}:-"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}:{digest}"


def redact_mapping(data: Any) -> Any:
    if isinstance(data, dict):
        out = {}
        for key, value in data.items():
            if str(key).lower() in SECRET_KEYS:
                out[key] = "***" if value else value
            else:
                out[key] = redact_mapping(value)
        return out
    if isinstance(data, list):
        return [redact_mapping(item) for item in data]
    if isinstance(data, str):
        return redact_text(data)
    return data
