import datetime as dt

from app.security import decode_token, hash_password, issue_token, role_allows, verify_password


def test_password_roundtrip():
    h = hash_password("s3cret-pass")
    assert verify_password("s3cret-pass", h)
    assert not verify_password("wrong", h)
    assert not verify_password("x", "not-a-hash")


def test_token_roundtrip_and_expiry():
    assert decode_token(issue_token(7, "operator"))["sub"] == "7"
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
    assert decode_token(issue_token(7, "operator", now=old)) is None
    assert decode_token("garbage") is None


def test_role_hierarchy():
    assert role_allows("admin", "operator")
    assert role_allows("operator", "operator")
    assert not role_allows("viewer", "operator")
    assert not role_allows("unknown", "viewer")


def test_token_carries_millisecond_issue_time():
    now = dt.datetime(2026, 9, 27, 10, 0, 0, 750000, tzinfo=dt.timezone.utc)
    claims = decode_token(issue_token(1, "viewer", now=now))
    assert claims["iat_ms"] == int(now.timestamp() * 1000) and claims["iat"] == int(now.timestamp())
