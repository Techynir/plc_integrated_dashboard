-- Asset console (overview, live, analytics, configuration screens).

-- Tag limits drive alarms (managed alarm rules, see app/limits.py). min_value/max_value are the
-- normal operating range. role says what a tag means to the analytics (speed, moisture, ...).
ALTER TABLE tags ADD COLUMN IF NOT EXISTS limit_dir text CHECK (limit_dir IN ('high', 'low'));
ALTER TABLE tags ADD COLUMN IF NOT EXISTS warn_limit double precision;
ALTER TABLE tags ADD COLUMN IF NOT EXISTS crit_limit double precision;
ALTER TABLE tags ADD COLUMN IF NOT EXISTS role text;
ALTER TABLE tags ADD COLUMN IF NOT EXISTS suppress_when_stopped boolean NOT NULL DEFAULT true;
ALTER TABLE tags ADD COLUMN IF NOT EXISTS guidance text NOT NULL DEFAULT '';

-- Asset metadata: template/type and the gateway connection details (descriptive: the gateway,
-- not this server, talks Modbus to the PLC).
ALTER TABLE devices ADD COLUMN IF NOT EXISTS asset_type text NOT NULL DEFAULT '';
ALTER TABLE devices ADD COLUMN IF NOT EXISTS asset_config jsonb NOT NULL DEFAULT '{}';

-- Alarm engine: on/off delays against chattering, suppression while the machine is stopped
-- (and for 90 s after restart), a "machine stopped" rule type, and rules generated from tag limits.
ALTER TABLE alarm_rules ADD COLUMN IF NOT EXISTS on_delay_s real NOT NULL DEFAULT 0;
ALTER TABLE alarm_rules ADD COLUMN IF NOT EXISTS off_delay_s real NOT NULL DEFAULT 0;
ALTER TABLE alarm_rules ADD COLUMN IF NOT EXISTS suppress_when_stopped boolean NOT NULL DEFAULT false;
ALTER TABLE alarm_rules ADD COLUMN IF NOT EXISTS guidance text NOT NULL DEFAULT '';
ALTER TABLE alarm_rules ADD COLUMN IF NOT EXISTS managed_by text;
ALTER TABLE alarm_rules DROP CONSTRAINT IF EXISTS alarm_rules_rule_type_check;
ALTER TABLE alarm_rules ADD CONSTRAINT alarm_rules_rule_type_check
    CHECK (rule_type IN ('high', 'low', 'equals', 'fault', 'offline', 'stopped'));
CREATE UNIQUE INDEX IF NOT EXISTS alarm_rules_managed ON alarm_rules (managed_by) WHERE managed_by IS NOT NULL;

ALTER TABLE alarms ADD COLUMN IF NOT EXISTS context text NOT NULL DEFAULT '';

-- Stoppage log: reason per stop (identified by device and the stop's start time).
CREATE TABLE IF NOT EXISTS stoppage_reasons (
    device_id  text NOT NULL REFERENCES devices ON DELETE CASCADE,
    started_at timestamptz NOT NULL,
    reason     text NOT NULL,
    set_by     text NOT NULL,
    set_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, started_at)
);

-- Commissioning checklist per asset.
CREATE TABLE IF NOT EXISTS commissioning_items (
    id         serial PRIMARY KEY,
    device_id  text NOT NULL REFERENCES devices ON DELETE CASCADE,
    position   integer NOT NULL DEFAULT 0,
    title      text NOT NULL,
    detail     text NOT NULL DEFAULT '',
    status     text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'assumed', 'done')),
    updated_by text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS commissioning_items_device ON commissioning_items (device_id, position);
