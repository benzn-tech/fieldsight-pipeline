# The Ask agent's spoken answers are recorded, transcribed, and fed back to itself

**Repo:** `fieldsight-pipeline`.
**Found:** 2026-08-12, from a real prod recording.
**v2 — the first design was reworked after review.** What survived: the problem, and the two
measured constraints. What did not: the writer, the key, the matcher, the label, and the list of
places that need changing. The rejected v1 is at the end, because the reasons it failed are the
constraints v2 has to satisfy.

## The measurement

The operator asked SP-Ask about the concrete pour during a meeting. The device plays the answer
aloud, the main recording is still running, and it picks the answer up. Diarisation separates
them cleanly:

```
[ 11.5s] speaker_0: …When will the concrete pour? Tell me when did the concrete pour?
[128.9s] speaker_1: The concrete pour is scheduled for Monday with the weather contingency to Wednesday.
[136.6s] speaker_0: Say it again. When did the concrete pour?
[145.1s] speaker_1: The concrete pour is scheduled          ← a 5-token fragment of the same answer
```

`voice_ask_log` holds the matching row:

```
[12:13:21] A: "The concrete pour is scheduled for Monday with a weather contingency to Wednesday."
```

That utterance is now a **finding on the daily timeline**, reading as a fact confirmed on site.
Nobody confirmed it — the agent read it back out of the index.

The loop closes: `extraction → report_chunks → RAG → agent answers aloud → recorded →
transcription → extraction`. One statement becomes N mutually "corroborating" findings across
days, all from one original utterance, and nothing in the report shows they are the same source.

## The five constraints any design must satisfy

Each was measured or read off the code, not assumed.

**C1 — Extraction cannot reach Aurora.** `fieldsight-prod-extract-session` is **not in VPC and
has no PG credentials**; `fieldsight-prod-ingest` has both. That is the architecture's rule
(functions that call LLMs are non-VPC, non-VPC has no database), not an oversight. **Anything
that queries `voice_ask_log` at extraction time is dead on arrival.**

**C2 — There are at least four transcript consumers, not two.** The RAG index does not pass
through the extraction assembler:

| consumer | route |
|---|---|
| extraction, rolling summary, finalize email, group merge | `assemble_session_turns` (`lambda_extract_session.py:921`) / `assemble_deduped_turns` (`:1009`) |
| RAG transcript-window chunks **and** embed-report | `lambda_ingest._load_turns` (`:440`) |
| **nightly report generator** | `lambda_report_generator.py:71` — raw `normalize_transcript`, no filters |
| **meeting minutes** | `lambda_meeting_minutes.py:285` — same |

The report generator matters more than it looks: its topics become **topic chunks** embedded
into `report_chunks` unconditionally (`chunking.py:99`, `lambda_ingest.py:585`). Filtering the
first two paths and stopping would leave the loop open through topic chunks, while an acceptance
test that only checks `transcript_window` chunks would pass. **The loop is only closed when every
prompt/index producer is covered.**

**C3 — Asymmetry between ingest and embed-report does not degrade, it breaks ingestion.**
`embed_from_sidecar` looks vectors up by `sha256(chunk_text[:8000])` and **raises `KeyError` on a
miss** (`lambda_ingest.py:218-219`). Embed-report (non-VPC) and ingest (in-VPC) each rebuild
`chunk_text` independently. If the filter fires on one side and not the other — different IAM on
a new prefix, a toggle present on one function only, or one side treating a read failure as
best-effort — **every** transcript-window hash misses and the whole report fails to ingest.
This repo has shipped both of those failure shapes before: S3 403-masquerading-as-404, and a
toggle wired on one side only.

**C4 — Labelling corrupts `speaker_count` unless explicitly excluded.**
`speaker_count = len({t['speaker'] for t in turns})` (`lambda_extract_session.py:1523`, and
`:1272` for groups). `lambda_item_writer.py:624` gates self-referential responsible-party
resolution on `speaker_count == 1`. A solo wearer plus an agent turn counts as **2** and silently
disables a gate that is documented as correct.

**C5 — Turns are ephemeral.** There is no stored turn object to label. Every consumer in C2
recomputes turns from the raw Transcribe JSON. "Mark the turn and let consumers decide" therefore
means either running the matcher in every consumer, or rewriting the transcript JSON — and
`transcripts/` `ObjectCreated` **triggers extract-session** (`template.yaml:1761-1763`), so a
rewrite re-fires the pipeline.

## Design v2

### Writer: the voice-audit function, one object per ask

**Not the ask lambda.** It has no `put_object` call anywhere (verified), and it cannot derive the
key: the voice body carries only `caller_sub`, and `caller_sub → user_folder` lives in Aurora,
which a non-VPC function cannot reach (C1).

`VoiceAuditFunction` is in-VPC, already receives `(caller_sub, answer)`, and already resolves the
user row (`lambda_voice_audit.py:27`). It writes:

```
voice_ask/{user_folder}/{date_nz}/{utc_ts}-{uuid}.json    →   {"at_utc": …, "answer": …}
```

**One object per ask, never an append.** S3 has no append; the v1 "append-only jsonl" would have
been GET-modify-PUT, losing updates between asks 8 seconds apart (the measured session) and
duplicating on the at-least-once `Event` retry.

`{date_nz}` is the **device-local** date, because that is what the transcript path uses. An ask
at 09:00 NZ is the previous UTC date; keying by `utcnow().date()` would file every morning ask
where no reader looks.

### Readers: one shared matcher, three call sites

A single module — sidecar load + match — imported by:

1. `assemble_session_turns` (covers extraction, rolling summary, finalize, group merge)
2. `lambda_ingest._load_turns` (covers ingest **and** embed-report, which calls it — they stay in
   lockstep by construction, which is what C3 requires)
3. the report generator / meeting minutes transcript loader

**The read must fail loudly and identically on every side.** Not best-effort: under C3 a silent
miss on one side is a whole-report ingest failure on the other. IAM (`ListBucket` + `GetObject`
scoped to `voice_ask/*`) must be granted to all three roles and **verified with
`simulate-principal-policy`**, not by reading the template — this repo has been burned by exactly
that.

### Matcher: containment, not similarity

The transcript is a re-transcription of TTS audio through a room mic, so it is never
character-identical — the measured case differs by one word (`with a` vs `with the`).

And a single played answer **splits across turns**: the measured tail is a 5-token fragment of a
16-token answer. A symmetric similarity ratio scores that around 0.4 and would leave half the
agent's sentence in the record — the first playback filtered, the fragment kept.

So: **is this turn's text contained in that answer**, by normalised-token coverage, with a
minimum-token floor. Reuse the evidence verifier's machinery rather than inventing a second fuzzy
matcher — `EVIDENCE_FUZZY = 0.80`, `EVIDENCE_FLOOR_TOKENS = 5` (`lambda_extract_session.py:265`)
— and its `_normalise_for_match` (`:175`), whose `[^\w\s]` is **CJK-safe**. The `[^0-9a-z]` form
is not: this repo has shipped two bugs where it erased Chinese entirely. Qwen answers Chinese
questions in Chinese; an English-only test suite proves nothing here.

Match requires **both**: containment above threshold **and** the turn's `abs_start` inside the
window. Either alone over-matches — a person may repeat what the agent said, and people talk
right after an ask.

### Window: two-sided, duration-aware, and one time base

`abs_start` is a **naive device-wall-clock NZ-local** datetime derived from the chunk filename
(`transcript_utils.py:52-55`, `:369-380`). `at_utc` is server UTC. Convert with `zoneinfo`
`Pacific/Auckland`, the pattern already used for finalize.

`at_utc` is stamped when the answer is *produced*, before the audio reaches the device, and
playback of a long answer runs tens of seconds. So the window is
`[at − ε, at + estimated_playback + slack]`, with ε covering a device clock slightly behind.

**Clock skew is the failure mode with no signal.** This codebase records a shipped instance of a
device wall clock 12 hours out (`lambda_extract_session.py:1032-1033`). A skewed device silently
matches nothing. **Therefore: every sidecar entry that matches zero turns in a session
overlapping its window must be counted and logged.** That counter is the only way the failure is
ever discovered, and it is a requirement, not a nice-to-have.

### Labelling: excluded from the count and from the prompt

Turns matched as agent-originated are marked `from_agent: True` on the in-memory turn and then:

- **excluded from `speaker_count`** (C4) — the solo-wearer gate must keep seeing 1;
- **excluded from `render_transcript`** input, so the prompt's participants rule cannot list the
  agent as a person;
- **excluded from `chunk_transcripts`** windows, so they never reach `report_chunks`.

The turn is not deleted from the audio, and the transcript JSON is not rewritten (C5). Making the
label visible in the transcript **viewer** needs a separate annotation artifact and is explicitly
out of scope here — the viewer is in-VPC org-api and a fourth call site; saying "the viewer shows
it" without building that would be fiction.

### Known residual: other devices in a grouped meeting

The room speaker plays the answer and **every** device in the group records it, but the sidecar is
keyed to the asker's `user_folder`. Other members' transcripts never look it up, so the agent's
words survive in their extractions and chunks — and the group merge unions speaker labels
(`:1272`), amplifying it.

The group path must therefore consult **all member users' sidecars**, not just the asker's.
Outside a group, a second device recording the same room is not covered at all. **State it; do
not pretend otherwise.**

## The alternative that is probably better, and is deliberately Phase 2

**The device knows exactly when it played an answer, on the same wall clock that stamps the chunk
filenames.** That single fact removes the clock-skew failure mode (§window), the identity problem
(`caller_sub → user_folder`), and the window-sizing guesswork. Shipping playback intervals in the
session manifest would make the matcher a time-range lookup instead of a fuzzy text match.

It is not done first because it needs a mobile release and a manifest field, and the backend fix
works on recordings that already exist. **But it is the better mechanism, and this document should
not be read as arguing otherwise.** It does not fix the other-devices-in-a-group case either.

## Acceptance

**Unit (pure matcher):**
- exact match inside the window → matched
- the measured one-word difference → matched
- **the measured 5-token fragment of a longer answer → matched** (containment, not similarity)
- same text far outside the window → not matched
- different text inside the window → not matched
- a human quoting the agent verbatim minutes later → not matched
- Chinese answer and Chinese turn → matched (CJK normalisation)
- no sidecar / empty sidecar → nothing matched, no exception
- duplicate sidecar entries (at-least-once `Event` delivery) → still one match, no double-count

**Invariants:**
- solo wearer + agent turn → `speaker_count == 1` (C4)
- ingest and embed-report produce **identical** `chunk_text` sets for the same day (C3) — the test
  that would have caught the hash-miss failure

**Against today's real data:**
- re-extract the 2026-08-12 Ben_UCPK2 session: the "Concrete Pour Schedule" finding no longer
  sourced from `speaker_1`
- those turns absent from `report_chunks` — **checking both `transcript_window` and `topic`
  chunks** (C2)

**Not claimed:** the agent's voice is still recorded. The audio remains a faithful record of the
room, which is correct for evidence. Only the derived layers change.

---

## Rejected v1, and why it failed

Kept because these are the constraints, not a postmortem.

- **Ask lambda writes the sidecar** — it has no `put_object` anywhere, and cannot derive
  `user_folder` from `caller_sub` without the database it cannot reach (C1).
- **Append-only JSONL** — S3 has no append.
- **Two call sites** — there are at least four (C2), and the missing ones keep the loop open
  through topic chunks.
- **Best-effort reads** — under C3 that is a whole-report ingest failure, not a quiet gap.
- **"Label and let consumers decide"** — there is no stored turn to label (C5).
- **Symmetric similarity** — leaves the fragment, i.e. half the agent's sentence, in the record.
- **Muting the recording during playback** (the mobile mirror of Phase B) — rejected on its own
  merits: the main recording is capturing the **room**, not competing for the microphone, so
  muting discards whatever else was said. That remains rejected.
