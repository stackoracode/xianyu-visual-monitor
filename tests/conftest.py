"""Shared fixtures. Database tests run on SQLite by default and on a disposable MySQL
database when ``XVM_TEST_MYSQL_URL`` is set (as in CI)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from xianyu_monitor.db import Database  # noqa: E402
from xianyu_monitor.models import Base  # noqa: E402
from xianyu_monitor.runtime_settings import RuntimeSettings  # noqa: E402
from xianyu_monitor.settings import Settings  # noqa: E402

MYSQL_URL = os.environ.get("XVM_TEST_MYSQL_URL", "")


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return MYSQL_URL or f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
async def db(db_url: str):
    database = Database(db_url)
    async with database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield database
    await database.dispose()


@pytest.fixture
def settings(tmp_path: Path, db_url: str) -> Settings:
    return Settings(env="test", database_url=db_url, secret_key="x" * 48, data_dir=tmp_path / "data",
                    smtp_host="smtp.example.invalid", smtp_from="monitor@example.com",
                    notify_to=["owner@example.com"], capture_backend="replay", ocr_backend="fake",
                    input_backend="fake", worker_name="test-desktop")


@pytest.fixture
def rt() -> RuntimeSettings:
    return RuntimeSettings({"monitor_mode": "active", "notifications_enabled": True, "dry_run": False,
                            "reply_debounce_seconds": 0,
                            "full_validation_every_n_frames": 1000, "challenge_probe_every_n_frames": 1})
