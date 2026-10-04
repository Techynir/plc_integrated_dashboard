-- Raw data tab filtered by status (rejected / duplicate / ignored): problem messages are rare, so a
-- small partial index finds them without scanning every stored message.
CREATE INDEX IF NOT EXISTS raw_messages_problems ON raw_messages (status, ts DESC) WHERE status <> 'ok';
