FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ src/
COPY alembic.ini ./
COPY migrations/ migrations/
RUN uv sync --frozen --no-dev

EXPOSE 8080

ENV GOOGLE_GENAI_USE_ENTERPRISE=true

CMD ["sh", "-c", "uv run alembic upgrade head && uv run uvicorn cascade.main:app --host 0.0.0.0 --port 8080"]
