-- Devices created by the web simulator (MQTT topic root sim/ instead of plc/). They are
-- flagged in the dashboard and purged automatically once idle.
ALTER TABLE devices ADD COLUMN IF NOT EXISTS simulated boolean NOT NULL DEFAULT false;
