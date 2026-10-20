"""The Alembic migration must produce exactly the schema described by the ORM models."""

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from conftest import MYSQL_URL
from xianyu_monitor.db import run_migrations, sync_url
from xianyu_monitor.models import Base


def _reset(url: str) -> None:
    engine = create_engine(sync_url(url))
    with engine.begin() as conn:
        if engine.dialect.name == "mysql":
            conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        Base.metadata.drop_all(conn)
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        if engine.dialect.name == "mysql":
            conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))
    engine.dispose()


@pytest.fixture
def migrated_url(tmp_path):
    url = MYSQL_URL or f"sqlite+aiosqlite:///{tmp_path / 'migrate.db'}"
    _reset(url)
    run_migrations(url)
    yield url
    _reset(url)


def test_migration_matches_models(migrated_url):
    engine = create_engine(sync_url(migrated_url))
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
        tables = set(inspect(conn).get_table_names())
    engine.dispose()
    assert set(Base.metadata.tables) <= tables
    assert diff == [], diff


def test_migration_is_idempotent_at_head(migrated_url):
    run_migrations(migrated_url)  # second upgrade is a no-op
