import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI

from cascade.db import engine
from cascade.routes import health


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await engine.dispose()


app = FastAPI(title="Cascade", lifespan=lifespan)


@app.middleware("http")
async def request_id(request, call_next):
    request.state.request_id = str(uuid.uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


app.include_router(health.router)
