# PLC Integrated Dashboard

A web dashboard for PLCs. PLCs publish JSON over MQTT. The system validates and stores the data, shows live values and trends, and raises alarms. It deploys to Google Cloud as a low-cost demo: a single VM for about USD 20–25/month.

```
PLC ──MQTT/TLS :8883──▶ Mosquitto ──▶ ingestor ──▶ TimescaleDB
                            │             └──app/live──┐      │
                            └────────────────────────▶ api ◀──┘ ──▶ Caddy (HTTPS) ──▶ browser
```

| Component | What it does |
|---|---|
| **Mosquitto 2.0 + go-auth** | MQTT broker. Every login and topic permission is checked against PostgreSQL (`mqtt_accounts`, `mqtt_acls`); the dashboard creates and rotates device logins there. |
| **ingestor** (Python) | Validates messages against [`schemas/telemetry.v1.json`](schemas/telemetry.v1.json), applies tag scaling, writes to TimescaleDB in batches, publishes live events, and evaluates alarm rules. |
| **TimescaleDB** | Stores raw values (30 days, compressed) plus 1-minute rollups (1 year) and 1-hour rollups (5 years). |
| **api** (FastAPI) | REST and WebSocket API: login and roles, devices and credentials, history, CSV export, alarms, users, and audit log. |
| **web** (React + ECharts) | Asset console. **Operations:** plant overview, live view, trends & historian, alarms & events. **Analytics:** performance & downtime, process quality (SPC), asset health. **Platform:** data quality & link, asset configuration, raw data, system health. Plus admin pages. Served by Caddy with automatic HTTPS. |
| **Logs** | Every container's output goes to Google Cloud Logging on GCP (`deploy/compose.gcp.yml`) and is searchable by admins under **Admin → Logs**. |
| **Web simulator** | Admin-only test site (`sim.<host>`): send custom or auto-generated messages, single or bulk, to a topic of your choice under `sim/`, `plc/` or `test/`. |

Documents:
- [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md): requirements and architecture.
- [docs/PLC_INTEGRATION.md](docs/PLC_INTEGRATION.md): guide for the PLC programmer.
- [docs/OPERATIONS.md](docs/OPERATIONS.md): how to reach every component on GCP (VM, database, broker, logs, backups).
- [docs/OPEN_POINTS.md](docs/OPEN_POINTS.md): decisions parked for later.

## Run locally

Requires Docker with the Compose plugin.

```bash
cp .env.example .env
docker compose up -d --build
```

- Dashboard: http://localhost:8080. Log in as `admin@example.com` / `admin12345` (set in `.env`).
- MQTT: `localhost:1883` for internal/dev use, and `localhost:8883` over TLS.
- Web simulator: http://localhost:8081 (admin login).
- Database: host `127.0.0.1` (not `localhost`: it is published on IPv4 only), port `5432`, database/user `plc`, password `plc-dev-password`.
- **Make local data match GCP:** run `./scripts/sync-from-gcp.sh`. It copies the GCP database (devices, register maps, history, rules, users) into the local stack, after backing up the local data to `backups/`. GCP stays the source of truth: the real PLC sends only there, so rerun the script to refresh.
- For load tests there is also a command-line simulator, [simulator/simulate.py](simulator/simulate.py), which connects like a real PLC.

Run the end-to-end check (covers ingestion, ACLs, history, alarms, the WebSocket, roles, the simulator, tag limits → alarm rules, and the asset analytics endpoints):

```bash
docker compose exec -T api python - < scripts/e2e_check.py
```

Run the unit tests:

```bash
cd services/ingestor && python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/pytest
cd services/api      && python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/pytest
```

To develop the frontend, keep the stack running and use `cd web && npm install && npm run dev` (Node 18+, see `web/.nvmrc`). Vite proxies `/api` to `localhost:8080`. If your machine has an older Node (e.g. 16), use `npm run dev:node20` / `npm run build:node20`: they fetch Node 20 into the npm cache for this project only. If `npm run build` reports a missing `@rollup/rollup-<platform>` module, delete `web/node_modules` and run `npm install` again.

## Deploy to GCP (demo)

Prerequisites: a GCP project with billing, plus `gcloud` (logged in) and `terraform` installed.

### 1. Access setup (once, as the project owner)

[`infra/bootstrap`](infra/bootstrap) creates three identities, so nobody needs the owner login:

| Identity | Used by | Can |
|---|---|---|
| `plc-deployer@…` service account | Terraform, `deploy.sh`, CI | Manage VMs, IPs, firewall, snapshots; SSH through IAP. **Cannot change IAM.** |
| `plc-runtime@…` service account | Application code (attached to the VM) | Only the APIs the code calls (logs and metrics by default) |
| People listed as `operators` / `viewers` | Their own Google accounts | Operators: read-only console + impersonate the deployer. Viewers: read-only console. |

No keys are created. Operators get short-lived deployer credentials by impersonating it with their own login, so audit logs name the person.

```bash
cd infra/bootstrap
cp terraform.tfvars.example terraform.tfvars   # project_id + operators/viewers emails
terraform init && terraform apply
```

To add or remove a person or a role later, edit `terraform.tfvars` and apply again. Grant the runtime account more APIs through `runtime_roles`, and the deployer more services through `deployer_roles`.

### 2. Infrastructure and deployment (any operator)

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # project_id + the two service accounts from step 1
terraform init && terraform apply              # runs as the deployer

cd ../..
./scripts/deploy.sh                            # dashboard + web simulator
```

Terraform creates:
- an `e2-small` Debian VM in `asia-south1`,
- a static IP,
- firewall rules: 80/443/8883 public, SSH only through IAP,
- daily disk snapshots.

`deploy.sh` does the rest:
- Keeps all secrets in **GCP Secret Manager**, generating any that are missing (first run: prints the initial admin password once).
- Keeps non-secret settings in `deploy/gcp-settings.env` (safe to commit).
- Copies the source to the VM and runs `docker compose up -d --build`.

The dashboard is then at `https://<ip-with-dashes>.sslip.io`. That host name resolves to the VM's IP without any DNS setup, and Caddy obtains a Let's Encrypt certificate for it. The PLCs connect to the same host name on port 8883.

To use your own domain, point a DNS A record at the static IP. Then set `SITE_ADDRESS` and `MQTT_PUBLIC_HOST` in `deploy/gcp-settings.env` and deploy again. The MQTT certificate is regenerated automatically; PLCs keep the same CA.

To run operations on the VM:

```bash
gcloud compute ssh plc-dashboard --zone asia-south1-a --tunnel-through-iap
cd /opt/plc-dashboard && sudo docker compose logs -f ingestor
```

## Onboarding a real PLC

1. **Admin → Devices → Add device.** Choose the device ID, site, and line. The dialog shows the password once, along with every connection detail. Use **Copy all details for the PLC programmer**.
2. Give the programmer [docs/PLC_INTEGRATION.md](docs/PLC_INTEGRATION.md) and the CA certificate (**Download CA certificate**).
3. When data arrives, open **Asset configuration** for the asset (pick it in the top bar):
   - map the Modbus registers (**Register map**),
   - in **Tag mapping & limits**, give each tag a name, unit, normal range and warning/critical limits, and assign the **roles** the analytics use: machine status, speed, motor current, steam pressure, moisture, vibration. Limits become alarm rules automatically (3 s on/off delay, held while the machine is stopped unless you untick it). Guidance text is shown to the operator with the alarm.
   - fill in **Connection** and **Analytics settings**: where running time comes from (`auto` = status register unless it contradicts speed), the comms timeout (default 15 s; gateways that send in bursts need more than the burst period), and an optional speed target.
4. Work through the **Commissioning checklist** on **Data quality & link**, including the float byte order (the raw register table shows what each value would read as in other orders).
5. Extra alarm rules (e.g. offline, fault, machine stopped) go under **Admin → Alarm rules**.

## Repository layout

```
docs/                 requirements, PLC integration guide
schemas/              telemetry JSON Schema + example payloads
services/api/         FastAPI app, DB migrations, bootstrap (certs, broker ACLs, admin user)
services/ingestor/    MQTT → validation → TimescaleDB, alarm engine
web/                  React SPA
simulator/            PLC simulator
deploy/               Mosquitto config, Caddyfile + web image
infra/terraform/      GCP VM, static IP, firewall, snapshots
scripts/              deploy.sh, e2e_check.py
docker-compose.yml    the whole stack (local and on the VM)
```

## Scaling beyond the demo

The MQTT topics, JSON schema, and REST API stay stable. [REQUIREMENTS.md §5.4](docs/REQUIREMENTS.md) describes the managed-services target: EMQX on GKE, Pub/Sub, Cloud Run, BigQuery, and IAP. If you need more capacity before that migration, a larger VM (`machine_type = "e2-medium"`) is the first step.
