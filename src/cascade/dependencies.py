from typing import Annotated

from fastapi import Depends, Request
from google.adk.runners import Runner
from sqlalchemy.ext.asyncio import AsyncSession

from cascade.config import Settings, get_settings
from cascade.db import get_db


def get_runner(request: Request) -> Runner:
    return request.app.state.runner


SettingsDep = Annotated[Settings, Depends(get_settings)]
RunnerDep = Annotated[Runner, Depends(get_runner)]
DbDep = Annotated[AsyncSession, Depends(get_db)]
