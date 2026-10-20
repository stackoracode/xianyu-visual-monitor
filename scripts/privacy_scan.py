#!/usr/bin/env python3
"""Pre-publication scan of tracked files for private data patterns.

Checks: forbidden file types (screenshots, saved/rendered HTML, archives, env files), email
addresses outside reserved example domains, non-loopback IPv4 addresses, URLs outside an
allow-list, private keys and credential-looking assignments. Run: ``python scripts/privacy_scan.py``.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".zip", ".har", ".db", ".sqlite3", ".pem", ".key"}
ALLOWED_HTML_DIR = "src/xianyu_monitor/web/templates/"
EMAIL_RE = re.compile(r"(?<![:/\w])[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})\b")
ALLOWED_EMAIL_DOMAINS = {"example.com", "example.invalid", "example.org", "anthropic.com"}
IPV4_RE = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")
ALLOWED_IPS = {"127.0.0.1", "0.0.0.0", "4.10.0.84"}  # last one: the pinned opencv-python version
DOC_NETS = ("192.0.2.", "198.51.100.", "203.0.113.")  # RFC 5737 documentation ranges
URL_RE = re.compile(r"https?://([A-Za-z0-9.-]+)")
ALLOWED_URL_HOSTS = {
    "github.com", "pypi.org", "files.pythonhosted.org", "www.ubuntu.com", "ubuntu.com", "docs.python.org",
    "onnxruntime.ai", "github.com.", "testserver", "localhost", "127.0.0.1", "smtp.example.com",
    "claude.com", "conda-forge.org", "opensource.org", "www.sqlalchemy.org", "fastapi.tiangolo.com",
}
SECRET_RES = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(password|passwd|secret|token)\s*[=:]\s*['\"]?(?!CHANGE_ME|\*\*\*|x+\b|<|\{|\$|os\.|settings|getpass|new_csrf_token|hash_password|request\.|str\(|data\.)[A-Za-z0-9+/_-]{12,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    return [p for p in out.decode().split("\0") if p]


def scan() -> list[str]:
    problems: list[str] = []
    for rel in tracked_files():
        path = ROOT / rel
        name = path.name
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            problems.append(f"{rel}: forbidden file type")
            continue
        if path.suffix.lower() in {".html", ".htm"} and not rel.startswith(ALLOWED_HTML_DIR):
            problems.append(f"{rel}: HTML outside templates (raw page captures are not allowed)")
        if name == ".env" or (name.endswith(".env") and not name.endswith(".example")):
            problems.append(f"{rel}: environment file")
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            problems.append(f"{rel}: binary or unreadable file")
            continue
        if rel == "scripts/privacy_scan.py":
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for match in EMAIL_RE.finditer(line):
                domain = match.group(1).lower()
                if not any(domain == d or domain.endswith("." + d) for d in ALLOWED_EMAIL_DOMAINS):
                    problems.append(f"{rel}:{lineno}: email address with non-example domain")
            for match in IPV4_RE.finditer(line):
                parts = [int(p) for p in match.groups()]
                ip = ".".join(map(str, parts))
                if all(p <= 255 for p in parts) and ip not in ALLOWED_IPS and not ip.startswith(DOC_NETS):
                    problems.append(f"{rel}:{lineno}: IPv4 address {ip}")
            for match in URL_RE.finditer(line):
                host = match.group(1).lower().rstrip(".")
                if host not in ALLOWED_URL_HOSTS and not host.endswith(".example.com"):
                    problems.append(f"{rel}:{lineno}: URL host {host} not in allow-list")
            for regex in SECRET_RES:
                if regex.search(line):
                    problems.append(f"{rel}:{lineno}: possible credential")
    return problems


if __name__ == "__main__":
    found = scan()
    for problem in found:
        print(problem)
    print(f"privacy scan: {len(found)} problem(s)")
    sys.exit(1 if found else 0)
