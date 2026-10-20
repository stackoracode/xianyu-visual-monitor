"""Password hashing (PBKDF2-SHA256, stdlib), CSRF tokens and login throttling."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import secrets

from xianyu_monitor.timeutil import utcnow

PBKDF2_ITERATIONS = 390_000
MAX_FAILED_LOGINS = 5
LOCKOUT = dt.timedelta(minutes=15)


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    if len(password) < 10:
        raise ValueError("password must be at least 10 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_matches(expected: str | None, submitted: str | None) -> bool:
    return bool(expected) and bool(submitted) and hmac.compare_digest(str(expected), str(submitted))


def is_locked(locked_until: dt.datetime | None) -> bool:
    return locked_until is not None and locked_until > utcnow()


def lock_deadline() -> dt.datetime:
    return utcnow() + LOCKOUT
