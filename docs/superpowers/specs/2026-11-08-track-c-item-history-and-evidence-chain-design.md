# Track C, slice 1: shared item history and the evidence chain (design)

**Date:** 2026-11-08 · **Status:** design, approved by the owner 2026-11-08 (§9) · **Branch:** `feature/track-c-event-links`
**Inputs:** assessment `specs/2026-09-24-event-graph-and-jev-assessment.md` §2.4, §2.5, §6 (Track C);
Track A verdict `specs/2026-09-28-jev-shadow-eval-findings.md` §8 (Jev is not the decision centre);
Track B plan and the continuity plan `plans/2026-09-30-extractor-declares-item-continuity.md` (template
for pre-registered bars); cross-session agreement with the status-suggestions line and jev TrackB,
2026-11-08 (recorded in §6).

---

## 1. Why

Three lines need the same missing thing: **a way to look back at a site's earlier items**.

- **Status suggestions** ("copper pipe for the west block is ordered, done") can only match an action
  item from an earlier pass of the *same session*. Assigned Monday, reported Wednesday is invisible.
  The DB already holds everything (`action_items.site_id/status/stable_id`); nothing at extraction
  time reads it.
- **Cross-day continuity** (jev TrackB) needs a site-scoped prior source across sessions.
- **The evidence chain** (owner, 2026-11-08): from any item, reach the words it came from (which audio,
  which second, who said it). Today evidence quotes exist only at topic level; the extractor strips
  any child-level evidence.

The owner's ruling: **store first, display later.** The relationships go into Aurora now because
(1) we do not yet know every future use and stored data extends easily, and (2) a stored chain makes
later evidence-chain extraction fast. No front-end tag. Report wording is a later experiment (one or
two versions), not this slice.

## 2. Goals

1. **`item_history.find_items`**: one site-scoped read function over the four item kinds, that the
   status line's reconcile step and TrackB's cross-day prior both build on. Delivered first; the
   status line is waiting on it.
2. **`event_links`**: one edge table for every item relationship. This slice writes only
   `supported_by` (item → verified transcript turn). The table reserves `resolves` (status line,
   statement → item, written on human confirm), `continues` (jev TrackB, same task across sessions)
   and `supersedes` (different item replaces an older one; not modelled yet).
3. **`supported_by` by heuristic**, no prompt change: pure code links each item to the transcript
   turn it most plausibly came from, under a high floor and a tie margin. Wrong links are worse than
   missing links.
4. **`claim_type`** stored on the four item tables, *derived* by code from the link and the
   speaker's identity, never self-reported by the model (assessment §2.4).
5. ~~Owner prep for prod `EMIT_EVIDENCE`~~ **Dropped 2026-11-08:** the repo variable
   `PROD_EMIT_EVIDENCE` has been `true` since 2026-10-12 (`gh variable list`), so prod finals already
   carry verified topic quotes. The assessment's "prod off" was stale. Remaining prod gap for
   `claim_type`: `PROD_SPEAKER_IDENTITY_MODE=shadow`, which computes and logs but writes no
   `speaker_turn_names` (`lambda_org_api.py` `willWriteNames` only under `on`). TEST is `on`.

## 3. Non-goals and anti-goals

- **Not** writing `resolves`, `continues` or `supersedes` rows. The table and API carry them; the
  writers belong to the status line and TrackB, each with its own bars and `decision_records` kind.
- **Not** a per-item citation prompt change. Measured cost (45 TEST finals since 2026-10-01: median
  10 items/session, quote median 79 chars): copying a quote per item adds ~20% output tokens;
  pointing at an existing topic-quote index adds ~3%. Either is a later option only if the heuristic's
  coverage is too low (§5.3).
- **Not** displaying anything: no UI tag, no report wording, no Ask change.
- **Not** Jev. Track A closed it; it may later be one more signal behind `decision_records`.
- **Not** entity anchoring (assessment (d)) and not Procore (paused).
- **Anti-goal:** a link that points at the wrong words. It would make the evidence chain lie, which
  is worse than no chain. Every threshold is chosen for precision first.
- **Anti-goal:** `observed` being read as "true". As with `evidence_match`, `observed` only means the
  recorder said it themselves. Nothing may present it as verified fact.
- **Anti-goal:** any path that crosses company or site, or keeps showing a deleted/redacted item
  through a link.

## 4. Constraints found in the code

- **The extractor is not in the VPC.** `ExtractSessionFunction` (`src/template.yaml`) has no
  `VpcConfig` and no DB access; it needs the internet for the model. The in-VPC functions with PG
  credentials (item writer, ingest, org-api, voiceprint writer and ~20 others) have no internet (BUG-36). So `find_items` is a repository function
  callable only from in-VPC code, and anything the extractor needs from Aurora has to travel through
  S3 (the existing `match_requests/` + `lambda_programme_matcher` pattern).
- **Speakers are joinable, with tolerance.** Transcript turns carry a diarization `speaker` label.
  `speaker_turn_names.turn_ref` is `_stem(source_filename)@start_sec` (`turn_name_overlay.py:113`),
  and re-extraction can shift `start_sec`, so every reader matches with `TOLERANCE_SEC = 0.5` overlap
  (`turn_name_overlay.py`, `repositories/voiceprints.py:1100`), never by equality. **Decision (my
  call):** `dst_turn` stores `{source_stem, start_sec, end_sec, speaker_label, quote_status}` and
  joins to names through `turn_name_overlay`'s matcher, not a new one.
- **Names arrive after the write.** `speaker_turn_names` for a session are written by the voiceprint
  writer, asynchronously, after the item writer has finished (`lambda_item_writer.py` `_request_match`
  → `voiceprint_requests/` → SpeakerEmbed → VoiceprintWriter). So at item-write time a fresh session
  has no named turns.
- **The existing lexical search drops Chinese.** `search_sql._lexical_vector` strips `[^a-zA-Z0-9]`.
  Items such as "二楼防水膜" would score zero, so `find_items` cannot reuse it.
- **Rows are re-inserted every pass.** `created_at` on a live item row is the latest pass, not first
  appearance. "When did this item first appear" is `min(created_at)` over all rows sharing the
  `stable_id`. Superseded passes survive under Track B, so the history is there.
- **Status vocabulary:** `open, in_progress, blocked, done, closed` (`action_pack_stages`);
  open = `open, in_progress, blocked`. Questions: `open, answered, dropped`.
- **No place column on items.** Track L's `place_key` is computed at read time.

## 5. Design

### 5.1 `event_links` (one migration, next free number at merge, currently 0097)

```
event_links
  id               uuid PK
  company_id       uuid NOT NULL → companies ON DELETE CASCADE
  site_id          uuid NOT NULL → sites     ON DELETE CASCADE
  link_type        text NOT NULL CHECK IN ('supported_by','resolves','continues','supersedes')
  src_kind         text NOT NULL CHECK IN ('action_item','finding','decision','question')
  src_stable_id    uuid NOT NULL            -- the item's cross-session identity
  src_row_id       uuid                     -- the per-pass row, for supported_by (see deletion)
  dst_kind         text NOT NULL CHECK IN ('turn','action_item','finding','decision','question')
  dst_stable_id    uuid                     -- item → item links
  dst_turn         jsonb                    -- supported_by: {source_stem, start_sec, end_sec, speaker_label, quote_status}
  method           text NOT NULL            -- 'lexical_v1' | 'human' | ...
  score            real
  rule_version     text NOT NULL
  decision_record_id uuid                   -- when a gated judgement produced it
  created_at       timestamptz NOT NULL DEFAULT now()
  CHECK (dst_kind = 'turn') = (dst_turn IS NOT NULL AND dst_stable_id IS NULL)
```

**Decisions (my calls, after the builder check):**
- **No `company_id` column.** Item tables carry only `site_id`; a project moving company changes only
  `sites.company_id` (project-owned tenancy). A denormalised `company_id` would go stale on a move,
  and cascade wrongly when the old company is deleted. Company is derived through `sites`.
- **`src_row_id` is four nullable FK columns** (`src_action_item_id`, `src_finding_id`,
  `src_decision_id`, `src_question_id`), each `ON DELETE CASCADE`, with a CHECK that exactly the one
  matching `src_kind` is set for `supported_by`. One column cannot reference four tables.

- **Scoping** follows `scope.visible_scope`: every read filters on the caller's site set.
- **Identity:** `stable_id` is *the* cross-session identity (TrackB, 2026-11-08). `item_id` is lineage
  inside one source key and is never a link endpoint.
- **`supported_by` belongs to a pass.** Each pass writes links for its own rows (`src_row_id`); readers
  take the links of the live row. Superseded passes keep theirs as history.
- **Deletion and redaction must reach the table.** Three paths, each with a real-Postgres test:
  1. a physical delete of an item row removes its `supported_by` links (`src_row_id` FK, `ON DELETE
     CASCADE`);
  2. item → item links have no FK, so every reader joins both endpoints to a *visible* live row through
     `deleted_predicates`; a redacted or deleted endpoint makes the link invisible. Topic deletion is a
     revocable tombstone (`redactions.reverted_at`), so links are hidden, never deleted, and come back
     on undelete. Only the physical delete functions (`delete_topics_for_source[_prefix]`) remove
     rows, and those cascade through path 1;
  3. a company or site delete cascades. `company_records_export` lists the table, so company offboarding
     exports it too.
- **No customer text is stored in the table.** It holds a locator and the speaker label only. The
  words live in the transcript, where redaction already applies.

### 5.2 `item_history.find_items` (the shared read API)

```
find_items(conn, *, company_id, site_id, kinds,
           statuses=None,            # dict kind -> list, e.g. {'action_item': ['open','in_progress','blocked']};
                                     # None or a kind absent from the dict = no filter for that kind;
                                     # an empty list is rejected; a status filter on 'decision'
                                     # (no status column) or on 'finding' (free text) is rejected
           since=None, until=None,   # on first_seen's source-session date (NZ date)
           recorded_by=None,         # source user_folder (the recorder), not `responsible`
           responsible=None,
           exclude_source_keys=None, # the calling session's own keys
           query=None,               # text; ranks, never filters
           limit=10)
→ list of {
    kind, stable_id, item_id (lineage only), text, responsible, deadline, status,
    first_seen (min created_at over the stable_id), source {user_folder, date, session_base,
    source_s3_key}, recorded_by, carried_from,
    score, matched_terms,
    place (null in this slice),
    origin (null, or {source_stem, start_sec, end_sec, speaker_label, quote_status} from the live
            row's supported_by link),
    first_seen_recorded_at (aware UTC recording START of first_seen's session, or null:
            meeting_session.opened_at for a sid{32hex} base, else the legacy stem's NZ wall
            clock via nz_time, else null -- group extraction, other key, missing row) }
```

- `company_id` and `site_id` are mandatory. Only live, unsuperseded, unredacted rows are returned, via
  the shared predicates in `deleted_predicates`, never inlined.
- **Ranking is done in Python** over the SQL-filtered candidates, using ASCII word tokens plus CJK
  character bigrams, and is tested on a mixed 中文/English fixture. With no `query`, score is 0 and
  the order is recency.
- **Order:** `score DESC, first_seen DESC, stable_id ASC`, which is deterministic.
- Target latency: under 2 s per call on a site's open items.
- **What may leave the VPC** (agreed with the status line): a request artifact for a non-VPC worker
  carries, per candidate, only `kind, stable_id, text, status, responsible, deadline, first_seen
  session date, first_seen_recorded_at, recorded_by, score, matched_terms, place`. Never `origin`,
  never other sessions' transcript text.

### 5.3 `supported_by` by heuristic

- **Tiers (my call):** final only. Group extractions have no shared clock and their evidence is
  stripped; live passes re-run every interval and would churn rows. Coverage denominators count
  final items only.
- **Not gated on `EMIT_EVIDENCE`** (moot on prod, where it is on), but the two candidate paths are
  measured separately and a path ships only if its own stratum passes the bars.
- **Where:** in `lambda_extract_session`, after evidence verification, as a pure module
  (`item_support.py`). That is the only place that holds the turns' *text*; the final artifact's
  `speaker_turns` carry labels and times only. For each child item it
  scores the item text against (a) the topic's verified quotes' turns, then (b) all turns inside the
  topic's `time_range`. The scorer uses the same ASCII + CJK-bigram tokens as `find_items`. It
  accepts the top turn only if `score ≥ floor` and `score − second ≥ margin`. Otherwise it records
  nothing.
- **Output:** a `support` object on each child in the artifact: locator, speaker label, score, method,
  rule_version. `lambda_item_writer` writes it to `event_links` and never re-derives it.
- **Flag:** `EMIT_ITEM_SUPPORT`, default `false` everywhere, wired template → both workflows → env.
  Off means the artifact is byte-identical to today's.
- **Floor and margin are not guessed.** They are fixed on a fit set, then judged on a held-out set
  against pre-registered bars (§7), before the flag goes on anywhere.

### 5.4 `claim_type`

- **Columns** on the four item tables: `claim_type text NULL CHECK IN ('observed','stated','inferred')`
  and `claim_basis jsonb` (link id, speaker identity used, rule_version).
- **Rule v1,** computed by `lambda_item_writer` in-VPC at write time:
  - **no link and nothing close in the topic's turns:** `inferred`; no link but weak or tied candidates: `NULL` (§9.3).
  - **linked turn's speaker identified as the recorder**, through `speaker_turn_names` → voiceprint →
    user = the session's user: `observed`.
  - **linked turn's speaker identified as someone else:** `stated`.
  - **speaker not identified:** `NULL`, meaning unknown. The rule never guesses.
- **When it is computed (pending owner, see §9):** at item-write time names do not exist yet (§4),
  so the writer can only store `inferred` or `NULL`. `observed` / `stated` appear only when the
  computation is re-run after the voiceprint writer has written that session's names. The design is
  one pure function `claim_type.derive(link, named_turns, recorder_user_id)`, called by the item writer
  at write time and by a recompute step after names land. A backfill script uses the same function.
- **Honest coverage:** on TEST (`SPEAKER_IDENTITY_MODE=on`) all three values can appear. On prod
  (`shadow`, no names written) items are only `inferred` or `NULL` until the owner turns speaker
  identity on.

### 5.5 Prod `EMIT_EVIDENCE`: already on

`PROD_EMIT_EVIDENCE=true` since 2026-10-12, so nothing is left to prepare. The prod flags this slice
adds (`PROD_EMIT_ITEM_SUPPORT`) default `false` and are the owner's to flip after the bars pass.

## 6. Interfaces with other lines (agreed 2026-11-08)

| Line | Gets from Track C | Owns |
|---|---|---|
| Status suggestions (待办状态+语音建议) | `find_items` with `exclude_source_keys`, `recorded_by`, `first_seen`, `score/matched_terms`, open statuses, CJK ranking, deterministic order; `event_links.link_type='resolves'` | the in-VPC → S3 request artifact, the non-VPC judge worker, its widening steps and twin-item tests, writing `resolves` on confirm |
| Continuity (jev TrackB) | `find_items` as a cross-day prior source (limit 40, recency order); `link_type='continues'` reserved | the cross-day prior block design (the extractor is non-VPC, so a prior must reach it through S3), the `continues` writer, its bars, its `decision_records` kind. A cross-day continues is never a carry: no `carried_from`, no shared `stable_id` across two live topics |

## 7. Measurement (pre-registered before any result)

Modelled on the continuity plan. The rules are frozen once the first result row exists; later changes
go in dated addenda.

- **Sets:** TEST finals with evidence (≥ 40 sessions) are split into a fit half and a held-out half by
  session. Prod reads only if the owner provides them.
- **Gold:** for each item, a blind labeller (agent) marks the turn it came from, or "none / several".
  The owner adjudicates a sample, about 40–50 items, plus every agent-vs-heuristic disagreement in
  that sample.
- **Bars on the held-out half (all must pass before `EMIT_ITEM_SUPPORT` is on anywhere):**
  1. **Link precision:** wrong links / accepted links, with a one-sided 95% Clopper–Pearson upper
     bound ≤ 5%. A link to an adjacent turn by the same speaker carrying the same words counts as
     correct.
  2. **Twin hard negatives** (two items differing only in unit/block/level): 0 links to the twin's turn.
  3. **Run-to-run stability:** the heuristic is deterministic, so two runs must be identical (a check
     on the code, not on noise).
  4. **Agent–owner agreement** on the adjudicated sample: ≥ 85%, the point bar used by continuity.
- **Reported, not gated:** coverage (items with a link), coverage per kind, per language, per
  `evidence_status`; `claim_type` distribution on TEST; `find_items` latency on the largest TEST site.
- **Coverage decision (owner's):** if held-out coverage is below the owner's chosen line, the next
  step is to measure the topic-quote-index prompt option (+~3% tokens) as a separate pre-registration.
- **`claim_type` v1** is a deterministic join with no model, so it has no accuracy bar of its own here.
  Its correctness rests on bar 1 plus speaker identity. A labelled check of `observed` vs `stated`
  waits until prod speaker identity is on.

## 9. Owner decisions (2026-11-08, approved)

1. **Recompute after names land:** the voiceprint writer, in-VPC, calls the per-session
   `claim_type` recompute right after writing that session's `speaker_turn_names`. It is one function
   call, with no new Lambda and no model. A backfill script uses the same function.
2. **Coverage line = 60%.** If held-out link coverage on final items is below 60%, the next step is a
   separate pre-registration measuring the topic-quote-index prompt option (+~3% output tokens).
   At or above 60%, the prompt stays unchanged.
3. **`inferred` is narrowed** (rule v1 amended before any result). An item is `inferred` only when the
   matcher found *nothing close* to it in the topic's turns, with best score below a low "nothing
   close" floor, the same idea as `evidence_match.NOTHING_CLOSE_RATIO`. An item with candidates that
   were too weak or tied is `NULL` (unknown). Otherwise at 60% coverage, most of the 40% unlinked
   items, which someone did say, would be mislabelled `inferred`. The nothing-close floor is fixed
   on the fit half like the others, and is checked by bar 5 below.
4. **A nightly model re-match agent** (owner's idea) belongs to the site decision loop
   (`specs/2026-11-05-site-decision-loop-design.md`, architecture line) as its first scheduled-scan
   handler, with a separate design. It is not built by Track C. Agreed constraints:
   - silent writes only in the store-only layer (`event_links` repairs, `claim_type`), tagged
     `method='nightly_*'`, reversible;
   - anything user-visible or state-changing (status, `continues`, `resolves`) is a proposal for a
     person;
   - each judgement kind has its own `decision_records` kind and bars.
   Track C provides it `find_items`, `event_links` and the recompute entry point.

Bar 5 (added with decision 3): **`inferred` precision**. Among held-out items the rule labels
`inferred`, the share that gold says some turn did support is ≤ 10%, judged by the one-sided 95%
Clopper–Pearson upper bound. Below 20 such items it is reported only.

## 8. Left to the builder

Module and function names inside the files; how `find_items` builds its SQL (one query or two); the
exact tokeniser, provided it shares one implementation between `find_items` and `item_support`; the
harness layout under `scripts/item_support_eval/`; the runbook format.
