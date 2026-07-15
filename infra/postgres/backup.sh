#!/bin/sh
# Daily pg_dump → S3 backup sidecar.
#
# pg-retention deletes rows older than 30 days, so the DB itself is NOT a
# backup — a lost volume means total data loss. This sidecar ships a daily
# custom-format dump (pg_dump -Fc, compressed, pg_restore-able) to S3, where
# a lifecycle rule expires objects under pg/ after 90 days.
#
# Credentials: none in env. The EC2 instance has the neemba-ec2-role instance
# profile attached; the aws cli picks up temporary credentials from IMDS
# (instance metadata, hop limit 2 so containers can reach it). The role only
# allows s3:PutObject/ListBucket on the pg/ prefix of the backup bucket.
#
# Matches the pg-retention sidecar idiom (alpine + while-loop + oneshot).
#
# Env (from .env.prod via compose env_file):
#   POSTGRES_HOST (default: postgres), POSTGRES_PORT (default: 5432),
#   POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DATABASE
# Env (set in compose, not secret):
#   BACKUP_BUCKET             S3 bucket name (required)
#   AWS_DEFAULT_REGION        region for the aws cli
# Tunables:
#   BACKUP_SLEEP_INTERVAL  (default: 24h)  sleep between backups
#   BACKUP_ONESHOT         (default: 0)    set to 1 to run once and exit
set -eu

PGHOST="${POSTGRES_HOST:-postgres}"
PGPORT="${POSTGRES_PORT:-5432}"
INTERVAL="${BACKUP_SLEEP_INTERVAL:-24h}"
ONESHOT="${BACKUP_ONESHOT:-0}"
: "${BACKUP_BUCKET:?BACKUP_BUCKET is required}"
export PGPASSWORD="${POSTGRES_PASSWORD}"

trap 'echo "pg-backup: terminating"; exit 0' TERM INT

backup() {
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  dump="/tmp/${POSTGRES_DATABASE}-${stamp}.dump"
  # -Fc: custom format — compressed and restorable table-by-table with
  # pg_restore (a plain .sql dump would need the whole file replayed).
  pg_dump -Fc -h "$PGHOST" -p "$PGPORT" -U "$POSTGRES_USER" \
    -d "$POSTGRES_DATABASE" -f "$dump"
  aws s3 cp --only-show-errors "$dump" \
    "s3://${BACKUP_BUCKET}/pg/$(basename "$dump")"
  rm -f "$dump"
  echo "pg-backup: uploaded $(basename "$dump")"
}

echo "pg-backup: starting (bucket=${BACKUP_BUCKET}, interval=${INTERVAL}, oneshot=${ONESHOT})"

if [ "$ONESHOT" = "1" ]; then
  backup
  echo "pg-backup: oneshot backup done at $(date -u +%FT%TZ)"
  exit 0
fi

while :; do
  if backup; then
    echo "pg-backup: backup done at $(date -u +%FT%TZ)"
  else
    echo "pg-backup: backup failed at $(date -u +%FT%TZ) (will retry next cycle)" >&2
  fi
  sleep "$INTERVAL" &
  wait $!
done
