import base64
import hashlib

from app.mqtt_accounts import from_dynsec, hash_password, verify_password


def test_hash_roundtrip_and_format():
    encoded = hash_password("s3cret", iterations=1000)
    assert encoded.startswith("PBKDF2$sha512$1000$")
    assert verify_password("s3cret", encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password("s3cret", "garbage")


def test_dynsec_hash_conversion_keeps_the_password():
    salt = b"0123456789ab"
    digest = hashlib.pbkdf2_hmac("sha512", b"plc-pass", salt, 101, 64)
    dynsec = f"$7$101${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"
    converted = from_dynsec(dynsec)
    assert converted.startswith("PBKDF2$sha512$101$")
    assert verify_password("plc-pass", converted)
    assert from_dynsec("$6$bad") is None
