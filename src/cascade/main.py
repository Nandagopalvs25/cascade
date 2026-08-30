import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI

from cascade.agents.runtime import build_campaign_runner
from cascade.config import get_settings
from cascade.db import engine
from cascade.observability import configure_structured_logging
from cascade.routes import health, pubsub, webhooks


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_structured_logging()
    app.state.runner = build_campaign_runner(get_settings())
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
app.include_router(webhooks.router)
app.include_router(pubsub.router)
