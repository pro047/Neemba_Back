#!/bin/sh
# Container entrypoint: apply DB migrations, then exec the server (CMD).
#
# Migrations only need the POSTGRES_* env vars (see migrations/env.py ->
# get_postgres_sync_url()); full app boot additionally requires NATS/DeepL/WS
# secrets. We run "alembic upgrade head" here so the schema is in place before
# the server (gunicorn in prod, uvicorn in dev) starts. The CMD is passed as
# "$@" and exec'd so it becomes PID 1's payload (signals/healthcheck intact).
set -e

# alembic.ini lives next to this script in the image (/app). Resolve it
# relative to the script so the server WORKDIR doesn't affect alembic.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "[entrypoint] applying database migrations: alembic upgrade head"
alembic -c "${SCRIPT_DIR}/alembic.ini" upgrade head
echo "[entrypoint] migrations applied; starting: $*"

exec "$@"
