import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from cascade import models  # noqa: F401 — registers Run/Event on Base.metadata
from cascade.config import Settings
from cascade.db import Base


TEST_DATABASE_URL = "postgresql+asyncpg://cascade:cascade@localhost:5432/cascade"

_test_engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
_test_sessionmaker = async_sessionmaker(_test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def settings_override() -> Settings:
    return Settings(
        database_url=TEST_DATABASE_URL,
        gcp_project_id="test-project",
        pubsub_push_service_account="cascade-pubsub-push@test-project.iam.gserviceaccount.com",
        pubsub_push_audience="https://cascade-test.run.app",
        trello_api_key="test-key",
        trello_api_token="test-token",
        trello_api_secret="test-secret",
        trello_board_id="test-board",
        trello_callback_url="https://cascade-test.run.app/webhooks/trello",
        trello_list_todo="list-todo",
        trello_list_in_progress="list-in-progress",
        trello_list_recommended="list-recommended",
        trello_list_needs_attention="list-needs-attention",
        trello_list_done="list-done",
    )


async def _create_all() -> None:
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _truncate_all() -> None:
    async with _test_engine.begin() as conn:
        await conn.execute(text("truncate table events, runs cascade"))


async def _override_get_db():
    async with _test_sessionmaker() as session:
        yield session


@pytest.fixture
def db_session_override():

    asyncio.run(_create_all())
    yield _override_get_db
    asyncio.run(_truncate_all())


@pytest.fixture
def db_sessionmaker(db_session_override):
    return _test_sessionmaker
