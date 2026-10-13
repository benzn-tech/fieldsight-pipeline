-- Ruling R21. supersede_topics_for_source[_prefix] (Track B Task 3) mark a topic retired
-- instead of deleting it, so report_chunks.topic_id's `ON DELETE SET NULL` never fired for a
-- topic superseded before this fix: the chunk stayed bound to the retired topic,
-- visible_chunks_predicate's superseded-topic arm hid it, and the new live topic that
-- replaced it had no chunks of its own until that day's report was re-ingested (may never
-- happen for an older day) -- a session silently dropped out of search/Ask. One-off repair
-- for rows already in that state; supersede_topics_for_source[_prefix] now unbind on every
-- future call, so this migration has nothing left to do after it runs once.
UPDATE report_chunks SET topic_id = NULL
WHERE topic_id IN (SELECT id FROM topics WHERE superseded_at IS NOT NULL);
