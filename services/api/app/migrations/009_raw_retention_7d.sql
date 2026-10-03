-- Keep raw MQTT messages (Raw data tab) for 7 days instead of 3, so a PLC that has been silent
-- over a long weekend still has its last messages available. Parsed telemetry is kept 30 days.
SELECT remove_retention_policy('raw_messages', if_exists => TRUE);
SELECT add_retention_policy('raw_messages', INTERVAL '7 days', if_not_exists => TRUE);
