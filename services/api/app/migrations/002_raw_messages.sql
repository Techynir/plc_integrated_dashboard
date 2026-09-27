-- Every telemetry message exactly as received, with its processing outcome, for the
-- "Raw data" view and for debugging PLC integrations. Kept 3 days.

CREATE TABLE IF NOT EXISTS raw_messages (
    ts        timestamptz NOT NULL,
    device_id text NOT NULL,
    topic     text NOT NULL,
    payload   text NOT NULL,
    status    text NOT NULL CHECK (status IN ('ok', 'rejected', 'duplicate', 'ignored')),
    detail    text NOT NULL DEFAULT ''
);

SELECT create_hypertable('raw_messages', by_range('ts', INTERVAL '1 day'), if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS raw_messages_device_ts ON raw_messages (device_id, ts DESC);

SELECT add_retention_policy('raw_messages', INTERVAL '3 days', if_not_exists => TRUE);
