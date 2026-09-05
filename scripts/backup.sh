#!/usr/bin/env bash
#
# StockBrain PostgreSQL backup.
#
# The database is the entire audit trail: every event, every classification,
# every research run, every risk evaluation, every authorization and -- the part
# that cannot be reconstructed -- every execution attempt and its outcome. A ZFS
# snapshot of a running PostgreSQL data directory is a crash-consistent copy,
# which is usually recoverable and is not a backup. This is.
#
# Usage:
#   scripts/backup.sh [DESTINATION_DIR]
#
# Defaults to ./backups. Reads POSTGRES_PASSWORD from .env, like the Makefile.
#
# Format: pg_dump -Fc (custom). Compressed, and restorable selectively with
# pg_restore, which plain SQL is not.
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${1:-${REPO_ROOT}/backups}"
RETAIN_DAYS="${BACKUP_RETAIN_DAYS:-14}"
DB_NAME="${POSTGRES_DB:-stockbrain}"
DB_USER="${POSTGRES_USER:-stockbrain}"

mkdir -p "${DEST}"

# UTC in the filename, always. A backup set named in local time is a backup set
# with two files claiming the same hour twice a year.
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="${DEST}/stockbrain-${STAMP}.dump"

echo "==> dumping ${DB_NAME} to ${TARGET}"

# `docker compose exec -T` so this works unattended from cron. `pg_dump` runs
# *inside* the container, so the host needs no PostgreSQL client and no
# published database port -- which is why compose does not publish one.
cd "${REPO_ROOT}"
docker compose exec -T postgres \
  pg_dump -U "${DB_USER}" -d "${DB_NAME}" --format=custom --no-owner --no-privileges \
  > "${TARGET}.partial"

# Renamed only after a clean exit, so a truncated dump is never mistaken for a
# good one by the restore script or by the retention sweep below.
mv "${TARGET}.partial" "${TARGET}"

# Verify the archive is readable before reporting success. `pg_restore -l`
# parses the table of contents; a dump that cannot be listed cannot be restored,
# and finding that out now is the entire point.
if ! docker compose exec -T postgres pg_restore -l < "${TARGET}" > /dev/null; then
  echo "!!! ${TARGET} is not a readable archive; keeping it for inspection" >&2
  exit 1
fi

SIZE="$(du -h "${TARGET}" | cut -f1)"
COUNT="$(docker compose exec -T postgres pg_restore -l < "${TARGET}" | grep -c '^[0-9]' || true)"
echo "==> ok: ${SIZE}, ${COUNT} archive entries"

# Retention. Only files matching the generated name are considered, so nothing
# else in the directory is ever deleted.
if [[ "${RETAIN_DAYS}" -gt 0 ]]; then
  echo "==> pruning dumps older than ${RETAIN_DAYS} days"
  find "${DEST}" -maxdepth 1 -name 'stockbrain-*.dump' -type f \
    -mtime "+${RETAIN_DAYS}" -print -delete
fi

# A backup nobody has ever restored is a hypothesis. `scripts/restore-test.sh`
# proves this file works, into a throwaway database, without touching the live
# one.
echo "==> verify it: scripts/restore-test.sh ${TARGET}"
