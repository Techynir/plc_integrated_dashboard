"""Generate a private CA and a broker server certificate for MQTT over TLS.

Run as a one-shot job before Mosquitto starts:  python -m app.certs
Existing certificates are kept unless the configured host names changed.
"""

import datetime as dt
import ipaddress
import json
import os
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def _names() -> list[str]:
    raw = [os.environ.get("MQTT_PUBLIC_HOST", "localhost"), "localhost", "127.0.0.1", "mosquitto"]
    raw += [n for n in os.environ.get("MQTT_EXTRA_SANS", "").split(",") if n]
    seen: list[str] = []
    for name in (n.strip() for n in raw):
        if name and name not in seen:
            seen.append(name)
    return seen


def _san(names: list[str]) -> x509.SubjectAlternativeName:
    entries: list[x509.GeneralName] = []
    for name in names:
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            entries.append(x509.DNSName(name))
    return x509.SubjectAlternativeName(entries)


def _has_ext(cert: x509.Certificate, ext_type: type) -> bool:
    try:
        cert.extensions.get_extension_for_class(ext_type)
        return True
    except x509.ExtensionNotFound:
        return False


def _write_key(path: Path, key: ec.EllipticCurvePrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    # Mosquitto runs as uid 1883 inside its container and must read the key.
    path.chmod(0o644)


def generate(out_dir: Path, names: list[str]) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    ca_path, ca_key_path = out_dir / "ca.crt", out_dir / "ca.key"

    ca_cert = x509.load_pem_x509_certificate(ca_path.read_bytes()) if ca_path.exists() else None
    if ca_cert is not None and ca_key_path.exists() and _has_ext(ca_cert, x509.SubjectKeyIdentifier):
        ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
    else:
        ca_key = ec.generate_private_key(ec.SECP256R1())
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PLC Dashboard MQTT CA")])
        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                    key_encipherment=False, data_encipherment=False, key_agreement=False,
                    encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            .sign(ca_key, hashes.SHA256())
        )
        _write_key(ca_key_path, ca_key)
        ca_key_path.chmod(0o600)
        ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))

    server_key = ec.generate_private_key(ec.SECP256R1())
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(ca_cert.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=825))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(_san(names), critical=False)
        # Strict verifiers (OpenSSL 3 X509_STRICT, many embedded TLS stacks) require key identifiers.
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    _write_key(out_dir / "server.key", server_key)
    (out_dir / "server.crt").write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    (out_dir / "names.json").write_text(json.dumps(names))


def main() -> int:
    out_dir = Path(os.environ.get("CERTS_DIR", "/certs"))
    out_dir.mkdir(parents=True, exist_ok=True)
    names = _names()
    marker = out_dir / "names.json"
    server_cert = out_dir / "server.crt"

    if server_cert.exists() and marker.exists() and json.loads(marker.read_text()) == names:
        cert = x509.load_pem_x509_certificate(server_cert.read_bytes())
        fresh = cert.not_valid_after_utc - dt.datetime.now(dt.timezone.utc) > dt.timedelta(days=30)
        if fresh and _has_ext(cert, x509.AuthorityKeyIdentifier):
            print(f"certs: up to date for {names}")
            return 0

    generate(out_dir, names)
    # Tells deploy.sh that a running broker must be restarted to load the new certificate.
    (out_dir / ".reload").touch()
    print(f"certs: generated server certificate for {names}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
