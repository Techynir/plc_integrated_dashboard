-- Gateway records now carry their poll time (bursts spaced back by the poll interval), not the
-- server receive time. Update the default checklist text where nobody has edited it.
UPDATE commissioning_items
SET detail = 'The gateway sends no timestamp; each record gets its poll time (bursts are spaced back by the poll interval from their arrival).'
WHERE title = 'Timestamp source'
  AND detail = 'The gateway sends no timestamp; the server''s receive time is used.';
