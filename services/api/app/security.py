import datetime as dt

import bcrypt
import jwt

from .config import settings

ROLES = ("viewer", "operator", "admin")
ROLE_RANK = {role: i for i, role in enumerate(ROLES)}
SESSION_COOKIE = "plc_session"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def issue_token(user_id: int, role: str, now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": str(user_id),
        "role": role,
        "iat": int(now.timestamp()),
        "iat_ms": int(now.timestamp() * 1000),  # sign-out-everywhere needs sub-second precision
        "exp": int((now + dt.timedelta(hours=settings.session_hours)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def set_session_cookie(response, user_id: int, role: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        issue_token(user_id, role),
        max_age=settings.session_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
        domain=settings.cookie_domain or None,
    )


def clear_session_cookie(response) -> None:
    # Clear both the shared (domain) cookie and any older host-only cookie.
    response.delete_cookie(SESSION_COOKIE, path="/")
    if settings.cookie_domain:
        response.delete_cookie(SESSION_COOKIE, path="/", domain=settings.cookie_domain)


def role_allows(role: str, required: str) -> bool:
    return ROLE_RANK.get(role, -1) >= ROLE_RANK[required]
