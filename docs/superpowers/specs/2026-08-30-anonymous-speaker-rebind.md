# Spec: one session, one speaker namespace

**Status:** proposal, second draft after one review.
**Date:** 2026-08-30. **Task 7** of
`docs/superpowers/plans/2026-08-30-name-a-speaker-build-an-asset.md`.

---

## The measured problem

Batching transcribes a session as ~14 separate ASR calls, and **each call numbers its speakers
from scratch**. Measured on a real 26-minute meeting with speaker ground truth taken from the
user's ears (`2026-08-29-asr-accuracy-measured-findings.md`):

| label | frames | purity against the three real people |
|---|---|---|
| `spk_0` | 2638 | **50.2 %** — Benny 1324 vs Ben 1311, an almost perfect coin flip |
| `spk_1` | 1944 | 62.5 % |

**`spk_0` means a different person in 6 of the 14 calls.** Within any one call the labels are
good — two runs agree 89–100 % on who is speaking — so the failure is entirely *between*
calls, which is exactly what makes it fixable without touching the ASR.

## What this does not fix, and why that matters

**Renaming a speaker is already unaffected by this.** `_propagate` embeds the session's turns
and clusters them **by voice** (`cluster_turns`, tau 0.85), and label inheritance is scoped per
`(source_filename, label)` deliberately, so a cross-call `spk_0` collision cannot merge two
people. An earlier draft of the plan claimed Task 4 was actively wrong without this work; it
was not, and the correction is kept here because the same wrong inference is easy to make
again.

What the 55.4 % damages is everything that **displays** a label. A customer reading a session
sees `Speaker 1` change person six times.

**Scoped honestly: this fixes the transcript viewer and nothing else, yet.** The mapping lives
in Aurora and the only reader that can reach Aurora is org-api. The report generator and the
meeting-minutes lambda are non-VPC and read transcripts straight off S3, and the legacy gateway
reader never sees the new key — all three stay at 55.4 % until they are given a path to the
mapping, which is a separate change. They are unharmed: an absent `speaker_group` reads exactly
as today.

## The approach, and the two alternatives it beats

| approach | extra vendor cost | latency after stop | result |
|---|---|---|---|
| today | — | — | 55.4 % |
| second whole-session ASR at finalize | **+1 full ASR bill** | +87.7 s | one namespace |
| **local ECAPA re-bind** | **0** | **≈106 s on Lambda** (see below) | **96.3 %** |

The unit of decision is the `(call, label)` pair, **not the turn**. That is the whole reason it
works:

| | re-bind | relabel every turn |
|---|---|---|
| decisions | **28** (14 calls × 2 labels) | 354 |
| evidence each | every long turn of that label in that call — median ≈40 s | one turn; **226 of 354 are under 3 s** |
| judgeable | **85.8 % by seconds** | 36 % **by turn count** |

*(Two different denominators. "Only 36 % of turns are long enough" is by count, it rules out
per-turn relabelling, and it says nothing about the re-bind. Stating the denominator is the
part that took a round of confusion to learn.)*

**Nothing here identifies anybody.** The groups are `A`, `B`, `C` within one session. No
biometric data is stored, no profile is created, no consent is required — which is why this is
the unblocked path while the named library waits on a consent surface.

---

## Where it runs — and why finalize is not in the chain

The first draft drew `finalize → embedder → writer` and called it existing plumbing. **Only
the second hop exists.** The review found three separate reasons the first one does not, and
the fix is to delete it rather than build it:

- `SessionFinalizeFunction` has **no `lambda:InvokeFunction` at all** (`template.yaml:2527`) —
  S3 and SES only.
- It has **no database connection**, so it cannot run the tombstone check. A conn-less turn
  read hands the embedder a *deleted* session's turns, which is the "deletion leaks hide in
  frozen copies" defect arriving from a new direction (`lambda_org_api.py:1278`).
- Its artifact carries `sessionId/recipient/folder/date/siteName` and **no `company_id`**
  (`lambda_finalize_claim.py:138`) — the value the table is keyed on.

And a fourth, which is why a synchronous invoke was never safe there: finalize runs Timeout
300 against `LLM_HTTP_TIMEOUT=240`. Appending ~100 s of ONNX after the email send can exceed
it, the S3 trigger retries, and **the confirmation email is sent twice** — the exact failure
`process_finalize_request` declines to re-raise for (`lambda_session_finalize.py:218`).

**The producer is `ItemWriterFunction`**, which is already all four things finalize is not: it
is in-VPC with the psycopg layer, it holds the connection, it knows `company_id`, and it is
already the session-end orchestrator — it writes `session_finalize_requests/` today
(`lambda_item_writer.py:317`).

```
ItemWriter ──S3 put──▶ voiceprint_requests/ ──existing event──▶ SpeakerEmbed ──invoke──▶ Writer
(in-VPC, DB)            (trigger already wired)                 (ECAPA)          (in-VPC)
```

**No new trigger.** The S3 notification on `voiceprint_requests/` is already hand-wired
(`scripts/wire-s3-events.sh:217`), which is the part BUG-33 makes expensive; this reuses it.
One new IAM line — ItemWriter gains `PutObject` on that prefix, where today it has
`match_requests/`, `keyframe_requests/` and `session_finalize_requests/`.

**No new audio grant either.** `SpeakerEmbedFunction` cannot read `transcripts/` and does not
need to: the artifact carries the turns, exactly as the correction artifact does, and the
embedder cuts audio from `users/*` through `_window_audio` — which already handles batched
turns via the batch map and concatenates seam-spanning windows.

The artifact is the existing shape with one field added:

```jsonc
{ "op": "rebind", "company_id": "…", "session_base": "sid…",
  "turns": [ {"source_filename": "…json", "speaker_label": "spk_0",
              "start_sec": 3.16, "end_sec": 112.66}, … ] }
```

`_from_request_artifact` branches on `op` — the correction path is untouched, and a reader
that does not recognise `rebind` skips it rather than mis-handling it.

## What is stored

```sql
CREATE TABLE speaker_label_groups (
  company_id      uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  session_base    text NOT NULL,          -- sid{hex}
  source_filename text NOT NULL,          -- the ASR call
  speaker_label   text NOT NULL,          -- that call's spk_N
  group_label     text NOT NULL,          -- 'A', 'B', … within this session
  spread          real,                   -- how tightly the centroids agreed
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (company_id, session_base, source_filename, speaker_label)
);
```

**A mapping, not a rewrite.** The transcript artifact is untouched, for the reasons
`turn_name_overlay` already documents: extraction re-runs rewrite the artifact and an overlay
survives it; a derived document with two writers is a defect this repo has paid for; and a
mapping can be withdrawn by deleting rows.

**Keyed on `(session, call, label)`, so a re-finalize overwrites in place** — `DELETE` by
`(company_id, session_base)` then insert, inside the transaction, the same shape the item
writer uses. A session re-run must not accumulate two generations of groups whose letters
disagree.

## Where it is applied

`_apply_speaker_names` in org-api already walks `speaker_segments` at read time and attaches
`speaker_name` / `speaker_state`, gated on `SPEAKER_IDENTITY_MODE`. The group joins it:

```jsonc
{ "source_filename": "…_c0004_….json", "speaker_label": "spk_0",
  "speaker_group": "A",                       // ← new
  "speaker_name": "Andy M", "speaker_state": "confirmed" }
```

**`speaker_label` is left exactly as it is.** Overwriting it would destroy the only record of
what the ASR actually said, and the mapping cannot then be checked against anything.

The frontend contract is **`speaker_group || speaker`**, not `|| speaker_label`. The viewer
already groups and colours on the coerced `s.speaker`, and `speaker_label` is `None` for
undiarised files — which is also why it cannot be part of a `NOT NULL` primary key, so those
segments simply have no group.

**`source_filename` must be byte-identical on both sides**, and this is the seam worth a test
rather than a sentence. The read path keys segments on the transcript basename *with* `.json`;
a writer that stores the `.wav` spelling, or one carrying `_off…`, produces **zero groups, zero
errors, and a clean fallback** — indistinguishable from "the re-bind has not run". One test
drives the producer and feeds its output to the read path, in the shape of
`test_embedder_writer_contract.py`.

## The parameters, and how much they are worth

- **centroid from turns ≥ 3 s.** Below that the embedding is not reliable — the same floor
  `_propagate` already uses.
- **average linkage at 0.45.** The measurement found the *same three groups* at 0.35, 0.40 and
  0.45, which is what makes 0.45 defensible rather than tuned. It is settable by environment
  variable without a redeploy, following `VOICEPRINT_MAX_FRAME_SPREAD`.
- **≈106 s on Lambda, not the 55 s the prototype measured.** ONNX costs ~98 ms per second of
  audio *on this runtime* (measured, and written down in `lambda_speaker_embed.py`); 85.8 % of
  a 26-minute session is ~1,084 s of audio. It fits the 600 s timeout and the 1769 MB with
  room, but `ReservedConcurrentExecutions: 5` is **shared with corrections and matches**, so a
  long re-bind occupies a slot the naming path also wants.
- **one meeting, three speakers.** That is the entire evidence base. The next test is a 3+
  speaker toolbox meeting, and label collisions get worse with more people, not better. This
  ships behind `SPEAKER_IDENTITY_MODE` like everything else in this feature.

## What it costs when it fails

Nothing that is worse than today. A session with no groups reads exactly as it reads now: the
overlay attaches no `speaker_group` and the frontend falls back to `speaker_label` at 55.4 %.

The failure worth naming is the opposite one: **groups that are confidently wrong.** Two people
merged into one group read as one person for the whole session, which is worse than an obviously
inconsistent `Speaker 1`. That is why the linkage threshold is not loosened to "get more
merging", and why the spread that produced each group is stored beside it — a group nobody can
audit is a group nobody can withdraw.

## What this does not do

- **No names.** Groups are letters. Naming is Task 2's endpoint and is unchanged.
- **No cross-session identity.** Group `A` in Monday's session and group `A` in Tuesday's are
  unrelated. Linking them is what the voiceprint library is for, and it has consent
  requirements this does not.
- **Nothing for merged multi-device meetings.** Groups are per-session, so a merged view shows
  each member's letters unaligned. Aligning them is a cross-session identity question, which is
  what the voiceprint library is for.
- **No change to `_propagate`.** Whether the group should later become the propagation unit is
  a real question and a separate one; making it now would couple an unproven grouping to the
  path that writes names.

## The decision I need

**Whether to run it on every session or only where it pays.** At ~106 s and a concurrency slot
shared with the naming path, and with most sessions being one person where the re-bind changes
nothing, the pre-check is nearly free: **more than one call AND more than one distinct
`(call, label)` pair**. I lean to including it.

It is listed rather than assumed because a skip is a silence, and this repository has a rule
about those: the producer logs the counts it skipped on, so "one speaker, nothing to do" and
"the producer never ran" are not the same absence.
