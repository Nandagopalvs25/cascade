Ruff format


# 1. start Postgres if it isn't already running 

docker compose up -d db

# 2. run the app

uv run uvicorn cascade.main:app --reload --port 8000
