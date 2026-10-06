-- Process rules (app/process_rules.py, ingestor/process_rules.py): alarms that compare several
-- signals, each explained in plain words: what is happening, why, and what to do next.
ALTER TABLE alarm_rules DROP CONSTRAINT IF EXISTS alarm_rules_rule_type_check;
ALTER TABLE alarm_rules ADD CONSTRAINT alarm_rules_rule_type_check
    CHECK (rule_type IN ('high', 'low', 'equals', 'fault', 'offline', 'stopped', 'process'));
ALTER TABLE alarms ADD COLUMN IF NOT EXISTS explain jsonb;
