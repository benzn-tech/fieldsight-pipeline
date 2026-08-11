# Plan — speaker identity implementation (Phases B–D, post-Phase-0)

**Status:** plan · 2026-08-11
**Unlocked by:** `specs/2026-08-11-speaker-phase0-results.md` (Phase 0 gate passed — with the
caveats in its "Review corrections" section, which this plan treats as binding).
**Contract:** `specs/2026-08-09-speaker-identity-v2.md` — §1 (a wrong confident name never
reaches extraction/reports/email), §6 (audit + withdrawal, consent), §8 (schema), §9
(threshold is measured, not chosen, per condition class), §10 (consent + tenancy).
**Sequencing inherited from:** `plans/2026-08-08-speaker-identity.md` — especially Phase D-2's
already-made decision: identity never gates the finalize email; it runs as **one bounded
re-run with its own budget and explicit email suppression**.

This plan is executable by a fresh session with no other context. Every phase: tests FIRST
(strict TDD — write the failing test, watch it fail, then implement), exact files, rollback,
and an **inert/live** marker. Repo unit tests use the FakeConn/FakeCursor double (mirror
`tests/unit/test_action_items_repo.py`) — no real Postgres, and nothing in a unit test may
require the pgvector extension. Run tests with:

```
export UV_LINK_MODE=copy
export AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=ap-southeast-2
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit -q
```

## Facts verified against the repo on 2026-08-11 (do not re-derive from the older specs)

- **Next migration number is `0038`** (`src/migrations/` tops out at `0037_topic_evidence.sql`).
  The v2 design's §8 comment agrees.
- The `speaker_count` computation sites in `src/lambda_extract_session.py` have moved: they are
  now **`:1272` (group merge)** and **`:1523` (single device)** (the specs cite 1240/1491).
  The consumer gate in `src/lambda_item_writer.py` is now **`:624`** (`== 1` resolves a
  self-referential responsible party), not `:580`.
- `FINAL_RERUN_MAX_GENERATIONS` (`lambda_extract_session.py:485`, default 3) is the shared
  growth-re-run budget — the identity re-run must NOT consume it.
- `lambda_item_writer.py:315` writes `session_finalize_requests/{sid}-updated.json` on update
  — the re-email path the identity re-run must suppress.
- `ExtractSessionFunction` is **non-VPC** (needs internet for Claude). `ItemWriterFunction` /
  `SuggestionWriterFunction` are the in-VPC shape (PsycopgLayer + VpcConfig + PG env). The
  established split (template comment at `template.yaml:2436–2455`, BUG-36): in-VPC functions
  have **only an S3 gateway endpoint** — no internet at all. `MatcherFunction` →
  `SuggestionWriterFunction` is the precedent for direct Lambda invoke across that boundary.
- S3 event notifications are managed **manually outside the stack** (BUG-33,
  `scripts/wire-s3-events.sh`) and prefixes must not overlap existing configs. This plan uses
  **direct Lambda invoke only** — no new S3 events.
- The three-segment switch discipline (repo variable → workflow `--parameter-overrides` →
  template Parameter) is pinned by `tests/unit/test_template_workflow_parameter_wiring.py`.
  Non-boolean parameters need their own explicit test there (see
  `test_the_upload_verify_mode_is_wired_in_both_environments` as the pattern).
- Migration-text unit tests are a repo convention: `tests/unit/test_migration_0030_devices.py`
  asserts the SQL string (idempotency + the exact columns dependents read).

## Two decisions this plan makes explicit

**D1 — Where embeddings are computed: a new `SpeakerEmbedFunction`, in-VPC.**
The embedder is a local model; it makes no external calls at inference. Putting it in-VPC
(mirroring ItemWriterFunction's Layers/VpcConfig/PG env) turns BUG-36 into a guarantee: the
model **cannot** silently download from HuggingFace at cold start, because nothing outside the
S3 gateway endpoint is reachable — so a packaging mistake fails loudly at the parity test, not
quietly in prod. It reads raw audio from `users/…/audio/` via the S3 gateway endpoint and
writes Aurora directly. It is invoked **only** by direct Lambda invoke (from org-api for
enrolment, from extract-session for matching).

**D2 — Model packaging: ONNX in the zip/layer first; container image only as fallback.**
Phase 0 ran SpeechBrain ECAPA-TDNN (192-d) under torch. torch + speechbrain is far beyond the
250 MB unzipped Lambda limit, so the Phase 0 stack cannot ship as a zip. Two options:

- **A (preferred): export ECAPA to ONNX** (~80 MB model) + onnxruntime — the exact packaging
  `lambda_vad` already uses for Silero (`models/silero_vad.onnx` + the VAD layer). The 08-08
  plan §0.2 warned "SpeechBrain ECAPA→ONNX is not turnkey", so option A is **gated on a parity
  test** (Phase 3): embeddings from the ONNX export must match recorded SpeechBrain reference
  vectors on committed fixtures (cosine ≥ 0.999 each). If parity fails, every Phase 0 number is
  void for the deployed model and option A is dead.
- **B (fallback): container-image Lambda** with torch + speechbrain baked in (~2 GB image).
  Works with zero model-conversion risk, but: cold start ~8–20 s (vs ~1–3 s for A), a new
  deploy mechanism (ECR + image build in both workflows), and deploy-role IAM additions
  (`github-actions-fieldsight-deploy` needs ECR push — check with `simulate-principal-policy`,
  and remember a missing deploy-role permission fails stack CREATE and blocks the pipeline).

**Cost/concurrency statement (required):** account concurrency ceiling is 1000 (raised
2026-08-04 from 10). `SpeakerEmbedFunction` gets **ReservedConcurrentExecutions: 5** so a burst
of finalizes can never starve org-api (which holds its own reserved 200 on prod). Per-turn
embedding on CPU at 1769 MB is ~100–400 ms (option A) — a 30-turn session is well under a 120 s
timeout. Cold start: option A ~1–3 s (zip + layer), option B ~8–20 s (image pull + torch
import); both acceptable because the invoke is async and never blocks the finalize email
(Phase 5's re-run design).

## What is deliberately out of scope

- Re-opening provider selection, re-transcribing, or fixing within-turn merges (a turn
  containing two people stays one turn; the system declines to name it — v2 §11).
- Backfilling names onto already-extracted sessions (Phase 6 defines the forward-only rule and
  what withdrawal does to history; a matching backfill is a separate decision).
- Shipping any absolute similarity threshold or margin value as "calibrated". **No shippable
  threshold exists.** Every number so far (97% relative, +0.262 cut, the +0.33…+0.44 margins)
  was fitted on the data that produced it, on read speech, quiet room, 5 m, same-day
  channel-matched enrolments, n=3–4 turns per distant speaker. What fixes this, exactly:
  1. **Held-out scripted material**: ≥3 people, two of them at 5–6 m, recorded on a
     **different day in a different room** with enrolment reads recorded **at least a day
     earlier** (kills the same-day channel match), including one overlapping-speech segment
     and one outdoor/noise segment (the Block V protocol in
     `fieldsight-vad-check/2026-08-11-blockV-script/SCRIPT.txt` is the template — it was
     written for exactly this and its 6 m + overlap segments were the parts not yet run).
  2. **Shadow-mode scores from real prod sessions** (Phase 5's `shadow` mode) joined to human
     corrections (Phase 4) as ground truth — v2 §9's measurement, per condition class.
  Until (1) or (2) exists, `SpeakerIdentityMode=on` is prohibited by this plan.

---

## Phase 1 — the shared arithmetic, in `src/` (inert everywhere)

Move the decision arithmetic out of `scripts/` (scripts never ship in the package — they are
even in the prod workflow's `paths-ignore`) into a module every Lambda can import.

**Files**
- `tests/unit/test_voiceprint_utils.py` (FIRST)
- `src/voiceprint_utils.py`

**Tests first — assert:**
- `cosine(a, b)`: loudness-invariant, zero-vector → 0.0 (port from `scripts/speaker_phase0.py`).
- `decide_name(scores, duration_s, min_turn_s, min_margin)` returns a three-state result
  (`confirmed | tentative | unknown` shaped as v2 §1):
  - duration below the floor → `unknown`, **whatever the scores say** (the findings' rule 1;
    default floor 3.0 s — the one miss was a 2.1 s turn);
  - nearest profile wins only with `best − second_best ≥ min_margin` (the findings' rule 2:
    relative matching with a required margin, never an absolute cut);
  - margin not met → `tentative` with the nearest name attached (viewer-only, §1);
  - empty profile set → `unknown`, not an exception.
- `window_is_homogeneous(frame_embeddings, …)` — the §6 contamination guard: short-frame
  embeddings of the marked window must form one cluster (max pairwise cosine distance under a
  bound) before any of it may be enrolled. Test with two synthetic clusters → False, one → True.
- All of it pure numpy — **no torch, no speechbrain, no onnxruntime imports at module level**
  (the same lazy-import discipline `scripts/speaker_phase0.py` already uses, for the same
  reason: unit tests must not need an 80 MB download).

**Implementation:** port `cosine`, `separability`, and the frame logic from
`scripts/speaker_phase0.py`; add `decide_name` and `window_is_homogeneous`. Leave the scripts
importing from `src/voiceprint_utils.py` so the reproducer and prod share one implementation.

**Inert:** nothing imports it in a deployed path yet. **Rollback:** delete the module.

---

## Phase 2 — migration 0038 + repository (inert on prod/test)

**Files**
- `tests/unit/test_migration_0038_voiceprints.py` (FIRST — mirror `test_migration_0030_devices.py`)
- `tests/unit/test_voiceprints_repo.py` (FIRST — FakeConn/FakeCursor, mirror `test_action_items_repo.py`)
- `src/migrations/0038_speaker_voiceprints.sql`
- `src/repositories/voiceprints.py`

**Migration** — v2 §8 verbatim (both tables), plus what §6 forces and §8 left implicit:
- `speaker_voiceprints`: as specified (`status` tentative|confirmed|**withdrawn**, `consent_at`,
  nullable `user_id`, `company_id NOT NULL` cascade).
- `speaker_voiceprint_samples`: as specified (`vector(192)`, `source`, `s3_key`,
  `window_start_s/end_s`) **plus** `created_by uuid` and `correction_ref text` — §6 requires
  every stored vector to keep a pointer to the correction that produced it so a bad enrolment
  can be withdrawn "along with everything it justified".
- A third table the audit chain needs (see Phase 5/6): `speaker_turn_names` — one row per
  named turn: `company_id, session_base, turn_ref (source_filename + start_sec), voiceprint_id,
  state (confirmed|tentative), score, margin, created_at`. This is the provenance that makes
  withdrawal enumerable; without it, un-naming after a withdrawal means grepping S3 artifacts.
- Everything `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` (repo idempotency
  convention; migrations re-run on every deploy).

**Migration-text tests assert:** idempotency; `vector(192)`; `company_id … NOT NULL`;
`consent_at` exists; the three status values appear; samples carry `s3_key` and the correction
pointer; `speaker_turn_names` carries `voiceprint_id`.

**Repository tests (FakeConn — no pgvector extension; embeddings cross the boundary as the
pgvector text literal `'[0.1,0.2,…]'` built by the repo, so the fake only ever sees a string):**
- `add_sample(conn, company_id, voiceprint_id, embedding, source, s3_key, window, created_by)`
  — serializes the vector, refuses an embedding whose length ≠ 192.
- `profiles_for_matching(conn, company_id)` — **the load-bearing query**: returns only rows
  with `consent_at IS NOT NULL` and `status != 'withdrawn'`; carries `status` so the caller
  can cap tentative profiles at tentative output. Assert the SQL contains `company_id =` —
  **no company_id-less query may exist on these tables** (v2 §8), and assert a `None`/absent
  company id raises rather than returning everything (the `[]`-means-no-filter trap: use
  `None` only for "no filter" and never accept it here).
- `withdraw(conn, company_id, voiceprint_id)` — sets status, **deletes the sample embeddings**
  (biometric data: the vector itself goes, the audit row's pointers stay), returns the sample
  ids so the caller can un-name (Phase 6).
- `confirmations_count(conn, …)` distinct-sessions logic for §6's "N independent confirmations".

**Inert:** tables exist, nothing reads them. **Rollback:** migrations are forward-only —
revert is a new migration `0039` dropping the three tables (record this now so it is not
invented under pressure).

---

## Phase 3 — `SpeakerEmbedFunction` (inert: deployed but nothing invokes it)

**Files**
- `tests/unit/test_voiceprint_onnx_parity.py` (FIRST — gates D2 option A)
- `tests/unit/test_lambda_speaker_embed.py` (FIRST)
- `tests/unit/test_template_workflow_parameter_wiring.py` (extend)
- `src/lambda_speaker_embed.py`
- `models/ecapa_tdnn.onnx` (exported artifact) + `tests/fixtures/voiceprint_parity/` (2–3 short
  wavs + their SpeechBrain reference embeddings as `.npy`, recorded once by
  `scripts/export_ecapa_onnx.py`)
- `scripts/export_ecapa_onnx.py` (the export + reference-vector recorder; runs on the dev box
  with `uv run --with torch --with speechbrain …`, never in CI)
- `src/template.yaml` (new function), `.github/workflows/deploy.yml` + `deploy-prod.yml`

**Parity test (the D2 gate):** for each fixture wav, onnxruntime embedding vs the committed
SpeechBrain reference: cosine ≥ 0.999. Skipped (`pytest.mark.skipif`) when
`models/ecapa_tdnn.onnx` is absent so CI stays green before the export lands — but Phase 5
may not start until it runs and passes. **If the export cannot reach parity, switch to D2
option B and re-record nothing** (the reference vectors stay the ground truth either way).

**Handler tests (embedder monkeypatched to a stub returning fixed vectors; FakeConn):**
- event `{"op": "enrol", …}`: fetches the named raw-audio key (S3 client monkeypatched),
  cuts the window, runs `window_is_homogeneous` — an inhomogeneous window is **rejected with a
  distinct error string**, nothing stored; a homogeneous one stores a sample row via the repo.
- event `{"op": "match", "session": …, "turns": [{source_filename, start_sec, end_sec}, …]}`:
  embeds each turn ≥ the duration floor, scores against `profiles_for_matching`, writes
  `speaker_turn_names` rows via the repo with `decide_name`'s three-state result, and returns
  the mapping. Turns under the floor are returned as `unknown` and **not embedded at all**.
- audio is always read from `users/{folder}/audio/…_c####.wav` (raw), never
  `audio_segments/` — assert the key shape (v2 §3 is a hard rule; the Phase 0 numbers are
  raw-audio numbers and do not transfer to compressed copies).
- 16 kHz is asserted, mismatch raises (the model silently degrades otherwise).
- an S3 `AccessDenied`/`ClientError` **raises** — never `except: pass` into an empty result
  (the standing `except ClientError` trap turned a missing IAM prefix into 200-empty before;
  and remember the 403-vs-404 shape: without ListBucket a missing key reads as 403).

**Template:** in-VPC shape mirroring `SuggestionWriterFunction` (PsycopgLayer, VpcConfig, PG
env, `Policies: VPCAccessPolicy` + S3 read on the raw-audio prefix), `MemorySize: 1769`,
`Timeout: 120`, `ReservedConcurrentExecutions: 5`, **no Events**. New layer or bundled
onnxruntime per D2-A (follow the VAD-layer precedent; the layer is built outside the stack and
passed as an ARN parameter — and do not conflate `sitesync-vad-layer` with the cp312-only
`fieldsight-vad-layer`, per `template.yaml:355–359`'s warning).

**IAM — verify, don't read:** `simulate-principal-policy` for (a) the new function's runtime
role against `s3:GetObject` on `users/*/audio/*` **and** `s3:ListBucket`, (b)
`github-actions-fieldsight-deploy` against every new resource type this adds. Both have failed
silently or fatally before (BUG-43 lesson 3; the LogGroup CREATE_FAILED rollback).

**Inert:** no trigger, no caller. **Rollback:** remove the function from the template (or leave
it — with no caller it is cost-free at rest).

---

## Phase 4 — enrolment by correction, with consent (changes live behaviour: new org-api surface)

v2 §6: a permitted user marks a transcript passage as a person; the system embeds the raw
audio window and adds it to that person's profile. Ordered before proactive enrolment (§7)
deliberately — real site acoustics, zero collection cost.

**Files**
- `tests/unit/test_org_api_voiceprints.py` (FIRST — `org.lambda_handler(make_event(...))`
  pattern with repo funcs monkeypatched, mirror `test_org_api_sessions.py`)
- `src/lambda_org_api.py` (or its route module, following how sessions routes are registered)
- `src/repositories/voiceprints.py` (extend)

**Consent is a precondition, not a checkbox.** A voiceprint is biometric information under the
NZ Privacy Act; consent must come from **the person whose voice it is** — not the wearer, not
the employer (v2 §10). Concretely:
- `POST /api/org/voiceprints` creates a profile only with an explicit consent payload
  (`consent_given: true` + the consenting identity); the row's `consent_at` is set server-side.
  A profile row without `consent_at` can be created for an **unnamed** recurring voice
  (`user_id NULL`, no biometric-to-identity link yet), but the moment a name/user is attached,
  consent is required or the request 4xxs.
- `POST /api/org/voiceprints/{id}/corrections` body: `{session_base, source_filename,
  start_sec, end_sec, display_name}` → invokes `SpeakerEmbedFunction` (`op: enrol`,
  **direct Lambda invoke**, the Matcher→SuggestionWriter precedent). Async; the response is
  202 with a correction id.
- Profile state machine (§6): new profiles are `tentative`; `confirmed` only after **N
  independent confirmations from different sessions** (`VOICEPRINT_CONFIRMATIONS`, default 3,
  code default only — it gates trust, not behaviour that needs a deploy-time flip).
- Every consumer stays company-scoped; **`platform_admin` span-all must be taught to the new
  write endpoints explicitly** (standing trap — each write endpoint separately).

**Tests first — assert:** consent enforcement (named profile without consent → 4xx, and the
error names the missing thing); company scoping (a user from company A cannot correct company
B's session); role gate; the invoke payload shape to SpeakerEmbedFunction; platform_admin
span-all on each new endpoint; N-confirmations transition (repo-level).

**Live-behaviour marker:** new endpoints exist, but nothing downstream consumes profiles yet —
extraction and reports are byte-identical. **Rollback:** the routes 404 when
`SPEAKER_IDENTITY_MODE=off` (read the Phase 5 switch even here, so one variable turns the
whole feature off end-to-end); flipping the repo variable and redeploying is the rollback.

---

## Phase 5 — matching at finalize: shadow first, three-segment switch (changes live behaviour, gated)

**The switch, wired end-to-end or it does not exist** (`FILTER_AUDIO_EVENT_TAGS` shipped with
a documented rollback that was not real; `TRANSCRIBE_WHOLE_CHUNK` had no Parameter at all):

1. repo variables `SPEAKER_IDENTITY_MODE` (prod + test),
2. both workflows' `--parameter-overrides`: `"SpeakerIdentityMode=${{ vars.SPEAKER_IDENTITY_MODE || 'off' }}"`,
3. `template.yaml` Parameter `SpeakerIdentityMode` — `Type: String`,
   `AllowedValues: [off, shadow, on]`, `Default: 'off'` — passed as env
   `SPEAKER_IDENTITY_MODE` to **every function that reads it**: ExtractSessionFunction,
   ItemWriterFunction, SpeakerEmbedFunction, OrgApiFunction.

**Wiring tests FIRST** in `tests/unit/test_template_workflow_parameter_wiring.py` (it is not a
boolean, so the sweep cannot see it — mirror the `UploadVerifyMode` test):
`test_the_speaker_identity_mode_is_wired_in_both_environments`, plus a
`_MODE_CONSUMERS`-style check that each reading function is given `!Ref SpeakerIdentityMode`
(the "middle segment" test, mirroring `test_every_function_that_reads_an_evidence_tunable_is_given_it`).
Also Parameters `SpeakerMinTurnSec` (Default `'3.0'`) and `SpeakerMarginMin` (Default `'999'`
— **a deliberately unreachable sentinel**: until calibration writes a real number, even
`on` mode can produce only tentative/unknown, so an accidental `on` cannot name anyone).
Pin both with the code-default-equals-template-default test pattern.

**Behaviour by mode**
- `off` (ships as default): nothing invokes SpeakerEmbedFunction. Prod/test byte-identical to
  today.
- `shadow`: after the finalize extraction succeeds, `lambda_extract_session` fires an **async**
  (`InvocationType='Event'`) `op: match` invoke with the assembled turns (the production turn
  units — `assemble_session_turns` output, post `_dedup_turn_boundaries` + announcement/tag
  filters; **not** the eval script's ad-hoc units, which is one of the review corrections).
  Scores land in `speaker_turn_names` with state capped at `tentative`. **No user-visible
  surface changes.** This is the calibration collector: shadow rows joined to Phase 4
  corrections are the held-out measurement v2 §9 requires.
- `on` (**prohibited until a calibrated `SpeakerMarginMin` exists** — see "out of scope"):
  same async invoke; when the match writes `confirmed` names it requests **one bounded
  identity re-run** of the extraction, which is where names may enter the artifact:
  - a **separate budget**: `SPEAKER_RERUN_MAX` (default 1), never
    `FINAL_RERUN_MAX_GENERATIONS` (`lambda_extract_session.py:485` — shared with growth
    re-runs and silently consumable);
  - a distinct re-run reason in the request artifact so logs can tell them apart;
  - **explicit email suppression**: `lambda_item_writer.py:315` writes
    `session_finalize_requests/{sid}-updated.json` on update — the identity re-run's write
    path must set a `suppress_email` marker the finalize consumer honours, and a unit test
    pins that an identity-re-run update **does not** produce the finalize request object.
  - v2 §1 enforcement at the boundary: only `confirmed` names reach the extraction prompt /
    minutes / email; `tentative` degrades to the anonymous label everywhere except the
    transcript-viewer artifact, where it renders as `(可能是 …)`. Add the Phase-A-style test:
    **fail if a raw `spk_N` string or a tentative name reaches a user-visible surface.**
  - `lambda_item_writer.py:624`: when a confirmed wearer identity exists for the session, the
    self-referential responsible party resolves from it; the `speaker_count == 1` gate remains
    the fallback (both computation sites `:1272`/`:1523` unchanged — the count keeps meaning
    "label-string union" and nothing new may read it as a headcount).

**Tests FIRST for this phase** (beyond wiring): mode dispatch in extract-session (off → no
invoke; shadow → invoke, no re-run, no artifact change; on → re-run request with its own
budget + suppression); the §1 boundary test; the item-writer gate test (confirmed wearer
beats the count gate; absent identity falls back; **absent is not 1**).

**Live-behaviour marker:** merging this changes prod behaviour **only if**
`SPEAKER_IDENTITY_MODE` is set to non-`off`; the template default is `off`, so the merge
itself is inert. **Rollback:** set the repo variable to `off`, redeploy — and the wiring tests
are what make that sentence true. Verify after any flip by reading the deployed function's
env (`aws lambda get-function-configuration`), not the PR description — the unwired-toggle
lesson.

---

## Phase 6 — withdrawal, and what happens to existing sessions

**Withdrawal (v2 §6/§10 — this is the part the Privacy Act makes non-optional):**
- `POST /api/org/voiceprints/{id}/withdraw` (Phase 4's route module; platform_admin span-all
  again): repo `withdraw()` sets `status='withdrawn'` and **deletes the embedding vectors**
  (the biometric data itself goes; the sample rows' audit pointers — s3_key, window,
  correction ref — remain as the record of what existed and was removed).
- Un-naming: `speaker_turn_names` is the enumeration — every row with the withdrawn
  `voiceprint_id` flips to `state='withdrawn'`, and the transcript-viewer read path treats it
  as `unknown`. For extractions where a confirmed name entered the artifact (only possible in
  `on` mode), each affected session gets one identity re-run **with the name removed**, same
  suppressed-email path as Phase 5. Emails already sent cannot be recalled; that asymmetry is
  why §1 keeps tentative names out of email in the first place — say so in the endpoint's
  response so the caller is not promised the impossible.
- Tests FIRST: withdraw deletes vectors (FakeCursor sees the DELETE), turn-name rows flip,
  the re-run request carries the suppression marker, and a withdrawn profile never appears in
  `profiles_for_matching` again (already pinned in Phase 2, re-asserted through the handler).

**Existing sessions (decide it, don't leave it implicit — 08-08 plan Phase D-3):**
**Forward-only.** Sessions extracted before a profile existed keep their anonymous labels.
Retro-*enrolment* works naturally (audio is retained indefinitely — v2 §2 — so a Phase 4
correction can point at any past recording), but retro-*matching* (backfilling names onto old
artifacts) is explicitly not built: it would re-run extractions at unbounded cost and re-open
every already-delivered report. If it is ever wanted, it is its own plan with its own budget.

---

## Execution order and gates

| phase | ships | inert? | hard gate before starting the next |
|---|---|---|---|
| 1 | `voiceprint_utils` | yes | tests green |
| 2 | migration 0038 + repo | yes (tables unused) | migration applies on test deploy |
| 3 | SpeakerEmbedFunction | yes (no caller) | **ONNX parity test passes** (else switch to container image and re-verify) |
| 4 | enrolment endpoints | new surface, gated by mode=off | consent flow exercised on our own site (v2 §10's sequencing) |
| 5 | shadow matching + switch | default off | shadow data accumulating on test |
| 6 | withdrawal + policy | endpoint only | — |
| — | `SpeakerIdentityMode=on` | **not in this plan** | calibrated `SpeakerMarginMin` from held-out material (see "out of scope") + a written decision |

Every phase lands as its own PR into `develop` (test stack) first; prod promotion is the
normal approval-gated `main` push. Nothing in Phases 1–3 can change any behaviour even if
merged straight to prod; Phases 4–6 are behind the one switch.
