"""Async database engine/session helpers and small key-value accessors."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from xianyu_monitor.models import Base, SystemState
from xianyu_monitor.timeutil import utcnow

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


class Database:
    def __init__(self, url: str, echo: bool = False):
        self.url = url
        kwargs: dict = {"echo": echo, "pool_pre_ping": True}
        if url.startswith("mysql"):
            kwargs.update(pool_recycle=1800, pool_size=5, max_overflow=5)
        self.engine: AsyncEngine = create_async_engine(url, **kwargs)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    @property
    def is_mysql(self) -> bool:
        return self.url.startswith("mysql")

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as session:
            yield session

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as session:
            async with session.begin():
                yield session

    async def create_all(self) -> None:
        """Test/dev helper. Production uses Alembic migrations (``xianyu-monitor migrate``)."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()


def sync_url(url: str) -> str:
    return url.replace("+aiomysql", "+pymysql").replace("+aiosqlite", "")


def run_migrations(url: str, revision: str = "head") -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", sync_url(url).replace("%", "%%"))
    command.upgrade(cfg, revision)


async def get_state(session: AsyncSession, key: str) -> dict:
    row = await session.get(SystemState, key)
    return dict(row.value or {}) if row else {}


async def set_state(session: AsyncSession, key: str, value: dict) -> None:
    row = await session.get(SystemState, key)
    if row is None:
        session.add(SystemState(key=key, value=value, updated_at=utcnow()))
    else:
        row.value = value
        row.updated_at = utcnow()


async def all_states(session: AsyncSession) -> dict[str, dict]:
    rows = (await session.execute(select(SystemState))).scalars().all()
    return {row.key: dict(row.value or {}) for row in rows}
