-- Valid range: the physically possible values of a tag. A reading outside it (or a status code
-- without a label) is a bad read, e.g. a gateway reading the PLC while it restarts. Bad reads are
-- kept as text for audit but are excluded from values, charts and alarms. This is different from
-- min_value/max_value (the expected operating range), which never hides data.
ALTER TABLE tags ADD COLUMN IF NOT EXISTS valid_min double precision;
ALTER TABLE tags ADD COLUMN IF NOT EXISTS valid_max double precision;
