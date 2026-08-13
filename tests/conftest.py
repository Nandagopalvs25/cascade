import pytest

from cascade.config import Settings


@pytest.fixture
def settings_override() -> Settings:
    return Settings(database_url="postgresql+asyncpg://test:test@localhost:5432/test")
