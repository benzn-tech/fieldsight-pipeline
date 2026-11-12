# A non-work topic's time range no longer cuts out the work said inside it (2026-11-13)

## Problem (measured on TEST 2026-11-13, session sid1fa36140..., request 280473f4)

The 4-speaker pre-roof walk scatters chit-chat (coffee, the game, a relative's
inspection) between work lines. The extraction gathered it into ONE non-work topic,
"Personal Banter", `time_range` "00:36 – 00:40", which covers the whole walk. The
day's work topics in the same minutes were all classified `work`.

`lambda_org_api._excluded_topics_for` sends every excluded topic (redacted,
non_work, or under a deleted prefix) with its whole `time_range`.
`transcript_window.excluded_spans` / `drop_spans` then remove every turn that overlaps
it. The walk call received 171 of 898 words. Unit 2 and Unit 3 came back empty, and
Unit 1 had one answer.

The same code is on prod (life-conversation separation, 2026-07-23), so any report
with banter spread through work loses that work. The earlier run on 2026-11-12 had
separate short banter topics, which is why it worked.

## Decision

An **automatic** `non_work` classification cuts only the parts of its range that no
`work` topic of the same scope covers. Work said in those minutes wins over a coarse
non-work range.

- Applies only to rows excluded **only** because `work_class == "non_work"`. An active
  redaction (a person confirmed "personal, remove") and a deleted-prefix row still cut
  their whole range: that is an explicit human choice, and the report must not show
  what they removed.
- The work cover is the union of the `time_range`s of the rows in the same scope that
  are NOT excluded (same `session_id` filter, extraction kind only). Each range is
  widened to whole minutes, `[HH:MM:00, end HH:MM:59]`, because extraction ranges are
  minute-granular ("00:39 – 00:39" means the minute).
- The non-work range minus the cover gives zero or more pieces. Each piece is sent as
  `{"id": <same id>, "time_range": "HH:MM:SS – HH:MM:SS"}`, which
  `chunking.parse_time_range` already reads. No pieces means nothing is sent for that
  topic.
- A non-work row whose `time_range` does not parse is sent unchanged (fail closed, as
  today). A work row whose range does not parse adds nothing to the cover.
- The topic list rules do not change: non-work rows stay out of the report's topic
  list (`_report_rows_in_scope`), and the UI still shows them under "suspected
  personal".

## Privacy trade-off (stated, accepted)

Banter lines said inside a work topic's minutes now reach the report model. They
already did whenever the extraction kept banter inside a work topic, which is the
common case. The report prompt already says "Do not quote ... personal remarks". The
classifier's own rule is "uncertain -> work". An explicit "personal, remove" still
cuts the whole range.

## Not changing

The worker (`transcript_window`) and the request shape (`excludedTopics` stays a list
of `{id, time_range}`; one id may now appear more than once). Redactions, deletions,
RAG and rollups are untouched.

## Verification

1. Unit tests on `_excluded_topics_for`:
   - a non-work range fully covered by work ranges sends nothing;
   - one partly covered sends the uncovered pieces;
   - a redacted topic and a deleted-prefix topic send the whole range even when covered;
   - an unparseable non-work range is sent unchanged;
   - an unparseable work range covers nothing;
   - the session filter applies to the cover too;
   - minute widening: a work "00:39 – 00:39" covers 00:39:00-00:39:59.
2. TEST: regenerate the pre-roof walk report for sid1fa36140 (request it again through
   org-api). The walk call sees all ~898 words, and Units 1-3 have answers.

## Fable review 2026-11-13: approved with changes (applied)

1. The non-work expiry sweep (`lambda_nonwork_expiry`, 24 h, reason
   `non_work_auto_expiry`, actor_role system) writes a redaction for the same topic.
   It counts as automatic, not human. Otherwise the bug returns a day later.
   `org.AUTO_NON_WORK_REDACTION` is pinned to the sweep's REASON by a test.
2. One rule: `_exclusion_reason` returns "deleted" | "redacted" | "auto_non_work" | None.
   `_redacted_or_hidden` is its yes/no.
3. Pieces are closed seconds next to the cover ([cover_end+1, cover_start-1]). The
   cover is widened to whole minutes; the cut range is not (deliberate). An inverted
   range ("23:58 - 00:02") cannot be placed and is sent unchanged.
   `transcript_window.uncovered_pieces` is the shared helper.
4. Follow-ups, not in this PR:
   - The search index (`session_index.py` ~196-215) drops turns in non_work spans the
     same way. It cannot tell human from automatic until `index_request.py` carries a
     flag.
   - Remaining pieces still cut whole turns (`drop_spans` before `clip_to_window`).
   - An extraction-time rule so a non_work topic cannot span minutes that work topics
     occupy (`is_mixed` exists for this).
   - The privacy guard on mixed sessions is human redaction. The "do not quote personal
     remarks" prompt line is not a measured guard.
5. The frontend does not read `excludedTopics` (checked fieldsight-ui).
