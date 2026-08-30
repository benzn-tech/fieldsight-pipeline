# Plan: naming a speaker builds a company asset

**Date:** 2026-08-30.
**Goal:** when a user renames `Speaker 2` to `Andy M`, that single act should (1) cut and store
audio of the right length as a voiceprint sample, (2) capture who Andy works for, and (3) get
easier every time it happens — eventually pre-filled from the site's sign-in register.

**This needs three sessions.** The ownership split is at the bottom, and the contracts are
written out rather than described because this repository's most expensive recurring defect is
two halves agreeing on a contract neither of them states.

---

## What already exists — verified against code and prod data, 2026-08-30

Do not re-plan these. Three of the four things this feature needs are built.

| piece | state |
|---|---|
| `POST /api/org/sessions/{id}/speaker-corrections` | live; takes `display_name`, `source_filename`, `start_sec`, `end_sec` |
| **cutting the right audio length** | live. `judged_window` re-tests the tightest contiguous 10 s inside a window too wide to accept whole (PRs #601–#603) |
| **collecting the speaker's other turns** | live. `_admit_harvest` stores each cluster member as its own sample, capped at 6 samples / 60 s |
| `speaker_voiceprints.external_ref` / `external_source` | **already in the schema** (migration 0049), designed for exactly the Sign On Site key, `UNIQUE (company_id, external_source, external_ref)` |
| `companies.voiceprint_consent_basis` | live — `notice \| attestation \| confirmed \| NULL` |
| the employer of a named person | **does not exist anywhere** |

**First real sample landed 2026-08-28**: window 43.16–53.16 s, spread 0.231 against the
unchanged 0.35 limit. The mechanism works end to end; what is missing is the *employer*, the
*suggestion*, and the UI that collects them.

---

## The distinction that must not be got wrong

`users.company_id` and `speaker_voiceprints.company_id` are the **tenant** — which customer's
data this is. `ABC Ltd` in *"Andy M is from ABC"* is Andy's **employer**, a subcontractor.

**These are different things and the schema has only the first.** Putting an employer into
`companies` would make every subcontractor a tenant — with sites, memberships, a consent basis
and an ACL — and would silently widen who can see what. It is a one-line mistake that is very
hard to walk back once rows exist.

So: employer is a new, nullable field, and it is **not** a foreign key to `companies`.

---

## Where the suggestion comes from — and the answer is "nowhere, yet"

The first version of this plan claimed `findings.entity_name` / `entity_trade` could answer
*"is Andy M from ABC?"* today, with no integration. **That was wrong, and it was wrong in the
way this plan warns about two sections later.**

`entity_name` is the entity a FINDING is about, and prod shows what that actually means:

```
Jerry / PK Building | Client            John        | IT / Management
Troy and Jay        | -                 Neil, James | Site Personnel
facade subbie       | facade            Zoe         | Rebar
```

People, groups of people, roles, one unparsed person-slash-company. **"Zoe | Rebar" means Zoe's
TRADE is rebar — not that she works for a company called Rebar.** Matching a typed name against
`entity_name` returns the person's trade, not their employer; listing entities seen on the site
returns co-occurrence. Either one, put behind *"Please confirm Andy M is from ABC?"*, is a guess
that the click then launders into a record stamped `employer_source: "suggested"`.

So the honest position: **there is no reliable person → employer source in the data today.**
The confirm-prompt UX waits for Sign On Site (Task 6), which is a register rather than an
inference. What ships before then is smaller and actually true:

- **ask once, remember forever** — if this company already has a profile named "Andy M" with an
  employer, offer that. Not a guess: it is an answer somebody already gave.
- **show the trade as context, never as the answer** — "Zoe — Rebar" beside the field helps a
  human type the right thing. It is not pre-filled and it is not a suggestion.

*(If `findings` is ever queried for this, the tenant path is `findings.site_id → sites.company_id`
— ONE hop, NOT NULL on both sides. `findings` has no user column at all, so the "reach the tenant
through `users`" rule that `topics` needs does not apply here; the first version of this plan
cited that rule and applied it to the wrong table, which is PR #592's lesson relocating rather
than being learned.)*

## Task 1 — the employer field  *(backend/pipeline — mine)*

Migration: number chosen at merge time (`0041_*` and `0044_*` already collide once each).

```
ALTER TABLE speaker_voiceprints
  ADD COLUMN employer_name   text,
  ADD COLUMN employer_source text
      CHECK (employer_source IN ('typed','suggested','sign_on_site')),
  ADD COLUMN employer_set_by uuid REFERENCES users(id),
  ADD COLUMN employer_set_at timestamptz;
```

**`employer_source` is the load-bearing column, not decoration.** A name typed by a person who
knows Andy and a name accepted with one click from a ranked guess are different evidence, and
they will need separating later — when a subcontractor changes, when a customer asks where a
claim came from, or when a suggestion source turns out to have been wrong for a month. Storing
only the string makes that question permanently unanswerable, which is the same mistake
`consent_given` made before `consent_basis` was added beside it.

**Nullable, and staying nullable.** The user was explicit: no register, no employer, and the
correction must still work. A required field here would block the correction that builds the
voiceprint — trading the thing that works for the thing that is nice to have.

**The pairing is a CHECK, not just endpoint validation:**

```sql
CHECK ((employer_name IS NULL) = (employer_source IS NULL))
```

`speaker_voiceprints` has three writers already (org-api's upsert, the voiceprint writer, and
the Sign On Site adapter to come). A rule enforced in one caller is a rule **until somebody
adds a second caller**, and this file's own history is a list of exactly that.

**An update never NULLs an employer it did not set.** A later correction on the same person
carrying no employer must leave the stored one alone — `SET employer_name = %s` with a NULL
parameter is the obvious default and it silently erases the answer somebody gave last week. A
*different* non-NULL employer overwrites and bumps `employer_set_by`/`employer_set_at`: people
change subcontractors, and the columns are there to record that it happened.

**Asserts:** an employer with no source is refused (endpoint AND constraint); a correction with
neither leaves an existing employer intact; a second correction with a different employer
overwrites and records who and when.

## Task 2 — the correction endpoint accepts and records it  *(mine)*

`POST /api/org/sessions/{id}/speaker-corrections` gains three optional body fields:

```jsonc
{
  "display_name":    "Andy M",
  "employer_name":   "ABC Ltd",      // optional
  "employer_source": "suggested",    // required IF employer_name is present
  "employer_ref":    null            // reserved for sign_on_site; ignored for now
}
```

Response gains `"employer": {"name": ..., "source": ...}` or `null`, read back from the stored
row rather than echoed from the request — *"we did it"* and *"the row was not there"* must not
look the same to the caller.

**Same tenancy rule as everything else on this endpoint:** the company comes from the caller,
never the body, and `_same_company_as_folder` still applies (PR #593).

**Where the employer lands when there is no profile row — and today, often there is not.**
`upsert_profile` runs only inside the consent branch: `consent_given`, or `ENROL_ON_CORRECTION`
with a company basis settled. A company that has settled nothing, correcting without
`consent_given`, creates **no `speaker_voiceprints` row at all** — so an `employer_name` sent
with that request has nowhere to go.

Silently dropping it is the worst of the three options, because the caller is told 202 and the
read-back says `"employer": null` for a request that carried one. The rule:

```jsonc
"employer": { "stored": false, "reason": "no voiceprint profile: this company has not
              settled a consent basis and the request carried no consent_given" }
```

The correction still succeeds — naming a speaker must not depend on the employer field — but
the response says plainly that the employer went nowhere. *"We did it"* and *"there was nothing
to write it to"* are different answers and this endpoint already refuses to conflate them
elsewhere.

**`employer_ref` is REFUSED, not ignored, until Task 6.** A field one side sends and the other
drops is this repository's most-documented failure shape. Non-null → 400 with the reason.

## Task 3 — "what do we already know about this name"  *(mine)*

```
GET /api/org/speakers/known?name=Andy%20M&site=<uuid>&date=YYYY-MM-DD
→ 200 { "known": {"employer": "ABC Ltd", "source": "typed", "profileId": "...", "samples": 3},
        "trade": "Rebar" }        // context for the human; NEVER pre-filled
→ 200 { "known": null, "trade": null }
```

**Not a suggestion endpoint.** It answers one question — *has somebody in this company already
told us who Andy M works for* — and returns `null` rather than a guess when nobody has.

- `known` comes from `speaker_voiceprints` in the caller's company with a matching
  `display_name` and a non-NULL `employer_name`. That is a record, not an inference.
- `trade` comes from `findings.entity_trade` for that name, reached
  `findings.site_id → sites.company_id`. **One hop, NOT NULL on both sides** — `findings` has no
  user column, so the `topics`-style join through `users` does not apply and would also drop
  every NULL-author row silently.
- `404` when `SPEAKER_IDENTITY_MODE == "off"`, like every sibling on this route.

**Trade is displayed, never submitted.** It is what the person does, not who employs them, and
the whole reason this endpoint is smaller than the first draft is that conflating those two put
a guess behind a confirm button.

Sign On Site becomes a second key in this response (`"register": {...}`) when Task 6 lands —
same endpoint, one more source, and *that* one may be pre-filled because a register is a record.
## Task 4 — the rename dialog  *(frontend session)*

The dialog collects a name today. It gains an **optional** employer field:

1. on open, call `GET /api/org/speakers/known` with the typed name (debounced)
2. if `known` returns:
   > **Andy M — ABC Ltd** *(recorded earlier)*  [ Use this ]  [ Different company ]
   - **Use this** → `employer_name: "ABC Ltd"`, `employer_source: "typed"` — it is still the
     value a human typed, just not today
   - **Different company** → free text → `employer_source: "typed"`
3. if `known` is null: a free-text field, empty is fine, nothing pre-filled
4. if `trade` is present, show it as grey helper text — *"heard on site as: Rebar"* — beside the
   field and **never inside it**

**The confirm prompt — "Please confirm Andy M is from ABC?" — is deliberately NOT in this
task.** It needs a source that can be wrong in a way a human would catch, and the only one that
qualifies is the sign-in register. Building it against anything available today would put a
one-click **Yes** in front of a co-occurrence guess. It arrives with Task 6, and
`employer_source: "sign_on_site"` exists so those rows stay separable from the typed ones.

Also for this session: `participantHint` still needs removing (spec already delivered) — it is a
positional zip of two independently ordered lists and it is why a name appears on the wrong
speaker today.
## Task 5 — surface the asset  *(frontend session, after Task 3)*

A company's voiceprint library is currently invisible. `GET /api/org/voiceprints` exists and
returns the profiles; the list should show name, employer, sample count, status
(`tentative`/`confirmed`), and a withdraw control (`DELETE /api/org/voiceprints/{id}` is live).

**Withdraw must be as reachable as enrol.** It is the half that makes holding biometric data
defensible, and it currently has no UI at all.

## Task 6 — Sign On Site  *(mine, blocked)*

**Blocked on one input: API docs or a sample response.** Nothing else is missing.

The adapter fills `external_ref` / `external_source` (already in the schema) and becomes the
top-ranked source in Task 3. It also unlocks something the matcher has never had: *who was on
this site that day* — `profiles_for_matching`'s site narrowing has been a measured no-op twice
for want of a candidate pool.

Written behind an interface so a second register (Sign On Site's competitors) is a new adapter
and not a second feature.

## Task 7 — anonymous speaker re-bind  *(mine — worth building, NOT a prerequisite)*

Measured (`docs/superpowers/specs/2026-08-29-asr-accuracy-measured-findings.md`): batched
speaker labels are **55.4 % pure across a session** — `spk_0` means a different person in 6 of
14 ASR calls. A local ECAPA re-bind of the per-call namespaces at finalize takes that to
**96.3 %** for zero vendor cost and ~55 s CPU, and needs no consent because nothing is stored
and nobody is identified.

**The first version of this plan claimed Task 4 was actively wrong without it. That was
false**, and the correction matters because it was about to impose an ordering on another
session:

- propagation does **not** trust the ASR label. `_propagate` embeds the session's turns and
  clusters them by voice (`cluster_turns`, tau 0.85, complete linkage) — a rename reaches
  people, not labels.
- label inheritance **is** scoped per `(source_filename, label)` deliberately, in the writer,
  precisely so a cross-call `spk_0` collision cannot merge two people.
- harvested samples come from that voice cluster **and** are re-checked by `judged_window`, so
  "contaminated at the source" is not a failure this code has.

What the 55.4 % actually damages is everything that **displays** `speaker_label` — the
transcript viewer, the report, the picker. That is worth fixing on its own terms, and it is
what makes a session look coherent to a customer. It is not a gate on Tasks 1–5.

The prototype is `tools/asr-eval/rebind.py`: centroid per `(call, label)` from turns ≥ 3 s,
average-linkage at 0.45. Production host is `SpeakerEmbedFunction` — non-VPC, python3.12,
1769 MB, 600 s timeout, and the only lambda with onnxruntime and the ECAPA model
(`models/ecapa_tdnn.onnx`, present in both buckets).
## Ownership

| | tasks | blocked by |
|---|---|---|
| **This session (backend/pipeline)** | 1, 2, 3, 6, 7 | Task 6 on Sign On Site docs |
| **Frontend session** | 4, 5, and `participantHint` removal | Tasks 2 and 3 landing first |
| | *(the confirm-prompt UX is Task 6's, not Task 4's)* | |
| **Other backend sessions** | nothing here — but Tasks 1–3 touch `lambda_org_api.py`, which several branches edit | coordinate at merge |

**Suggested order:** 1 → 2 → 3 → (4, 5 in parallel with 7) → 6 when the docs arrive.

Task 7 moved out of first place when its rationale turned out to be false. It is still worth
building — it is what makes a transcript look coherent — but it gates nothing here, and an
invented dependency would have held another session up for no reason.

---

## The contracts, in one place

So the frontend session can build against them before the backend lands, and so a mismatch is
a red test rather than an empty list in production.

```jsonc
// POST /api/org/sessions/{sessionBase}/speaker-corrections
{ "user": "Ben_UCPK2", "display_name": "Andy M",
  "source_filename": "...json", "start_sec": 3.16, "end_sec": 112.66,
  "employer_name": "ABC Ltd", "employer_source": "suggested" }
// → 202 { "requestId": "...", "propagation": "queued",
//         "enrolment": "requested", "employer": {"name": "ABC Ltd", "source": "suggested"} }

// GET /api/org/speakers/known?name=Andy+M&site=<uuid>&date=2026-08-30
// → 200 { "known": {"employer": "ABC Ltd", "source": "typed",
//                   "profileId": "...", "samples": 3},
//         "trade": "Rebar" }          // display only; never submitted
// → 200 { "known": null, "trade": null }
```

`employer_source` values are exactly `typed | suggested | sign_on_site`. **A seam test pins
this list on both sides**, in the shape of `test_embedder_writer_contract.py` — because the
last time two halves disagreed about an enum, one sent `notice` while the other understood only
`attestation`, and the resulting 400 read as a configuration problem for two days.

---

## What I am not proposing

- **No `employers` table yet.** One nullable text column until there is enough data to know
  whether "ABC Ltd", "ABC" and "A.B.C. Limited" need reconciling. Normalising too early builds
  a second identity system beside `name_aliases`, which already does this job for display names
  and which the entities spec is about to feed.
- **No cross-company person identity.** Andy at customer A and Andy at customer B stay two
  profiles. That rule is asserted in `test_no_cross_company_voice_identity.py` and it is the
  one the product owner stated in the strongest terms.
- **No automatic acceptance of a suggestion.** Confidence high enough to skip the question is
  a threshold nobody has measured, and the click is what turns a guess into a record.
