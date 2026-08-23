from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from cascade.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


ADK_MANAGED_TABLES = frozenset(
    {"sessions", "events", "app_states", "user_states", "adk_internal_metadata"}
)


def exclude_adk_session_tables_from_autogenerate(
    name: str | None, type_: str, parent_names: dict
) -> bool:
    if type_ == "table":
        return name not in ADK_MANAGED_TABLES
    return True


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()
