#!/usr/bin/env bash
# Deploy (or update) the stack on the GCP VM created by infra/terraform.
#
#   cd infra/terraform && terraform apply      # once
#   ./scripts/deploy.sh                         # every release (dashboard + web simulator)
#
# Requires: gcloud (authenticated), terraform, tar. Runs as the deployer service account
# when infra/terraform sets deployer_service_account. SSH goes through IAP, so the
# VM needs no public SSH port.
#
# Secrets live only in GCP Secret Manager (see deploy/secrets.list); missing ones are
# generated here. Non-secret settings live in deploy/gcp-settings.env, which is safe to commit.
# On the VM, deploy/fetch-secrets.sh combines both into a root-only .env.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TF="$ROOT/infra/terraform"
SETTINGS="$ROOT/deploy/gcp-settings.env"
LEGACY_ENV="$ROOT/deploy/.env.demo"   # pre-Secret-Manager file: migrated, then deleted
SECRETS_LIST="$ROOT/deploy/secrets.list"
REMOTE_DIR=/opt/plc-dashboard

tf_out() { terraform -chdir="$TF" output -raw "$1"; }
PROJECT="$(tf_out project_id)"
REGION="$(tf_out zone | sed 's/-[a-z]$//')"
ZONE="$(tf_out zone)"
INSTANCE="$(tf_out instance_name)"
IP="$(tf_out external_ip)"
HOST="$(tf_out default_hostname)"
DEPLOYER="$(tf_out deployer_service_account)"
GCLOUD=(gcloud --project "$PROJECT")
if [ -n "$DEPLOYER" ]; then
  # Act as the deployer service account (your login needs Token Creator on it; see infra/bootstrap).
  GCLOUD+=(--impersonate-service-account "$DEPLOYER")
fi
SSH=("${GCLOUD[@]}" compute ssh "$INSTANCE" --zone "$ZONE" --tunnel-through-iap --quiet)

random_secret() { openssl rand -base64 48 | tr -d '/+=\n' | cut -c1-"${1:-32}"; }
secret_exists() { "${GCLOUD[@]}" secrets describe "$1" >/dev/null 2>&1 </dev/null; }
create_secret() {  # name value  (stored in the VM's region only)
  printf '%s' "$2" | "${GCLOUD[@]}" secrets create "$1" --replication-policy=user-managed \
    --locations="$REGION" --labels=app=plc-dashboard --data-file=- >/dev/null
}
secret_rows() { grep -vE '^\s*(#|$)' "$SECRETS_LIST"; }
legacy_value() { [ -f "$LEGACY_ENV" ] && grep "^$1=" "$LEGACY_ENV" | head -1 | cut -d= -f2- || true; }

# Renamed from deploy/demo.env.
[ -f "$ROOT/deploy/demo.env" ] && [ ! -f "$SETTINGS" ] && mv "$ROOT/deploy/demo.env" "$SETTINGS"

FRESH_INSTALL=false
[ -f "$SETTINGS" ] || [ -f "$LEGACY_ENV" ] || FRESH_INSTALL=true

# ---------------------------------------------------------------- settings (not secret)
if [ ! -f "$SETTINGS" ]; then
  if [ -f "$LEGACY_ENV" ]; then
    echo "==> Moving settings from deploy/.env.demo to deploy/gcp-settings.env"
    SECRET_VARS="$(secret_rows | awk '{print $1}' | paste -sd'|' -)"
    { echo "# Non-secret deployment settings (safe to commit). Secrets: GCP Secret Manager, see deploy/secrets.list."
      grep -vE "^\s*#|^\s*$|^(${SECRET_VARS}|MOSQUITTO_DYNSEC_PASSWORD|OPERATOR_PASSWORD|OPERATOR_EMAIL|SIM_DEVICES|SIM_INTERVAL|SIM_INVALID_RATE)=" "$LEGACY_ENV"
    } > "$SETTINGS"
  else
    echo "==> Writing settings to deploy/gcp-settings.env"
    cat > "$SETTINGS" <<EOF
# Non-secret deployment settings (safe to commit). Secrets: GCP Secret Manager, see deploy/secrets.list.
# Set SITE_ADDRESS / MQTT_PUBLIC_HOST to your own domain (DNS A record -> $IP) if you have one.
ADMIN_EMAIL=${ADMIN_EMAIL:-admin@example.com}
SITE_ADDRESS=$HOST
MQTT_PUBLIC_HOST=$HOST
MQTT_EXTRA_SANS=$IP
ACME_EMAIL=${ACME_EMAIL:-admin@example.com}
HTTP_PORT=80
HTTPS_PORT=443
COOKIE_SECURE=true
DB_MEMORY=512MB
EOF
  fi
fi

# Settings added after the first deploy: append any that are missing (existing values are kept).
ensure_setting() {
  grep -q "^$1=" "$SETTINGS" || { echo "$1=$2" >> "$SETTINGS"; echo "    added $1 to deploy/gcp-settings.env"; }
}
SITE="$(grep '^SITE_ADDRESS=' "$SETTINGS" | cut -d= -f2-)"
ensure_setting SIM_SITE_ADDRESS "sim.$SITE"
ensure_setting DASHBOARD_URL "https://$SITE"
ensure_setting SIMULATOR_URL "https://sim.$SITE"
ensure_setting SIM_DEVICE_TTL_MIN 30
ensure_setting COOKIE_DOMAIN "$SITE"
ensure_setting COMPOSE_FILE "docker-compose.yml:deploy/compose.gcp.yml"   # container logs -> Cloud Logging
SIM_URL="$(grep '^SIMULATOR_URL=' "$SETTINGS" | cut -d= -f2-)"

# ---------------------------------------------------------------- secrets (Secret Manager)
echo "==> Checking secrets in Secret Manager ($PROJECT)"
while read -r var name flags; do
  secret_exists "$name" && continue
  value="$(legacy_value "$var")"
  [ "$var" = "MQTT_ADMIN_PASSWORD" ] && [ -z "$value" ] && value="$(legacy_value MOSQUITTO_DYNSEC_PASSWORD)"
  if [ -n "$value" ]; then
    create_secret "$name" "$value"
    echo "    moved $var into secret $name"
  elif [ "${flags:-}" = "optional" ]; then
    if $FRESH_INSTALL && [ "$var" = "ADMIN_PASSWORD" ]; then
      value="$(random_secret 16)"
      create_secret "$name" "$value"
      echo "    first admin login: $(grep '^ADMIN_EMAIL=' "$SETTINGS" | cut -d= -f2-) / $value  (change it after signing in)"
    fi
  else
    if [ -f "$LEGACY_ENV" ] || ! $FRESH_INSTALL; then
      case "$var" in DB_PASSWORD)
        echo "ERROR: secret $name is missing but the database already exists; restore it before deploying." >&2
        exit 1 ;;
      esac
    fi
    create_secret "$name" "$(random_secret 48)"
    echo "    generated secret $name"
  fi
done < <(secret_rows)

if [ -f "$LEGACY_ENV" ]; then
  # Every required secret now exists in Secret Manager: the local copy is no longer needed.
  rm -f "$LEGACY_ENV"
  echo "    deleted deploy/.env.demo (secrets are now only in Secret Manager)"
fi

echo "==> Waiting for VM bootstrap (Docker install)"
for _ in $(seq 1 60); do
  if "${SSH[@]}" --command "test -f /var/run/plc-dashboard-ready && command -v docker" >/dev/null 2>&1; then
    break
  fi
  sleep 10
done

echo "==> Packaging source"
BUNDLE="$(mktemp -t plc-dashboard.XXXXXX).tgz"
trap 'rm -f "$BUNDLE"' EXIT
tar -C "$ROOT" -czf "$BUNDLE" \
  --exclude=node_modules --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache \
  --exclude=dist --exclude=.git --exclude=.terraform --exclude='*.tfstate*' --exclude='.env*' \
  docker-compose.yml deploy services schemas simulator web

echo "==> Uploading to $INSTANCE ($IP)"
"${GCLOUD[@]}" compute scp "$BUNDLE" "$INSTANCE:/tmp/" --zone "$ZONE" --tunnel-through-iap --quiet

echo "==> Starting stack"
"${SSH[@]}" --command "sudo bash -s" <<EOF
set -euo pipefail
cd $REMOTE_DIR
find . -mindepth 1 -maxdepth 1 ! -name '.env' -exec rm -rf {} +
tar -xzf /tmp/$(basename "$BUNDLE")
rm -f /tmp/$(basename "$BUNDLE")
bash deploy/fetch-secrets.sh
docker compose up -d --build --remove-orphans
# Restart the broker if the TLS certificate was (re)generated during this deploy.
if docker compose run --rm --no-deps --entrypoint sh certs -c 'test -f /certs/.reload && rm /certs/.reload'; then
  docker compose restart mosquitto
fi
docker image prune -f >/dev/null
docker compose ps --format 'table {{.Service}}\t{{.Status}}'
EOF

cat <<EOF

==> Deployed
    Dashboard:  https://$HOST   (first HTTPS request may take ~30s while the certificate is issued)
    Simulator:  $SIM_URL
    MQTT (TLS): $HOST:8883      (CA certificate: Admin -> Devices -> Download CA certificate)
    Settings:   deploy/gcp-settings.env   (no secrets; safe to commit)
    Secrets:    GCP Secret Manager, project $PROJECT (see deploy/secrets.list and docs/OPERATIONS.md)
EOF
