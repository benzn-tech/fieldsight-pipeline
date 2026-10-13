-- Glossary entries learned from a person's own correction (owner, 2026-10-02):
-- a third source beside 'correction' (confirmed by hand) and 'manual'.
ALTER TABLE name_aliases DROP CONSTRAINT IF EXISTS name_aliases_source_check;
ALTER TABLE name_aliases ADD CONSTRAINT name_aliases_source_check
  CHECK (source IN ('correction', 'manual', 'learned'));
