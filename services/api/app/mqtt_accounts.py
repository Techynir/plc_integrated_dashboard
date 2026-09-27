"""MQTT logins stored in PostgreSQL (tables mqtt_accounts / mqtt_acls).

Mosquitto's go-auth plugin reads these tables to authenticate every connection and to
authorise every publish/subscribe, so creating, rotating, disabling or deleting a row here
takes effect without touching the broker (within the plugin's short cache time).
"""

import base64
import hashlib
import hmac
import os

import asyncpg

ITERATIONS = 100_000
READ, WRITE, SUBSCRIBE = 1, 2, 4


def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, iterations, 64)
    return f"PBKDF2$sha512${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        prefix, algo, iterations, salt, digest = encoded.split("$")
        if prefix != "PBKDF2" or algo != "sha512":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.pbkdf2_hmac("sha512", password.encode(), base64.b64decode(salt), int(iterations), len(expected))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def from_dynsec(encoded_password: str) -> str | None:
    """Convert Mosquitto dynamic-security "$7$<iter>$<salt>$<hash>" (PBKDF2-SHA512) to our format."""
    parts = encoded_password.split("$")
    if len(parts) != 5 or parts[1] != "7":
        return None
    return f"PBKDF2$sha512${parts[2]}${parts[3]}${parts[4]}"


def device_acls(device_id: str) -> list[tuple[str, int]]:
    # A PLC may only publish below plc/<site>/<line>/<its own id>/<kind>; it cannot subscribe.
    return [(f"plc/+/+/{device_id}/+", WRITE)]


async def upsert_account(
    conn: asyncpg.Connection,
    username: str,
    password_hash: str,
    *,
    kind: str,
    acls: list[tuple[str, int]],
    device_id: str | None = None,
    superuser: bool = False,
    description: str = "",
) -> None:
    async with conn.transaction():
        await conn.execute(
            """
            INSERT INTO mqtt_accounts (username, password_hash, kind, device_id, is_superuser, description)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (username) DO UPDATE SET
                password_hash = EXCLUDED.password_hash, kind = EXCLUDED.kind, device_id = EXCLUDED.device_id,
                is_superuser = EXCLUDED.is_superuser, description = EXCLUDED.description,
                enabled = true, updated_at = now()
            """,
            username, password_hash, kind, device_id, superuser, description,
        )
        await conn.execute("DELETE FROM mqtt_acls WHERE username = $1", username)
        if acls:
            await conn.executemany(
                "INSERT INTO mqtt_acls (username, topic, access) VALUES ($1, $2, $3)",
                [(username, topic, access) for topic, access in acls],
            )


async def set_device_password(conn: asyncpg.Connection, device_id: str, password: str) -> None:
    await upsert_account(
        conn, device_id, hash_password(password), kind="device", device_id=device_id,
        acls=device_acls(device_id), description=f"PLC {device_id}",
    )


async def set_enabled(conn: asyncpg.Connection, username: str, enabled: bool) -> None:
    await conn.execute(
        "UPDATE mqtt_accounts SET enabled = $2, updated_at = now() WHERE username = $1", username, enabled
    )


async def delete_account(conn: asyncpg.Connection, username: str) -> None:
    await conn.execute("DELETE FROM mqtt_accounts WHERE username = $1", username)
