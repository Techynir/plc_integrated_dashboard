-- MQTT logins and permissions, checked by Mosquitto (go-auth plugin) on every connection
-- and publish/subscribe. Replaces the broker's own dynamic-security file.
--
-- password_hash: PBKDF2-SHA512 "PBKDF2$sha512$<iterations>$<salt b64>$<hash b64>"
-- access bits:   1 = receive messages, 2 = publish, 4 = subscribe

CREATE TABLE IF NOT EXISTS mqtt_accounts (
    username      text PRIMARY KEY,
    password_hash text NOT NULL,
    kind          text NOT NULL CHECK (kind IN ('device', 'service')),
    device_id     text,
    is_superuser  boolean NOT NULL DEFAULT false,
    enabled       boolean NOT NULL DEFAULT true,
    description   text NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mqtt_acls (
    id       serial PRIMARY KEY,
    username text NOT NULL REFERENCES mqtt_accounts ON DELETE CASCADE,
    topic    text NOT NULL,
    access   smallint NOT NULL CHECK (access BETWEEN 1 AND 7),
    UNIQUE (username, topic)
);
