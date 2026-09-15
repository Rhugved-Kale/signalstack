#!/usr/bin/env bash
#
# Production entrypoint (Render runs this as the start command).
#
# Migrations run BEFORE the server starts and a failure aborts the boot:
# serving an API against a schema that does not match the models produces
# confusing 500s at request time instead of one clear failure at deploy time.
set -euo pipefail

# Render injects PORT; default to 8000 so this script also works locally.
PORT="${PORT:-8000}"

echo "==> SignalStack API starting (environment=${ENVIRONMENT:-local}, port=${PORT})"

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "==> WARNING: DATABASE_URL is not set; falling back to the local default"
fi

echo "==> Applying database migrations"
if ! alembic upgrade head; then
  echo "!!! Migrations failed — refusing to start the server" >&2
  exit 1
fi
echo "==> Migrations applied"

echo "==> Launching uvicorn on 0.0.0.0:${PORT}"
# A single worker on purpose: the free tier has 512 MB, and each worker would
# carry its own connection pool and its own bootstrap thread.
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --workers 1 \
  --proxy-headers \
  --forwarded-allow-ips '*'
