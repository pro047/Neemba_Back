#!/bin/sh
# 30-day retention sweep for the monitor tables (Phase 7).
#
# Scheduled DELETE sidecar (no schema change, low risk): periodically prunes
# rows older than the retention window from app.translations and app.sessions.
# Matches the repo's certbot/reloader sidecar idiom (alpine + while-loop).
#
# Rules:
#   * app.translations — delete by created_at < now() - <days>.
#   * app.sessions     — delete only ENDED sessions (ended_at IS NOT NULL) whose
#                        ended_at is older than the window. Live sessions
#                        (ended_at IS NULL) are NEVER deleted regardless of age.
#   * No FK between the tables (plan §5), so delete order is irrelevant.
#   * Re-running is idempotent — a second pass matches no extra rows.
#
# The to_regclass() guards make the sweep a no-op (not an error) before the
# python service has run its alembic migration to create the app schema.
#
# Env (from .env.prod via compose env_file):
#   POSTGRES_HOST (default: postgres), POSTGRES_PORT (default: 5432),
#   POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DATABASE
# Tunables:
#   RETENTION_DAYS            (default: 30)
#   RETENTION_SLEEP_INTERVAL  (default: 24h)  sleep between sweeps
#   RETENTION_ONESHOT         (default: 0)    set to 1 to run once and exit
set -eu

PGHOST="${POSTGRES_HOST:-postgres}"
PGPORT="${POSTGRES_PORT:-5432}"
DAYS="${RETENTION_DAYS:-30}"
INTERVAL="${RETENTION_SLEEP_INTERVAL:-24h}"
ONESHOT="${RETENTION_ONESHOT:-0}"
export PGPASSWORD="${POSTGRES_PASSWORD}"

trap 'echo "pg-retention: terminating"; exit 0' TERM INT

sweep() {
  psql -v ON_ERROR_STOP=1 -X -q \
    -h "$PGHOST" -p "$PGPORT" -U "$POSTGRES_USER" -d "$POSTGRES_DATABASE" <<SQL
DO \$retention\$
DECLARE
  t_deleted bigint := 0;
  s_deleted bigint := 0;
BEGIN
  IF to_regclass('app.translations') IS NOT NULL THEN
    DELETE FROM app.translations
      WHERE created_at < now() - interval '${DAYS} days';
    GET DIAGNOSTICS t_deleted = ROW_COUNT;
  END IF;
  IF to_regclass('app.sessions') IS NOT NULL THEN
    DELETE FROM app.sessions
      WHERE ended_at IS NOT NULL
        AND ended_at < now() - interval '${DAYS} days';
    GET DIAGNOSTICS s_deleted = ROW_COUNT;
  END IF;
  RAISE NOTICE 'pg-retention: deleted % translations, % sessions (window=% days)',
    t_deleted, s_deleted, ${DAYS};
END
\$retention\$;
SQL
}

echo "pg-retention: starting (window=${DAYS}d, interval=${INTERVAL}, oneshot=${ONESHOT})"

if [ "$ONESHOT" = "1" ]; then
  sweep
  echo "pg-retention: oneshot sweep done at $(date -u +%FT%TZ)"
  exit 0
fi

while :; do
  if sweep; then
    echo "pg-retention: sweep done at $(date -u +%FT%TZ)"
  else
    echo "pg-retention: sweep failed at $(date -u +%FT%TZ) (will retry next cycle)" >&2
  fi
  sleep "$INTERVAL" &
  wait $!
done
