#!/bin/sh
# Backend container entrypoint.
# Runs migrations first, then starts the uvicorn dev server.
set -e

echo "[entrypoint] Running database migrations..."
alembic upgrade head

echo "[entrypoint] Starting API server..."
exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers 2
