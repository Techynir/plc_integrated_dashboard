# Operations Guide

How to reach every part of the running system on GCP.

Throughout this guide, `<ip>` is `34-93-142-194` and the VM is `plc-dashboard` in `asia-south1-a`, project `plc-integrated-dashboard`.

## 1. What runs where

Everything runs as Docker containers on **one VM**, in the directory `/opt/plc-dashboard`.

| Component | Container | Reachable from the internet | Reachable only on the VM (via SSH tunnel) |
|---|---|---|---|
| Dashboard (web UI) + HTTPS | `web` (Caddy) | `https://<ip>.sslip.io` (443; port 80 redirects) | — |
| REST API + API docs | `api` (FastAPI) | `https://<ip>.sslip.io/api/...`, docs at `/api/docs` | — |
| MQTT broker | `mosquitto` | `<ip>.sslip.io:8883` (TLS, for PLCs) | `localhost:1883` (plain, admin/services) |
| Database | `db` (TimescaleDB / PostgreSQL 16) | — | `localhost:5432` |
| Telemetry processor | `ingestor` | — (no port; see **System health** page) | — |
| Web simulator | `simulator-ui` | `https://sim.<ip>.sslip.io` | — |
| One-shot setup jobs | `certs`, `init` | — | — |

**Where the secrets are:** only in **GCP Secret Manager** (project `plc-integrated-dashboard`, stored in `asia-south1`). No password or key is in a file on your laptop or in git.

| Secret (Secret Manager) | Used as | What it is |
|---|---|---|
| `plc-db-password` | `DB_PASSWORD` | Database user `plc`, database `plc` |
| `plc-jwt-secret` | `JWT_SECRET` | Signs login sessions. Changing it signs everyone out. |
| `plc-mqtt-admin-password` | `MQTT_ADMIN_PASSWORD` | MQTT superuser `admin` (API, and you via the SSH tunnel) |
| `plc-mqtt-db-password` | `MQTT_DB_PASSWORD` | Read-only database login `mqtt_auth` the broker uses to check MQTT logins |
| `plc-ingestor-mqtt-password` | `INGESTOR_MQTT_PASSWORD` | MQTT login of the ingestor |
| `plc-simulator-mqtt-password` | `SIMULATOR_MQTT_PASSWORD` | MQTT login of the web simulator |
| `plc-initial-admin-password` (new installations only) | `ADMIN_PASSWORD` | Creates the first dashboard admin |

How it fits together:
* **`deploy/gcp-settings.env`** holds only non-secret settings (host names, ports, URLs). It is safe to commit.
* **`deploy/secrets.list`** maps each variable to its secret. `scripts/deploy.sh` creates any secret that is missing.
* **On every deploy** the VM reads the secrets with its own service account (`plc-runtime`, which may read only `plc-*` secrets) and writes `/opt/plc-dashboard/.env`: root-only, mode 600. Docker Compose needs that file to start the containers.

View or change a secret (as the deployer):

```bash
export CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT=plc-deployer@plc-integrated-dashboard.iam.gserviceaccount.com
gcloud secrets versions access latest --secret=plc-mqtt-admin-password     # show
printf '%s' 'NEW-VALUE' | gcloud secrets versions add plc-mqtt-admin-password --data-file=-   # change
./scripts/deploy.sh                                                         # apply
```

* **Safe to change, then redeploy:** the MQTT passwords, `plc-mqtt-db-password` and `plc-jwt-secret`. The deploy re-applies them.
* **Do not change `plc-db-password`** without first changing the database user's password (`ALTER USER plc PASSWORD ...`); otherwise the services can no longer connect.
* **Secret values must not contain quotes or line breaks.**
* **Old versions stay in Secret Manager** until you disable or destroy them there.

**All user logins are stored in and checked against the database:**
* **Dashboard and simulator logins:** table `users`, bcrypt hashes.
* **MQTT logins for PLCs and services:** tables `mqtt_accounts` and `mqtt_acls`, PBKDF2-SHA512 hashes. The broker checks every connection, publish and subscribe against these tables. A new, rotated or disabled login takes effect within 5 seconds; changed topic permissions within 30 seconds.

## 2. Dashboard users

| User | Role | Can |
|---|---|---|
| `admin@example.com` | admin | Everything: devices and credentials, tags, alarm rules, users, audit log |
| `operator@example.com` | operator | View everything and acknowledge alarms. No configuration. |

- **Add users, change roles, reset passwords:** **Admin → Users**. Each person should get their own account.
- **Change your own password:** every user can do this with **Change password** at the bottom of the left menu. It needs the current password. Changing a password, or an admin resetting it, signs that user out on every other browser and device.
- **Wrong passwords:** 10 wrong passwords from one IP within 5 minutes lock that IP out for the rest of the 5 minutes.
- **One sign-in for both sites:** signing in to the dashboard also signs you in to the simulator (`sim.<host>`). The session cookie is shared through `COOKIE_DOMAIN` in `deploy/gcp-settings.env`. Opening the simulator while signed out goes to the dashboard's sign-in page and then back.
- **Signing out** from either site signs you out of both and shows the "You have been signed out" page. The browser's Back button then lands on the sign-in page, not on the previous screen.

These accounts are for the dashboard only. They are unrelated to Google Cloud access; see [README → Access setup](../README.md#1-access-setup-once-as-the-project-owner).

## 3. Shell access to the VM

Run all `gcloud` commands from the repo root on your laptop. They connect through IAP; there is no public SSH port.

```bash
# helper: act as the deployer service account
export CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT=plc-deployer@plc-integrated-dashboard.iam.gserviceaccount.com

gcloud compute ssh plc-dashboard --zone asia-south1-a --tunnel-through-iap
# on the VM:
cd /opt/plc-dashboard
sudo docker compose ps                         # status of every container
sudo docker compose logs -f --tail 100 ingestor   # follow logs (api, mosquitto, web, db, simulator)
sudo docker compose restart api                # restart one component
```

Common tasks on the VM:

```bash
df -h /            # disk space
free -m            # memory
```

To deploy a new version, run `./scripts/deploy.sh` from the laptop, never by editing files on the VM.

## Quick access: one command for the database and the broker

```bash
./scripts/tunnel.sh     # keep it running; it prints the connection details (passwords from Secret Manager)
```

Then use DBeaver/pgAdmin on `127.0.0.1:15432` and MQTT Explorer/MQTTX on `127.0.0.1:11883`, as described below.

## 4. Database (TimescaleDB / PostgreSQL)

### From the VM (quickest)

```bash
sudo docker compose exec db psql -U plc -d plc
```

### From your laptop with a GUI (DBeaver, pgAdmin, TablePlus, DataGrip)

1. Open a tunnel and leave it running in its own terminal:
   ```bash
   gcloud compute ssh plc-dashboard --zone asia-south1-a --tunnel-through-iap -- -N -L 15432:localhost:5432
   ```
2. Connect the tool to: host `127.0.0.1` (not `localhost`, which macOS may resolve to IPv6), port `15432`, database `plc`, user `plc`, password = secret `plc-db-password`. SSL is not needed; the tunnel is already encrypted.

### Tables worth knowing

| Table / view | Contents |
|---|---|
| `devices` | One row per PLC: status, online, last_seen, message counts |
| `tags` | Per device and tag config: display name, unit, scale/offset, normal range, warning/critical limits, analytics role, operator guidance |
| `tag_latest` | Latest value of every tag |
| `telemetry` | Every value (hypertable; raw data kept 30 days, compressed after 1 day) |
| `telemetry_1m`, `telemetry_1h` | 1-minute (1 year) and 1-hour (5 years) rollups |
| `raw_messages` | Every MQTT message exactly as received, with result (7 days) |
| `ingest_errors` | Rejected messages with the reason (7 days) |
| `alarm_rules`, `alarms` | Alarm configuration and history. Rules with `managed_by = 'limit:…'` come from tag limits; change them in Asset configuration |
| `stoppage_reasons` | Reason chosen for each machine stop (Performance & downtime) |
| `commissioning_items` | Commissioning checklist per asset (Data quality & link) |
| `users`, `audit_log` | Dashboard users and who did what |

```sql
-- latest values of one PLC
SELECT tag, value_num, value_text, ts FROM tag_latest WHERE device_id = 'conveyer-plc-line-01' ORDER BY tag;

-- last 20 raw messages from a PLC
SELECT ts, status, payload, detail FROM raw_messages WHERE device_id = 'conveyer-plc-line-01' ORDER BY ts DESC LIMIT 20;

-- one tag over the last hour, 1-minute averages
SELECT bucket, sum_val / cnt AS avg, min_val, max_val FROM telemetry_1m
WHERE device_id = 'conveyer-plc-line-01' AND tag = 'result_1' AND bucket > now() - interval '1 hour' ORDER BY bucket;

-- database size
SELECT pg_size_pretty(pg_database_size('plc'));
```

**Be careful with writes.** Change configuration through the dashboard: it also updates the broker and the ingestor, and records an audit entry. Direct `UPDATE`s to `devices`, `tags` or `alarm_rules` are picked up by the ingestor only within 60 seconds.

**Copy GCP data to your local stack:** `./scripts/sync-from-gcp.sh` (from the laptop). It replaces the local database with a copy of the GCP one; the previous local data is saved in `backups/`. GCP is the source of truth, and local is a snapshot.

**Manual backup** (in addition to the automatic daily disk snapshots):
```bash
sudo docker compose exec -T db pg_dump -U plc -Fc plc > plc-$(date +%F).dump    # on the VM
gcloud compute scp plc-dashboard:/opt/plc-dashboard/plc-*.dump . --zone asia-south1-a --tunnel-through-iap
```

## 5. MQTT broker (Mosquitto)

### See live PLC traffic with a GUI (MQTT Explorer, MQTTX)

1. Open a tunnel:
   ```bash
   gcloud compute ssh plc-dashboard --zone asia-south1-a --tunnel-through-iap -- -N -L 11883:localhost:1883
   ```
2. Connect the tool to: host `127.0.0.1`, port `11883`, no TLS, username `admin`, password = secret `plc-mqtt-admin-password`.
3. Subscribe to:
   * `plc/#`: everything the PLCs send (raw)
   * `app/#`: what the ingestor publishes after processing (`app/live/...`, `app/alarms`, `app/stats`)
   * `$SYS/#`: broker statistics

Do not use the `admin` account from outside through port 8883. Keep it behind the tunnel.

### From the VM, on the command line

```bash
sudo docker compose exec mosquitto sh      # then, inside the container:
mosquitto_sub -u admin -P '<MQTT_ADMIN_PASSWORD>' -t 'plc/#' -v          # watch PLC messages
```

Device MQTT logins are managed from the dashboard (**Admin → Devices**: add, rotate password, disable, delete). They are rows in `mqtt_accounts` / `mqtt_acls`:

```sql
SELECT username, kind, enabled, is_superuser, updated_at FROM mqtt_accounts ORDER BY kind, username;
SELECT username, topic, access FROM mqtt_acls ORDER BY username;   -- access: 1 receive, 2 publish, 4 subscribe
```

Use the dashboard rather than editing these rows by hand; it also generates the password hash.

## 6. Web simulator

`https://sim.<ip>.sslip.io` is a **test tool for admins only** (dashboard admin login). Operators and viewers are refused, and the dashboard shows its link only to admins.

1. **Target:** site, line and device ID. The **Devices** field creates `<id>-01 … <id>-NN`.
2. **Message:**
   * **Auto values:** a table of values (random, wave, counter, fixed, on/off) in array, JSON object or canonical format.
   * **Custom text:** sent exactly as typed. Placeholders such as `{{rand:270:280}}` are replaced fresh in every message; the full list is on the page.
3. **Send:** a single message, or a bulk run (N messages per device every X seconds, or until you press Stop).

How it behaves:
* **Topic:** you choose it. `{{site}}`, `{{line}}` and `{{device}}` are filled in from the fields.
  * `sim/…` (default `sim/{{site}}/{{line}}/{{device}}/telemetry`): a simulated device. Shown on the dashboard with a badge and removed automatically.
  * `plc/…`: acts exactly like a real PLC, for testing the real pipeline end to end. Not marked simulated and **not removed automatically**.
  * `test/…`: not shown on the dashboard; for other MQTT subscribers.

  Wildcards and other roots, such as the internal `app/`, are refused. Messages go through the same broker and ingestor as real PLCs, so they show up in the dashboard, trends, raw data and alarms.
* **Dashboard badge:** simulated devices carry a **Simulated** badge there.
* **Removal:** a simulated device is removed with all its data when you click **Remove** or **Remove all**, and automatically `SIM_DEVICE_TTL_MIN` minutes after its last message.
* **Real PLCs are protected:** the simulator cannot publish for a real PLC's device ID, and a real device cannot be created with a simulated device's ID.

## 7. API

* Interactive docs: `https://<ip>.sslip.io/api/docs`. Log in to the dashboard in the same browser first; "Try it out" then uses your session.
* Health check (no login): `https://<ip>.sslip.io/api/healthz` returns `{"ok": true, "db": true, "broker": true}`

## 8. Monitoring and health

| Where | What |
|---|---|
| Dashboard → **System health** | Broker connection, ingest rate, rejected/duplicate messages, DB size, online devices |
| Dashboard → **Raw data** | Every message as received, with parse result |
| Dashboard → **Admin → Logs** | Output of every component (api, ingestor, mosquitto, web, simulator-ui, db, init, certs), kept 30 days. Filter by component, level (warnings/errors) and period; search by text or by a JSON field with `key=value`, e.g. `logger=ingestor` or `device_id=plc-01`. **Live** refreshes every 10 s. |
| GCP Console → Logging → Logs Explorer | The same logs (log name `gcplogs-docker-driver`), for advanced queries |
| `sudo docker compose logs <service>` | Local copy on the VM (recent lines only) |
| GCP Console → Compute Engine → VM instances → `plc-dashboard` → Observability | CPU, memory, disk, network of the VM |
| GCP Console → Compute Engine → Snapshots | Daily disk snapshots (kept 7 days) |

## 9. If something is wrong

| Symptom | Check |
|---|---|
| Dashboard does not load | `curl https://<ip>.sslip.io/api/healthz`; on the VM `sudo docker compose ps` and `logs web api` |
| PLC cannot connect | Broker log: `sudo docker compose logs --tail 50 mosquitto` shows each connection attempt and why it was refused |
| PLC connected, no values | Dashboard → **Raw data** (is anything arriving? is it rejected?), then `logs ingestor` |
| Device shows offline | No data for longer than 3× its publish interval (min. 15 s), or its Last Will fired |
| Disk filling up | `df -h /`; reduce `SIM_DEVICES`, remove the simulator, or increase `disk_size_gb` in Terraform |
