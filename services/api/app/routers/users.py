from typing import Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..deps import CurrentUser, admin
from ..security import LOGIN_PATTERN, hash_password, normalize_login

router = APIRouter(prefix="/users", tags=["users"])

Role = Literal["viewer", "operator", "admin"]
USER_COLUMNS = "id, email, name, role, disabled, created_at, last_login_at"
# a user ID such as OP2386 (stored in capitals) or an e-mail address (stored in lower case)


class UserCreate(BaseModel):
    email: str = Field(pattern=LOGIN_PATTERN, max_length=254, description="user ID (e.g. OP2386) or e-mail")
    name: str = Field(default="", max_length=120)
    role: Role = "viewer"
    password: str = Field(min_length=8, max_length=256)


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    role: Role | None = None
    disabled: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=256)


def user_dict(row) -> dict:
    d = dict(row)
    for key in ("created_at", "last_login_at"):
        d[key] = d[key].isoformat() if d[key] else None
    return d


async def _other_active_admins(user_id: int) -> int:
    return await db.pool().fetchval(
        "SELECT count(*) FROM users WHERE role = 'admin' AND NOT disabled AND id <> $1", user_id
    )


@router.get("")
async def list_users(_: CurrentUser = Depends(admin)) -> list[dict]:
    rows = await db.pool().fetch(f"SELECT {USER_COLUMNS} FROM users ORDER BY email")
    return [user_dict(r) for r in rows]


@router.post("", status_code=201)
async def create_user(body: UserCreate, user: CurrentUser = Depends(admin)) -> dict:
    try:
        row = await db.pool().fetchrow(
            f"INSERT INTO users (email, name, role, password_hash) VALUES ($1, $2, $3, $4) RETURNING {USER_COLUMNS}",
            normalize_login(body.email), body.name, body.role, hash_password(body.password),
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(409, "A user with this user ID already exists") from exc
    await db.audit(user.email, "user.create", normalize_login(body.email), {"role": body.role})
    return user_dict(row)


@router.patch("/{user_id}")
async def update_user(user_id: int, body: UserUpdate, user: CurrentUser = Depends(admin)) -> dict:
    changes = body.model_dump(exclude_none=True)
    demoting = changes.get("role", "admin") != "admin" or changes.get("disabled") is True
    if demoting and not await _other_active_admins(user_id):
        target_role = await db.pool().fetchval("SELECT role FROM users WHERE id = $1", user_id)
        if target_role == "admin":
            raise HTTPException(400, "Cannot remove the last active admin")
    if "password" in changes:
        changes["password_hash"] = hash_password(changes.pop("password"))
    reset_sessions = "password_hash" in changes
    if not changes:
        raise HTTPException(400, "Nothing to update")
    assignments = ", ".join(f"{col} = ${i}" for i, col in enumerate(changes, start=2))
    if reset_sessions:
        assignments += ", password_changed_at = now()"
    row = await db.pool().fetchrow(
        f"UPDATE users SET {assignments} WHERE id = $1 RETURNING {USER_COLUMNS}", user_id, *changes.values()
    )
    if row is None:
        raise HTTPException(404, "User not found")
    audit_details = {k: v for k, v in changes.items() if k != "password_hash"}
    if "password_hash" in changes:
        audit_details["password"] = "reset"
    await db.audit(user.email, "user.update", row["email"], audit_details)
    return user_dict(row)


@router.delete("/{user_id}", status_code=204)
async def delete_user(user_id: int, user: CurrentUser = Depends(admin)) -> None:
    if user_id == user.id:
        raise HTTPException(400, "You cannot delete your own account")
    email = await db.pool().fetchval("DELETE FROM users WHERE id = $1 RETURNING email", user_id)
    if email is None:
        raise HTTPException(404, "User not found")
    await db.audit(user.email, "user.delete", email)
