# Voices page: tell same-named people apart, and merge duplicates (design, 2026-10-01)

**Owner (2026-10-01):** the Voices page lists two live "Ben Lin" rows (and deleted ones);
"our names can't be merged — and if three people really are all called Ben Lin, how do we
tell them apart?" Approved order: (1) show identity, (4) hide deleted, (2) merge, (3) ask
at naming time. This spec covers 1, 4 and 2. Item 3 follows separately.

**Customer rule:** plain words. Never show a similarity number.

## Why there are two

Unlinked profiles are keyed by (name, who asserted it) on purpose — two people can share a
name, and merging two voices into one profile makes BOTH misrecognised. On TEST company
6a23c57c the two live "Ben Lin" profiles score 0.278 against each other (strangers reached
0.274 in the owner-labelled set), but they were recorded on different devices, which is
known to depress scores (Ben's own clips: 0.54 on his device vs 0.41 elsewhere). The
system cannot decide; a person must, with the evidence in front of them.

## 1. Identity on every row — `GET /api/org/voiceprints`

Each row gains (all nullable; plain values, no scores):
- `linkedAccount`: `{name, email}` of the linked user, or null ("not linked to an account").
- `heardOn`: distinct recording folders the live samples came from (`split_part(s3_key,'/',2)`),
  most samples first, max 3, e.g. `["Ben_Lin_test2", "Ben_UCPK2"]`.
- `firstNamed`: `{at: date, by: "<folder or name of asserted_by/consented_by>"}`.
- `employer`: existing `employer_name`.
- `mergedInto`: `{id, displayName}` when this row was merged (see 2), else null.

## 4. Deleted and merged rows collapsed (frontend only)

Live rows first. Withdrawn/merged rows hidden behind "Show deleted (n)".

## 2. Merge — "Same person as…"

- `GET /api/org/voiceprints/{id}/merge-check?into={target}` →
  `{"verdict": "alike" | "unsure" | "different", "message": "<plain words>"}`
  from the cosine similarity of the two profiles' live-sample means (pgvector avg):
  ≥ 0.50 `alike` ("These sound like the same person."), 0.35–0.50 `unsure` ("These may be
  the same person — recordings from different devices can sound different."), < 0.35
  `different` ("These two voices sound different. Merging them could make <name> harder
  to recognise."). Thresholds from the 2026-09-28 labelled set (strangers ≤ 0.274; genuine
  mostly > 0.35). Either side with no live samples → `unsure`.
- `POST /api/org/voiceprints/{id}/merge` body `{"into": target_id, "confirm": bool}`:
  - same company (from the caller), both live, not the same id; roles `_CORRECTION_ROLES`;
    404 when identity is off.
  - verdict `different` and `confirm` not true → 409 with the message (nothing changes).
  - ONE transaction:
    1. samples: move source samples to the target; a source sample that would collide with
       a target sample on the one-per-second unique index (0070) is deleted instead
       (the target already holds that window).
    2. `speaker_turn_names`: rows with `voiceprint_id = source` → target, and their
       `display_name` becomes the target's. Rows with no voiceprint are NOT touched by
       name: another person may share the name, which is the whole problem.
    2b. every other `voiceprint_id` column (e.g. `site_attendance`) → target.
    3. `speaker_name_proposals`: source → target; drop rows that would collide with an
       existing target row (unique key).
    4. target inherits `user_id` (and link columns) when it has none and the source has one;
       inherits `employer_*` when it has none.
    5. source: `status='withdrawn'`, `merged_into=target`, `merged_at=now()`,
       `merged_by=caller`. Withdrawn is used so every existing `status <> 'withdrawn'`
       reader excludes it without change; `merged_into` tells a merge from a deletion.
  - returns `{"mergedInto": target_id, "samplesMoved": n, "samplesDropped": n}`.
- Migration (next free number): `speaker_voiceprints` adds nullable `merged_into uuid
  REFERENCES speaker_voiceprints(id)`, `merged_at timestamptz`, `merged_by uuid REFERENCES
  users(id)`.

## Rename — `PATCH /api/org/voiceprints/{id}` `{"displayName": "Ben Lin (Cassidy)"}`

So two genuinely different Ben Lins can be told apart in every transcript. Trimmed, 1–80
chars, live profile, same company, `_CORRECTION_ROLES`. Updates the profile and the
`display_name` of its `speaker_turn_names` rows (by `voiceprint_id` only), one transaction.

## Tests that must go red without the change

Identity fields' SQL text; merge-check verdict boundaries; merge refuses `different`
without confirm; collision handling for samples and proposals; cross-company refusal;
the transaction rolls back if any step fails; a merged row shows `mergedInto`. Real DB:
migration + the merge statements on fieldsight_test in a rolled-back transaction.
