-- Initial schema. Statements are executed one at a time (continuous aggregates
-- cannot run inside a transaction), so each must be idempotent and end with ';'
-- at end of line. Do not use semicolons inside statements.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ---------------------------------------------------------------- users / audit

CREATE TABLE IF NOT EXISTS users (
    id            serial PRIMARY KEY,
    email         text UNIQUE NOT NULL,
    name          text NOT NULL DEFAULT '',
    password_hash text NOT NULL,
    role          text NOT NULL CHECK (role IN ('viewer', 'operator', 'admin')),
    disabled      boolean NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz
);

CREATE TABLE IF NOT EXISTS audit_log (
    id      bigserial PRIMARY KEY,
    ts      timestamptz NOT NULL DEFAULT now(),
    actor   text NOT NULL,
    action  text NOT NULL,
    target  text NOT NULL DEFAULT '',
    details jsonb NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS audit_log_ts ON audit_log (ts DESC);

-- ---------------------------------------------------------------- devices / tags

CREATE TABLE IF NOT EXISTS devices (
    device_id           text PRIMARY KEY,
    name                text NOT NULL DEFAULT '',
    site                text NOT NULL DEFAULT '',
    line                text NOT NULL DEFAULT '',
    description         text NOT NULL DEFAULT '',
    expected_interval_s real NOT NULL DEFAULT 1 CHECK (expected_interval_s > 0),
    enabled             boolean NOT NULL DEFAULT true,
    has_credentials     boolean NOT NULL DEFAULT false,
    online              boolean NOT NULL DEFAULT false,
    status              text,
    last_seen           timestamptz,
    last_seq            bigint,
    seq_gaps            bigint NOT NULL DEFAULT 0,
    msg_count           bigint NOT NULL DEFAULT 0,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tags (
    device_id    text NOT NULL REFERENCES devices ON DELETE CASCADE,
    tag          text NOT NULL,
    display_name text NOT NULL DEFAULT '',
    unit         text NOT NULL DEFAULT '',
    data_type    text NOT NULL DEFAULT 'number' CHECK (data_type IN ('number', 'boolean', 'string')),
    value_scale  double precision NOT NULL DEFAULT 1,
    value_offset double precision NOT NULL DEFAULT 0,
    min_value    double precision,
    max_value    double precision,
    decimals     smallint NOT NULL DEFAULT 2,
    pinned       boolean NOT NULL DEFAULT false,
    configured   boolean NOT NULL DEFAULT false,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, tag)
);

-- ---------------------------------------------------------------- telemetry

-- quality: 0 = GOOD, 1 = UNCERTAIN, 2 = BAD. Booleans are stored as 0/1 in value_num.
CREATE TABLE IF NOT EXISTS telemetry (
    ts         timestamptz NOT NULL,
    device_id  text NOT NULL,
    tag        text NOT NULL,
    value_num  double precision,
    value_text text,
    quality    smallint NOT NULL DEFAULT 0
);

SELECT create_hypertable('telemetry', by_range('ts', INTERVAL '1 day'), if_not_exists => TRUE);

CREATE UNIQUE INDEX IF NOT EXISTS telemetry_device_tag_ts ON telemetry (device_id, tag, ts DESC);

ALTER TABLE telemetry SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'device_id, tag',
    timescaledb.compress_orderby = 'ts DESC'
);

SELECT add_compression_policy('telemetry', INTERVAL '1 day', if_not_exists => TRUE);

SELECT add_retention_policy('telemetry', INTERVAL '30 days', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS tag_latest (
    device_id  text NOT NULL,
    tag        text NOT NULL,
    ts         timestamptz NOT NULL,
    value_num  double precision,
    value_text text,
    quality    smallint NOT NULL DEFAULT 0,
    PRIMARY KEY (device_id, tag)
);

CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_1m
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket(INTERVAL '1 minute', ts) AS bucket,
       device_id,
       tag,
       min(value_num)        AS min_val,
       max(value_num)        AS max_val,
       sum(value_num)        AS sum_val,
       count(value_num)      AS cnt,
       last(value_num, ts)   AS last_val
FROM telemetry
WHERE value_num IS NOT NULL
GROUP BY bucket, device_id, tag
WITH NO DATA;

SELECT add_continuous_aggregate_policy('telemetry_1m',
    start_offset => INTERVAL '3 hours',
    end_offset => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists => TRUE);

SELECT add_retention_policy('telemetry_1m', INTERVAL '365 days', if_not_exists => TRUE);

CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_1h
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket(INTERVAL '1 hour', bucket) AS bucket,
       device_id,
       tag,
       min(min_val)                 AS min_val,
       max(max_val)                 AS max_val,
       sum(sum_val)                 AS sum_val,
       sum(cnt)                     AS cnt,
       last(last_val, bucket)       AS last_val
FROM telemetry_1m
GROUP BY 1, device_id, tag
WITH NO DATA;

SELECT add_continuous_aggregate_policy('telemetry_1h',
    start_offset => INTERVAL '3 days',
    end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '30 minutes',
    if_not_exists => TRUE);

SELECT add_retention_policy('telemetry_1h', INTERVAL '1825 days', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS ingest_errors (
    ts        timestamptz NOT NULL DEFAULT now(),
    topic     text NOT NULL,
    device_id text,
    reason    text NOT NULL,
    payload   text
);

SELECT create_hypertable('ingest_errors', by_range('ts', INTERVAL '1 day'), if_not_exists => TRUE);

SELECT add_retention_policy('ingest_errors', INTERVAL '7 days', if_not_exists => TRUE);

-- ---------------------------------------------------------------- alarms

CREATE TABLE IF NOT EXISTS alarm_rules (
    id          serial PRIMARY KEY,
    name        text NOT NULL,
    device_id   text REFERENCES devices ON DELETE CASCADE,  -- NULL = every device
    tag         text,                                        -- required for high/low/equals
    rule_type   text NOT NULL CHECK (rule_type IN ('high', 'low', 'equals', 'fault', 'offline')),
    threshold   double precision,
    deadband    double precision NOT NULL DEFAULT 0 CHECK (deadband >= 0),
    severity    text NOT NULL DEFAULT 'warning' CHECK (severity IN ('critical', 'warning', 'info')),
    message     text NOT NULL DEFAULT '',
    webhook_url text,
    enabled     boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS alarms (
    id            bigserial PRIMARY KEY,
    rule_id       int REFERENCES alarm_rules ON DELETE SET NULL,
    rule_name     text NOT NULL,
    device_id     text NOT NULL,
    tag           text,
    severity      text NOT NULL,
    message       text NOT NULL,
    trigger_value double precision,
    raised_at     timestamptz NOT NULL DEFAULT now(),
    cleared_at    timestamptz,
    acked_at      timestamptz,
    acked_by      text,
    ack_comment   text
);

CREATE INDEX IF NOT EXISTS alarms_raised_at ON alarms (raised_at DESC);

CREATE INDEX IF NOT EXISTS alarms_active ON alarms (device_id) WHERE cleared_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS alarms_one_active ON alarms (rule_id, device_id) WHERE cleared_at IS NULL;
