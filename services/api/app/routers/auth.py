import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .. import db
from ..config import settings
from ..deps import CurrentUser, current_user
from ..security import clear_session_cookie, hash_password, normalize_login, set_session_cookie, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

# Simple in-memory brute-force guard: 10 failed logins per IP per 5 minutes.
_FAIL_WINDOW_S = 300
_FAIL_LIMIT = 10
_failures: dict[str, deque] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _too_many_failures(ip: str) -> bool:
    q = _failures[ip]
    cutoff = time.monotonic() - _FAIL_WINDOW_S
    while q and q[0] < cutoff:
        q.popleft()
    return len(q) >= _FAIL_LIMIT


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


class PasswordChangeIn(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=8, max_length=256, description="at least 8 characters")


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response) -> dict:
    ip = _client_ip(request)
    if _too_many_failures(ip):
        raise HTTPException(429, "Too many failed attempts, try again later")

    row = await db.pool().fetchrow(
        "SELECT id, email, name, role, password_hash, disabled FROM users WHERE email = $1",
        normalize_login(body.email),
    )
    if row is None or row["disabled"] or not verify_password(body.password, row["password_hash"]):
        _failures[ip].append(time.monotonic())
        raise HTTPException(401, "Invalid user ID or password")

    await db.pool().execute("UPDATE users SET last_login_at = now() WHERE id = $1", row["id"])
    await db.audit(row["email"], "login", details={"ip": ip})
    set_session_cookie(response, row["id"], row["role"])
    return {"id": row["id"], "email": row["email"], "name": row["name"], "role": row["role"]}


@router.post("/logout")
async def logout(response: Response) -> dict:
    clear_session_cookie(response)
    return {"ok": True}


@router.get("/config")
async def public_config() -> dict:
    """Unauthenticated: where the login page may send the user back to after sign-in."""
    return {"simulator_url": settings.simulator_url or None, "dashboard_url": settings.dashboard_url or None}


@router.get("/me")
async def me(user: CurrentUser = Depends(current_user)) -> dict:
    return user.as_dict()


@router.post("/password")
async def change_password(
    body: PasswordChangeIn, request: Request, response: Response, user: CurrentUser = Depends(current_user)
) -> dict:
    """Any signed-in user can change their own password. Other sessions of the user are signed out."""
    ip = _client_ip(request)
    if _too_many_failures(ip):
        raise HTTPException(429, "Too many failed attempts, try again later")
    current_hash = await db.pool().fetchval("SELECT password_hash FROM users WHERE id = $1", user.id)
    if not verify_password(body.current_password, current_hash):
        _failures[ip].append(time.monotonic())
        raise HTTPException(400, "Current password is incorrect")
    if body.new_password == body.current_password:
        raise HTTPException(400, "The new password must be different from the current one")
    await db.pool().execute(
        "UPDATE users SET password_hash = $1, password_changed_at = now() WHERE id = $2",
        hash_password(body.new_password), user.id,
    )
    set_session_cookie(response, user.id, user.role)
    await db.audit(user.email, "password.change", user.email)
    return {"ok": True}
