import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.environ.get("DATABASE_URL", "postgresql://plc:plc@localhost:5432/plc")
    )

    # Internal broker connection. The API's "admin" MQTT account (superuser) observes app/# and $SYS/#.
    mqtt_host: str = field(default_factory=lambda: os.environ.get("MQTT_HOST", "localhost"))
    mqtt_port: int = field(default_factory=lambda: int(os.environ.get("MQTT_PORT", "1883")))
    mqtt_admin_user: str = field(default_factory=lambda: os.environ.get("MQTT_ADMIN_USER", "admin"))
    mqtt_admin_password: str = field(
        default_factory=lambda: os.environ.get("MQTT_ADMIN_PASSWORD") or os.environ.get("MOSQUITTO_DYNSEC_PASSWORD", "")
    )
    mqtt_db_password: str = field(default_factory=lambda: os.environ.get("MQTT_DB_PASSWORD", ""))
    ingestor_mqtt_password: str = field(default_factory=lambda: os.environ.get("INGESTOR_MQTT_PASSWORD", ""))
    simulator_mqtt_password: str = field(default_factory=lambda: os.environ.get("SIMULATOR_MQTT_PASSWORD", ""))
    simulator_url: str = field(default_factory=lambda: os.environ.get("SIMULATOR_URL", ""))
    dashboard_url: str = field(default_factory=lambda: os.environ.get("DASHBOARD_URL", ""))
    sim_device_ttl_min: int = field(default_factory=lambda: int(os.environ.get("SIM_DEVICE_TTL_MIN", "30")))

    # What PLCs are told to connect to (shown in the admin UI).
    mqtt_public_host: str = field(default_factory=lambda: os.environ.get("MQTT_PUBLIC_HOST", "localhost"))
    mqtt_public_tls_port: int = field(default_factory=lambda: int(os.environ.get("MQTT_PUBLIC_TLS_PORT", "8883")))
    certs_dir: str = field(default_factory=lambda: os.environ.get("CERTS_DIR", "/certs"))

    jwt_secret: str = field(default_factory=lambda: os.environ.get("JWT_SECRET", "dev-insecure-secret-change-me"))
    session_hours: int = field(default_factory=lambda: int(os.environ.get("SESSION_HOURS", "12")))
    cookie_secure: bool = field(default_factory=lambda: _bool("COOKIE_SECURE", False))
    # Parent domain shared by the dashboard and the simulator (sim.<domain>) so one sign-in
    # covers both. Empty = host-only cookie (fine locally: cookies ignore the port).
    # Plant local time zone: shifts (A 06-18, B 18-06) and daily rollups use it.
    plant_tz: str = field(default_factory=lambda: os.environ.get("PLANT_TZ", "Asia/Kolkata"))
    cookie_domain: str = field(default_factory=lambda: os.environ.get("COOKIE_DOMAIN", ""))

    admin_email: str = field(default_factory=lambda: os.environ.get("ADMIN_EMAIL", "AD4127"))
    admin_password: str = field(default_factory=lambda: os.environ.get("ADMIN_PASSWORD", ""))
    operator_email: str = field(default_factory=lambda: os.environ.get("OPERATOR_EMAIL", ""))
    operator_password: str = field(default_factory=lambda: os.environ.get("OPERATOR_PASSWORD", ""))


settings = Settings()
