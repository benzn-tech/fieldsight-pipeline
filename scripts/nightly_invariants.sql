-- Nightly re-match invariants (spec 2026-11-10-nightly-rematch-agent-design.md §5, §7 Engineering).
-- Each statement is a SELECT that must return ZERO rows. Every returned row is a violation.
-- Read-only: run in a READ ONLY transaction, e.g.
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c 'SET default_transaction_read_only = on' -f scripts/nightly_invariants.sql
-- Every query returns a site_id column so a caller can scope the result to its sites.
-- A nightly link is an event_links row with method LIKE 'nightly\_%' (underscore escaped).
-- Each statement is headed by a "-- name:" line and is ONE statement ending in a semicolon.
-- tests/integration/test_nightly_invariants.py loads the file by those headers.

-- name: nightly_link_without_nightly_support_record
SELECT e.id AS link_id, e.site_id, e.decision_record_id
FROM event_links e
WHERE e.method LIKE 'nightly\_%' ESCAPE '\'
  AND NOT EXISTS (SELECT 1 FROM decision_records d
                  WHERE d.id = e.decision_record_id AND d.kind = 'nightly_support');

-- name: two_nightly_links_for_one_source_row
-- Not grouped by site: two nightly links on one source row are a violation even when the
-- links name different sites. site_id (the first; uuid has no min()) is kept for scoping.
SELECT (array_agg(e.site_id ORDER BY e.site_id))[1] AS site_id, e.src_kind,
       COALESCE(e.src_action_item_id, e.src_finding_id, e.src_decision_id,
                e.src_question_id) AS src_row_id,
       count(*) AS links
FROM event_links e
WHERE e.method LIKE 'nightly\_%' ESCAPE '\'
GROUP BY e.src_kind,
         COALESCE(e.src_action_item_id, e.src_finding_id, e.src_decision_id, e.src_question_id)
HAVING count(*) > 1;

-- name: nightly_link_not_supported_by
SELECT e.id AS link_id, e.site_id, e.link_type
FROM event_links e
WHERE e.method LIKE 'nightly\_%' ESCAPE '\'
  AND e.link_type <> 'supported_by';

-- name: two_nightly_reemits_for_one_line_n_l
-- A dropped-request re-emit (no L) is once per (line, N, night) by design, so it is grouped
-- by night as well. Not grouped by site: the key is global, like the table's UNIQUE.
SELECT (array_agg(r.site_id ORDER BY r.site_id))[1] AS site_id, r.line, r.n_extraction_key,
       r.l_extraction_key, CASE WHEN r.l_extraction_key IS NULL THEN r.night END AS night,
       count(*) AS reemits
FROM nightly_reemits r
GROUP BY r.line, r.n_extraction_key, r.l_extraction_key,
         CASE WHEN r.l_extraction_key IS NULL THEN r.night END
HAVING count(*) > 1;

-- name: nightly_link_on_row_not_live_at_write
-- Best effort: the source row's topic was superseded before the link was created.
SELECT e.id AS link_id, e.site_id, t.id AS topic_id, t.superseded_at, e.created_at
FROM event_links e
LEFT JOIN action_items ai ON ai.id = e.src_action_item_id
LEFT JOIN findings fi ON fi.id = e.src_finding_id
LEFT JOIN topic_decisions de ON de.id = e.src_decision_id
LEFT JOIN topic_questions qu ON qu.id = e.src_question_id
JOIN topics t ON t.id = COALESCE(ai.topic_id, fi.topic_id, de.topic_id, qu.topic_id)
WHERE e.method LIKE 'nightly\_%' ESCAPE '\'
  AND t.superseded_at IS NOT NULL
  AND t.superseded_at < e.created_at;
