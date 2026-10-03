#!/usr/bin/env bash
# Make the LOCAL stack's data match GCP: copies the GCP database (devices, register maps,
# tags, history, alarm rules, alarms, users) into the local Docker database.
#
#   ./scripts/sync-from-gcp.sh
#
# GCP is the source of truth: the real PLC publishes only there, so local data is a snapshot
# from the moment you run this. The current local database is backed up to backups/ first.
# Local service passwords (MQTT accounts of ingestor/simulator/admin, broker DB login) are
# re-applied afterwards, so the local stack keeps working with its own .env.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TF="$ROOT/infra/terraform"
cd "$ROOT"
tf_out() { terraform -chdir="$TF" output -raw "$1"; }
PROJECT="$(tf_out project_id)"
ZONE="$(tf_out zone)"
INSTANCE="$(tf_out instance_name)"
DEPLOYER="$(tf_out deployer_service_account)"
ARGS=(--project "$PROJECT")
[ -n "$DEPLOYER" ] && ARGS+=(--impersonate-service-account "$DEPLOYER")
TS_IMAGE="timescale/timescaledb:2.30.1-pg16"   # same version as both databases
PORT=15433

mkdir -p backups
STAMP="$(date +%Y%m%d-%H%M%S)"
LOCAL_BACKUP="backups/local-before-sync-$STAMP.dump"
GCP_DUMP="backups/gcp-$STAMP.dump"

echo "==> Backing up the local database to $LOCAL_BACKUP"
docker compose exec -T db pg_dump -U plc -Fc plc > "$LOCAL_BACKUP"

echo "==> Opening a tunnel to the GCP database"
gcloud "${ARGS[@]}" compute ssh "$INSTANCE" --zone "$ZONE" --tunnel-through-iap --quiet -- \
  -N -o ExitOnForwardFailure=yes -L "$PORT:localhost:5432" >/dev/null 2>&1 &
TUNNEL=$!
trap 'kill $TUNNEL 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do nc -z 127.0.0.1 "$PORT" 2>/dev/null && break; sleep 1; done
GCP_PW="$(gcloud "${ARGS[@]}" secrets versions access latest --secret=plc-db-password)"

echo "==> Dumping the GCP database to $GCP_DUMP"
docker run --rm -e PGPASSWORD="$GCP_PW" "$TS_IMAGE" \
  pg_dump -h host.docker.internal -p "$PORT" -U plc -Fc plc 2>/dev/null > "$GCP_DUMP"
kill $TUNNEL 2>/dev/null || true
echo "    $(du -h "$GCP_DUMP" | cut -f1)"

echo "==> Restoring into the local database"
docker compose stop api ingestor simulator-ui mosquitto web >/dev/null 2>&1
docker compose exec -T db psql -U plc -d postgres -q -v ON_ERROR_STOP=1 <<'SQL'
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'plc' AND pid <> pg_backend_pid();
DROP DATABASE plc;
CREATE DATABASE plc;
SQL
docker compose exec -T db psql -U plc -d plc -q -c "CREATE EXTENSION IF NOT EXISTS timescaledb" -c "SELECT timescaledb_pre_restore()" >/dev/null
docker compose exec -T db pg_restore -U plc -d plc --no-owner --no-privileges < "$GCP_DUMP" 2>&1 \
  | grep -vE "already exists|timescaledb|^$" | head -5 || true
docker compose exec -T db psql -U plc -d plc -q -c "SELECT timescaledb_post_restore()" >/dev/null

echo "==> Re-applying local service settings and starting the stack"
docker compose up -d >/dev/null 2>&1    # init re-runs: local MQTT service passwords, broker DB login
docker compose ps --format '  {{.Service}}\t{{.Status}}'

echo
echo "Local now matches GCP as of $STAMP."
echo "  Previous local data: $LOCAL_BACKUP   (restore: docker compose exec -T db pg_restore -U plc -d plc --clean < FILE)"
echo "  Logins are the GCP ones. Live PLC data keeps arriving only on GCP; rerun this script to refresh."
