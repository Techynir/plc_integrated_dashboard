"""One-shot initialisation job:  python -m app.bootstrap

1. Apply SQL migrations.
2. Create the first admin (and optional operator) dashboard user if none exists.
3. Store the service accounts' MQTT logins in PostgreSQL, import device logins from the old
   dynamic-security file once, and create the broker's read-only database login.
4. Write the broker's database settings (go-auth.conf) and prepare its data directory.

Everything is idempotent; the job runs on every deploy before api/ingestor start.
"""

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import asyncpg

from . import mqtt_accounts
from .config import settings
from .mqtt_accounts import READ, SUBSCRIBE, WRITE
from .security import hash_password

log = logging.getLogger("bootstrap")
MIGRATIONS_DIR = Path(__file__).parent / "migrations"
BROKER_CONF_DIR = Path(os.environ.get("BROKER_CONF_DIR", "/brokerconf"))
BROKER_DATA_DIR = Path(os.environ.get("BROKER_DATA_DIR", "/mqdata"))
BROKER_UID = 1000  # mosquitto user in the broker image


def split_sql(sql: str) -> list[str]:
    """Split a migration file into statements (see the rules in 001_init.sql)."""
    without_comments = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in re.split(r";\s*$", without_comments, flags=re.MULTILINE) if s.strip()]


async def migrate(conn: asyncpg.Connection) -> None:
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, applied_at timestamptz DEFAULT now())"
    )
    applied = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.stem in applied:
            continue
        log.info("applying migration %s", path.name)
        for statement in split_sql(path.read_text()):
            await conn.execute(statement)
        await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", path.stem)


async def ensure_admin_user(conn: asyncpg.Connection) -> None:
    if await conn.fetchval("SELECT count(*) FROM users WHERE role = 'admin' AND NOT disabled"):
        return
    if len(settings.admin_password) < 8:
        raise SystemExit("ADMIN_PASSWORD (8+ chars) is required to create the first admin user")
    await conn.execute(
        "INSERT INTO users (email, name, password_hash, role) VALUES ($1, 'Administrator', $2, 'admin') "
        "ON CONFLICT (email) DO UPDATE SET role = 'admin', disabled = false, password_hash = EXCLUDED.password_hash",
        settings.admin_email.lower(),
        hash_password(settings.admin_password),
    )
    log.info("created admin user %s", settings.admin_email)


async def ensure_operator_user(conn: asyncpg.Connection) -> None:
    """Optional first operator (OPERATOR_EMAIL/OPERATOR_PASSWORD). Created once; never overwritten."""
    if not settings.operator_email:
        return
    if len(settings.operator_password) < 8:
        raise SystemExit("OPERATOR_PASSWORD (8+ chars) is required when OPERATOR_EMAIL is set")
    created = await conn.fetchval(
        "INSERT INTO users (email, name, password_hash, role) VALUES ($1, 'Operator', $2, 'operator') "
        "ON CONFLICT (email) DO NOTHING RETURNING id",
        settings.operator_email.lower(),
        hash_password(settings.operator_password),
    )
    if created:
        log.info("created operator user %s", settings.operator_email)


# ---------------------------------------------------------------- MQTT logins (PostgreSQL)

SERVICE_ACCOUNTS = [
    # username, password setting, env name, superuser, ACLs, description
    ("admin", "mqtt_admin_password", "MQTT_ADMIN_PASSWORD", True, [], "API and operators (via SSH tunnel)"),
    ("ingestor", "ingestor_mqtt_password", "INGESTOR_MQTT_PASSWORD", False,
     [("plc/#", READ | SUBSCRIBE), ("sim/#", READ | SUBSCRIBE), ("app/#", WRITE)], "Telemetry ingestor"),
    # Admin-only test tool: may publish as a PLC (plc/), as a simulated PLC (sim/) or anywhere under test/.
    ("simulator", "simulator_mqtt_password", "SIMULATOR_MQTT_PASSWORD", False,
     [("plc/#", WRITE), ("sim/#", WRITE), ("test/#", WRITE)], "Web simulator"),
]


async def ensure_service_accounts(conn: asyncpg.Connection) -> None:
    for username, attr, env_name, superuser, acls, description in SERVICE_ACCOUNTS:
        password = getattr(settings, attr)
        if len(password) < 12:
            raise SystemExit(f"{env_name} (12+ chars) is required")
        current = await conn.fetchval("SELECT password_hash FROM mqtt_accounts WHERE username = $1", username)
        # Keep the stored hash when the password is unchanged (hashing is deliberately slow).
        password_hash = current if current and mqtt_accounts.verify_password(password, current) else (
            mqtt_accounts.hash_password(password)
        )
        await mqtt_accounts.upsert_account(
            conn, username, password_hash, kind="service", superuser=superuser, acls=acls, description=description
        )


async def import_dynsec_devices(conn: asyncpg.Connection, data_dir: Path) -> None:
    """One-time migration from Mosquitto dynamic-security: keep existing PLC passwords working."""
    path = data_dir / "dynamic-security.json"
    if not path.exists():
        return
    imported = 0
    for client in json.loads(path.read_text()).get("clients", []):
        roles = {r.get("rolename") for r in client.get("roles", [])}
        username = client.get("username", "")
        if "plc-device" not in roles or not client.get("encoded_password"):
            continue
        if await conn.fetchval("SELECT 1 FROM mqtt_accounts WHERE username = $1", username):
            continue
        converted = mqtt_accounts.from_dynsec(client["encoded_password"])
        if converted is None:
            log.warning("cannot import MQTT login for %s (unknown hash format); rotate its password", username)
            continue
        await mqtt_accounts.upsert_account(
            conn, username, converted, kind="device", device_id=username,
            acls=mqtt_accounts.device_acls(username), description=f"PLC {username} (imported)",
        )
        if client.get("disabled"):
            await mqtt_accounts.set_enabled(conn, username, False)
        imported += 1
    path.rename(path.with_suffix(".json.migrated"))
    log.info("imported %d device MQTT logins from dynamic-security", imported)


async def ensure_broker_db_role(conn: asyncpg.Connection) -> None:
    """Read-only database login the broker uses to check MQTT credentials."""
    if len(settings.mqtt_db_password) < 12:
        raise SystemExit("MQTT_DB_PASSWORD (12+ chars) is required")
    if not await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = 'mqtt_auth'"):
        await conn.execute("CREATE ROLE mqtt_auth LOGIN")
    await conn.execute(await conn.fetchval("SELECT format('ALTER ROLE mqtt_auth PASSWORD %L', $1::text)",
                                           settings.mqtt_db_password))
    await conn.execute("GRANT SELECT ON mqtt_accounts, mqtt_acls TO mqtt_auth")


def write_broker_config(conf_dir: Path) -> None:
    """Mosquitto cannot read environment variables, so its database settings are generated here."""
    db = urlparse(settings.database_url)
    conf_dir.mkdir(parents=True, exist_ok=True)
    target = conf_dir / "go-auth.conf"
    target.write_text(
        "# Generated by app.bootstrap on every deploy. Do not edit.\n"
        "auth_plugin /mosquitto/go-auth.so\n"
        "auth_opt_log_level warn\n"
        "auth_opt_backends postgres\n"
        "auth_opt_hasher pbkdf2\n"
        "auth_opt_hasher_salt_encoding base64\n"
        # Short caches: logins are re-checked after 5 s, permissions after 30 s.
        "auth_opt_cache true\n"
        "auth_opt_cache_type go-cache\n"
        "auth_opt_cache_reset true\n"
        "auth_opt_auth_cache_seconds 5\n"
        "auth_opt_acl_cache_seconds 30\n"
        f"auth_opt_pg_host {db.hostname}\n"
        f"auth_opt_pg_port {db.port or 5432}\n"
        f"auth_opt_pg_dbname {db.path.lstrip('/')}\n"
        "auth_opt_pg_user mqtt_auth\n"
        f"auth_opt_pg_password {settings.mqtt_db_password}\n"
        "auth_opt_pg_sslmode disable\n"
        "auth_opt_pg_userquery SELECT password_hash FROM mqtt_accounts WHERE username = $1 AND enabled LIMIT 1\n"
        "auth_opt_pg_superquery SELECT count(*) FROM mqtt_accounts WHERE username = $1 AND is_superuser AND enabled\n"
        "auth_opt_pg_aclquery SELECT topic FROM mqtt_acls WHERE username = $1 AND (access & $2) <> 0\n"
    )
    target.chmod(0o640)
    os.chown(target, 0, BROKER_UID)


def prepare_broker_data(data_dir: Path) -> None:
    """The broker (uid 1000) must own its data directory. A persistence file written by a newer
    Mosquitto version is set aside so the broker starts cleanly (only queued messages are lost)."""
    if not data_dir.exists():
        return
    old = data_dir / "mosquitto.db"
    if old.exists() and not (data_dir / ".go-auth").exists():
        old.rename(data_dir / "mosquitto.db.pre-go-auth")
    (data_dir / ".go-auth").touch()
    for path in [data_dir, *data_dir.rglob("*")]:
        os.chown(path, BROKER_UID, BROKER_UID)


async def connect_db() -> asyncpg.Connection:
    for _ in range(30):
        try:
            return await asyncpg.connect(settings.database_url)
        except (OSError, asyncpg.CannotConnectNowError) as exc:
            log.warning("database not ready (%s), retrying", exc)
            await asyncio.sleep(2)
    raise SystemExit("database did not become ready")


async def main() -> None:
    conn = await connect_db()
    try:
        await migrate(conn)
        await ensure_admin_user(conn)
        await ensure_operator_user(conn)
        await ensure_service_accounts(conn)
        await ensure_broker_db_role(conn)
        await import_dynsec_devices(conn, BROKER_DATA_DIR)
    finally:
        await conn.close()
    write_broker_config(BROKER_CONF_DIR)
    prepare_broker_data(BROKER_DATA_DIR)
    log.info("bootstrap complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
    sys.exit(0)
