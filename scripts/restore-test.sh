#!/usr/bin/env bash
#
# Prove a StockBrain backup is restorable, without touching the live database.
#
# A backup nobody has ever restored is a hypothesis. This restores one into a
# throwaway database, counts the rows that matter, checks the schema revision
# against the migration head, and drops it again.
#
# Usage:
#   scripts/restore-test.sh backups/stockbrain-20260905T120000Z.dump
#
# The temporary database is dropped on exit, including on failure, so a repeated
# run never collides with a leftover from a previous one.
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# How to reach the PostgreSQL container. From a repository checkout the compose
# project answers; on TrueNAS the app is a pasted compose file with no checkout,
# so name the container instead (`docker ps` shows it):
#   STOCKBRAIN_PG_CONTAINER=ix-stockbrain-postgres-1 scripts/backup.sh /mnt/tank/apps/stockbrain/backups
pg_exec() {
  if [[ -n "${STOCKBRAIN_PG_CONTAINER:-}" ]]; then
    docker exec -i "${STOCKBRAIN_PG_CONTAINER}" "$@"
  else
    (cd "${REPO_ROOT}" && docker compose exec -T postgres "$@")
  fi
}
DUMP="${1:-}"
DB_USER="${POSTGRES_USER:-stockbrain}"
TEST_DB="stockbrain_restore_$$"

if [[ -z "${DUMP}" || ! -f "${DUMP}" ]]; then
  echo "usage: scripts/restore-test.sh <dump-file>" >&2
  exit 2
fi

cd "${REPO_ROOT}"

psql_admin() {
  pg_exec psql -U "${DB_USER}" -d postgres -v ON_ERROR_STOP=1 "$@"
}
psql_test() {
  pg_exec psql -U "${DB_USER}" -d "${TEST_DB}" -tA -v ON_ERROR_STOP=1 "$@"
}

cleanup() {
  # `|| true`: cleanup must never be the thing that fails the script and hides
  # the real error.
  psql_admin -c "DROP DATABASE IF EXISTS ${TEST_DB};" > /dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> creating ${TEST_DB}"
psql_admin -c "CREATE DATABASE ${TEST_DB} OWNER ${DB_USER};" > /dev/null

echo "==> restoring ${DUMP}"
# `--exit-on-error` so a partially restored database is never reported as a
# successful restore. `--no-owner`/`--no-privileges` because the dump was taken
# the same way and the target role is the same one.
pg_exec \
  pg_restore -U "${DB_USER}" -d "${TEST_DB}" --no-owner --no-privileges --exit-on-error \
  < "${DUMP}"

echo "==> verifying"

# 1. The schema is at a revision the code recognises. A restore that lands on an
#    older revision is restorable but not *runnable*, and the difference matters
#    at 3am.
RESTORED_REVISION="$(psql_test -c "SELECT version_num FROM alembic_version;")"
HEAD_REVISION="$(cd backend && .venv/bin/alembic heads 2>/dev/null | awk '{print $1}' | head -1)"
echo "    schema revision: ${RESTORED_REVISION} (code head: ${HEAD_REVISION:-unknown})"
if [[ -n "${HEAD_REVISION}" && "${RESTORED_REVISION}" != "${HEAD_REVISION}" ]]; then
  echo "    note: the dump predates the current code; 'alembic upgrade head' would be needed"
fi

# 2. The tables whose loss cannot be reconstructed from anywhere else. Sources
#    and events can, in principle, be re-ingested; an execution attempt and its
#    outcome cannot -- it is the only record that an order may have been placed.
for table in \
  execution_attempts \
  broker_orders \
  trade_proposals \
  risk_evaluations \
  approval_actions \
  theses \
  research_runs \
  events \
  sources \
  llm_calls \
  provider_calls \
  firecrawl_calls \
  app_settings
do
  # A dump taken before a table existed is a valid dump. Reporting "absent" is
  # the honest answer; erroring out would make an older backup look corrupt.
  EXISTS="$(psql_test -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name='${table}';")"
  if [[ "${EXISTS}" != "1" ]]; then
    printf '    %-22s absent (predates this table)\n' "${table}"
    continue
  fi
  COUNT="$(psql_test -c "SELECT count(*) FROM ${table};")"
  printf '    %-22s %s rows\n' "${table}" "${COUNT}"
done

# 3. The invariants that make the safety claims true. A restore that dropped
#    `uq_execution_attempts_sent_once` would look fine and would permit a second
#    transmitted attempt for one proposal.
echo "==> checking critical constraints survived the round trip"
MISSING=0
for index in \
  uq_execution_attempts_sent_once \
  uq_trade_proposals_active_instrument \
  uq_trade_proposals_active_thesis \
  uq_approval_actions_opaque_token_hash \
  uq_jobs_dedupe_key_active \
  uq_notifications_dedupe_key \
  uq_sources_provider_item \
  uq_sources_canonical_url_hash
do
  PRESENT="$(psql_test -c "SELECT count(*) FROM pg_indexes WHERE indexname = '${index}';")"
  if [[ "${PRESENT}" != "1" ]]; then
    echo "    MISSING: ${index}" >&2
    MISSING=$((MISSING + 1))
  else
    printf '    %-40s ok\n' "${index}"
  fi
done

# 4. And the ambiguous-execution question, which is the first thing to ask after
#    any restore: does this database contain an order whose fate is unknown?
AMBIGUOUS="$(psql_test -c "SELECT count(*) FROM execution_attempts WHERE ambiguous AND reconciled_at IS NULL;" 2>/dev/null || echo 0)"
UNRESOLVED="$(psql_test -c "SELECT count(*) FROM execution_attempts WHERE sent_to_broker AND outcome IN ('PENDING','AMBIGUOUS');" 2>/dev/null || echo 0)"
echo "==> ambiguous, unreconciled attempts in this backup: ${AMBIGUOUS}"
echo "==> transmitted, unresolved attempts in this backup:  ${UNRESOLVED}"
if [[ "${UNRESOLVED}" != "0" ]]; then
  echo "    Restoring this backup means reconciling those against the broker BEFORE"
  echo "    enabling execution. See docs/operations.md, 'Disaster recovery'."
fi

if [[ "${MISSING}" -gt 0 ]]; then
  echo "!!! ${MISSING} critical index/constraint missing after restore" >&2
  exit 1
fi

echo "==> restore verified"
