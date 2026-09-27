from dataclasses import dataclass
from typing import Callable

from fastapi import Depends, HTTPException, Request, WebSocket

from . import db
from .security import SESSION_COOKIE, decode_token, role_allows


@dataclass(frozen=True)
class CurrentUser:
    id: int
    email: str
    name: str
    role: str

    def as_dict(self) -> dict:
        return {"id": self.id, "email": self.email, "name": self.name, "role": self.role}


async def user_from_cookie(token: str | None) -> CurrentUser | None:
    claims = decode_token(token) if token else None
    if not claims:
        return None
    # Role is re-read from the database so demotions/disables apply immediately.
    row = await db.pool().fetchrow(
        "SELECT id, email, name, role, disabled, password_changed_at FROM users WHERE id = $1", int(claims["sub"])
    )
    if row is None or row["disabled"]:
        return None
    # A password change signs the user out of every session started before it.
    issued_ms = claims.get("iat_ms", claims.get("iat", 0) * 1000)
    if issued_ms <= int(row["password_changed_at"].timestamp() * 1000):
        return None
    return CurrentUser(row["id"], row["email"], row["name"], row["role"])


async def current_user(request: Request) -> CurrentUser:
    user = await user_from_cookie(request.cookies.get(SESSION_COOKIE))
    if user is None:
        raise HTTPException(401, "Not authenticated")
    return user


async def websocket_user(websocket: WebSocket) -> CurrentUser | None:
    return await user_from_cookie(websocket.cookies.get(SESSION_COOKIE))


def require(role: str) -> Callable:
    async def dependency(user: CurrentUser = Depends(current_user)) -> CurrentUser:
        if not role_allows(user.role, role):
            raise HTTPException(403, f"Requires {role} role")
        return user

    return dependency


viewer = require("viewer")
operator = require("operator")
admin = require("admin")
