import json

import asyncpg

from .config import settings

_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def connect() -> asyncpg.Pool:
    global _pool
    _pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=10, init=_init_connection)
    return _pool


async def close() -> None:
    if _pool is not None:
        await _pool.close()


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("database pool is not initialised")
    return _pool


async def audit(actor: str, action: str, target: str = "", details: dict | None = None) -> None:
    await pool().execute(
        "INSERT INTO audit_log (actor, action, target, details) VALUES ($1, $2, $3, $4)",
        actor,
        action,
        target,
        details or {},
    )
