#!/usr/bin/env bash
# Open secure tunnels from this laptop to the database and MQTT broker on the GCP VM.
#
#   ./scripts/tunnel.sh          # keep this terminal open; Ctrl+C closes the tunnels
#
# Then connect your tools to:
#   PostgreSQL (DBeaver/pgAdmin):  localhost:15432   db=plc  user=plc    password=DB_PASSWORD
#   MQTT (MQTT Explorer/MQTTX):    localhost:11883   no TLS  user=admin  password=MQTT_ADMIN_PASSWORD
# (passwords come from GCP Secret Manager: plc-db-password, plc-mqtt-admin-password)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TF="$ROOT/infra/terraform"
tf_out() { terraform -chdir="$TF" output -raw "$1"; }

PROJECT="$(tf_out project_id)"
ZONE="$(tf_out zone)"
INSTANCE="$(tf_out instance_name)"
DEPLOYER="$(tf_out deployer_service_account)"

ARGS=(--project "$PROJECT")
[ -n "$DEPLOYER" ] && ARGS+=(--impersonate-service-account "$DEPLOYER")

# Passwords are read from Secret Manager (as the deployer); nothing is stored locally.
env_value() {
  case "$1" in
    DB_PASSWORD) name=plc-db-password ;;
    MQTT_ADMIN_PASSWORD) name=plc-mqtt-admin-password ;;
  esac
  gcloud "${ARGS[@]}" secrets versions access latest --secret="$name" 2>/dev/null || echo "<see Secret Manager: $name>"
}
cat <<INFO
Opening tunnels to $INSTANCE (Ctrl+C to close)...

  PostgreSQL  host=localhost port=15432 database=plc user=plc password=$(env_value DB_PASSWORD)
  MQTT        host=localhost port=11883 (no TLS) user=admin password=$(env_value MQTT_ADMIN_PASSWORD)
              subscribe to  plc/#  (raw PLC traffic)  and  app/#  (processed)

INFO
exec gcloud "${ARGS[@]}" compute ssh "$INSTANCE" --zone "$ZONE" --tunnel-through-iap --quiet -- \
  -N -o ExitOnForwardFailure=yes -L 15432:localhost:5432 -L 11883:localhost:1883
