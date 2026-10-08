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
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=750000)  # must not be expired
    claims = decode_token(issue_token(1, "viewer", now=now))
    assert claims["iat_ms"] == int(now.timestamp() * 1000) and claims["iat"] == int(now.timestamp())


def test_logins_are_user_ids_or_emails():
    import re

    from app.security import LOGIN_PATTERN, normalize_login

    assert normalize_login(" ad4127 ") == "AD4127" and normalize_login("OP2386") == "OP2386"
    assert normalize_login("Someone@Mill.IN") == "someone@mill.in"
    assert all(re.match(LOGIN_PATTERN, v) for v in ("AD4127", "op2386", "QA10234", "someone@mill.in"))
    assert not any(re.match(LOGIN_PATTERN, v) for v in ("admin", "A1", "OP98", "AD 4127", "x@y"))
