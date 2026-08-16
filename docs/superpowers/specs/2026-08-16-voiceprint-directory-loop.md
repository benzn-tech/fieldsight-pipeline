# Voiceprints that build themselves: the directory loop

**Status:** spec, revised after adversarial review.
**Date:** 2026-08-16
**Builds on:** Phase 4 (enrolment by correction), Phase 5 (matching at request), both live on TEST.

> **Revision note.** The first draft asserted a "non-negotiable" — *human corrections feed
> the library, machine matches never do* — and then designed step 3 to harvest enrolment
> samples from the correction's **cluster**. Cluster membership is machine inference:
> `_propagate` caps every propagated turn at `tentative` and says why
> (`lambda_speaker_embed.py:355-357` — *"the margin that would justify `confirmed` has never
> been measured, so an inferred name is a suggestion"*). The design broke its own rule in
> the same document. Eleven further defects were found alongside it; all are folded in
> below, and the ones that change the design are marked.

---

## What this is for

Today a voiceprint exists only if somebody performs a ceremony: point at a passage, type a
name, tick consent. One passage, one sample, one person at a time. Nothing about that scales
to a site with ten people from ten companies.

The goal is a library that fills itself from work that is already happening:

- a conversation is analysed → known voices get their names, unknown ones stay `spk_0`;
- a person names an unknown speaker **once** → that name spreads across the meeting *and*
  the system collects that speaker's clean speech from the same meeting and enrols it;
- the next meeting recognises them.

---

## Verified facts (measured, not assumed)

| claim | status |
|---|---|
| `memberships` gives a per-site roster | **verified**: TEST 12 rows / 12 people / 5 sites; PROD 16 / 16 / 9 |
| `recordings.site_id` can give a per-DAY roster | **verified, uneven**: PROD 2793/2880 = 97 %; TEST 193/358 = 54 % |
| people without logins can hold an identity | **verified**: `users.kind` defaults `login`, `cognito_sub` made nullable by 0007 for `field_only` entries |
| `recordings.site_for_day` exists and is reachable from org-api | **verified**: `repositories/recordings.py:142`; `recordings` already imported at `lambda_org_api.py:144` |
| site narrowing already works | **FALSE, twice over — see below** |

---

## The finding that reorders everything

`profiles_for_matching` has a site-scoped branch (`repositories/voiceprints.py:273-275`):

```sql
AND (p.user_id IS NULL OR EXISTS (
      SELECT 1 FROM memberships m
       WHERE m.user_id = p.user_id AND m.site_id = %s))
```

`p.user_id IS NULL` is deliberate: an unnamed recurring voice must stay in scope.

But `lambda_org_api.py:1510` creates every profile without a `user_id`. So every profile
takes the escape and `site_id` changes nothing.

**And there is a second, independent inertness** (review #10): `speaker_match` writes its
artifact at `lambda_org_api.py:1333-1338` with **no `site_id` key at all**, while the matcher
reads `req.get("site_id")` at `lambda_speaker_embed.py:571`. Fixing only the query would move
nothing.

This is the shape this codebase keeps producing: *a guard that exists while the mechanism
routes around it*. It fails silently — narrowing would ship, deploy, be marked done, and have
no effect until margins collapsed, which presents as "the model got worse".

**Consequence:** linking profiles to identities is step 1, not step 3.

---

## Step 0 — the evidence we already have and throw away

A real TEST session, with one speaker already named:

```
Ivy     18:10:02  Do you think if the lift will be working ... The lift.   (~9 s)
spk_1   18:10:11  Oh, sure.                                                (~1 s)
spk_0   18:10:11  Will they be using it?                                   (~2 s)
spk_1   18:10:12  The li- lift they're gonna use.
spk_0   18:10:13  Yes.
spk_1   18:10:13  Yeah, they're gonna use.
```

Three speakers on screen. Almost certainly two people: Ivy asks, somebody answers, Ivy
follows up. Every turn after the first is under the 3 s floor, so the voiceprint declines all
of them — correctly.

**But the transcriber already grouped them, and we discard that.** `_session_turns`
(`lambda_org_api.py`) hands the matcher three fields:

```python
out.append({"source_filename": name, "start_sec": ..., "end_sec": ...})
```

No speaker label — even though `speaker_segments` carries one (`lambda_org_api.py:5960`,
from the transcript's `speaker_label`). The transcriber said *"these short turns are the same
voice"*, and that statement never reaches the layer complaining it cannot judge short turns.

The schema anticipated this: `speaker_turn_names.label_disagreement` exists, the writer
accepts it (`lambda_voiceprint_writer.py:95`), and **nothing has ever produced a value for
it**. The column was reserved for comparing our answer against the transcriber's, and the
comparison was never written.

### Step 0a — write-time precedence, which does not exist today

**The first draft of this section called inheritance "free". It is not, and this is why.**

`record_turn_name` (`repositories/voiceprints.py`) supersedes **unconditionally**:

```sql
UPDATE speaker_turn_names SET superseded_at = now()
 WHERE company_id = %s AND session_base = %s AND turn_ref = %s
   AND superseded_at IS NULL
```

No `source` predicate, no `state` predicate. Then it inserts. So a `label_inheritance` row
written onto a turn that already holds `source='correction', state='confirmed'` **silently
kills the human's row**. `_SOURCE_RANK` cannot help: it acts at *read* time, on rows that
both survived.

Two rules in this spec — *"state never above the source it inherited from"* and *"it may
never overwrite a row a voiceprint confirmed"* — have **no mechanism behind them**. They are
assumptions written in prose, which this codebase has established will be violated.

And it is not a rare path: `speaker_match` is on demand and every new correction re-queues
the whole session's turns, so a re-run is the normal case.

**So precedence moves into the write.** `record_turn_name` gains a rule: a weaker source may
not supersede a stronger live row. It refuses and says so, rather than winning by arriving
later. This is required before *any* new source is introduced, which makes it step 0a rather
than a detail of step 0.

### Step 0b — label inheritance

Group turns by `(source_filename, speaker_label)` — and that pairing gets per-call scoping
for free, for a reason worth recording: batching **deliberately merges** speaker namespaces
across chunks (that is its one measured benefit, 33 namespaces down to 9), ElevenLabs ids are
remapped to `spk_0…N` per response in first-seen order (`elevenlabs_utils.py:56, 69-71`), and
every segment of a batch carries the batch object's name. So `source_filename` **is** the
transcription-call identity in both the batched and unbatched cases. Grouping on
`speaker_label` alone would be the bug.

If any turn in a group carries a name from a stronger source, every turn in that group
inherits it — **with no duration floor**, because the evidence is not acoustic. It is the
transcriber's own grouping.

Four things the first draft missed, each of which would make this ship broken:

**Withdrawal must reach inherited rows.** `withdraw` finds turn names by `voiceprint_id` or
by `correction_ref`. A row inherited from a *match* has neither — machine rows carry
`voiceprint_id=None` by standing rule. Withdraw the profile, the match rows vanish, and the
inherited rows keep naming the person. That is precisely the "200 returned, seven rows still
naming them" failure the withdrawal fix was written for, reintroduced through a new door.
**Inherited rows must carry the provenance of whatever they inherited from.**

**A rejection must leave a tombstone.** `unname` supersedes rows and records nothing about
*why*. A user says "that is not me", the rows go, and the next inheritance run re-derives the
same name from the same label and writes it back — with no log line anywhere. Inheritance
must not re-derive a name that was explicitly removed for that session.

**The budget split shreds label groups.** `_split_for_budget` chops on cumulative duration
with no awareness of `(source_filename, speaker_label)`, and the split's own comment states
the invariant it relies on: *"the splits are independent … no turn's answer depends on
another's"*. Inheritance violates that by construction — a group's one long turn can land in
run 1 while its short turns land in run 2, which then inherits nothing and reports no error.
Group before splitting, or inherit in a pass that runs after all runs land.

**`TOLERANCE_SEC` was justified by the floor this removes.** `turn_name_overlay.py:32-36`
reasons that "the duration floor is 3 s, so half a second cannot reach a neighbouring turn".
With a row on every turn and turns of one second, 0.5 s reaches the neighbour — and
`resolve`'s greedy pairing will hand it over whenever the exact partner has moved, which is
the normal case after re-extraction shifts offsets. The constant needs re-deriving against
sub-3 s turns, not inheriting.

**And one claim withdrawn.** The first draft said inheritance "finally populates"
`label_disagreement`. It cannot: that column means *the provider's label disagreed with the
voice grouping* (`0040_turn_name_provenance.sql:36-39`), and `_propagate` filters to ≥ 3 s
before clustering — so sub-3 s turns are in no cluster and there is nothing to disagree with.
It can be populated only for the turns that never needed inheritance.

**Label presence must be explicit.** `speaker_label` absence is coerced to the literal
`"spk_0"` in two places (`lambda_org_api.py:5960`, `transcript_utils.py:592`). A response with
no diarisation is therefore indistinguishable from one that genuinely is all `spk_0`, and
inheritance keyed on the coerced value would spread one name across a whole file. Key on an
explicit presence check.

**`unmatchedNames` needs rework.** It counts rows matching no turn, over a response already
windowed to one topic's time range. Rows are sparse today so it reads as "a name the user set
is no longer shown". With a row on every turn, every windowed request reports hundreds of
orphans and the signal dies.

### Why short audio is not embedded instead

Not conservatism — measurement. `decide_name`'s own text records it: *the one Phase 0 miss
was a 2.1 s turn and it scored its own speaker lowest*. A short embedding is not weak
evidence, it is **actively misleading**: it pushes the correct person down the ranking. The
3 s floor stays.

### The residue, and what step 4 does with it

Only for groups that inheritance cannot reach — where no turn under one label is long enough
for any acoustic judgement.

**Input:** the window's turns with labels, times and text; the names already settled; the
site roster. **Task:** assign a name only where the dialogue makes it near-certain, and
return unknown otherwise.

Three hard constraints:

1. it may never overwrite a row a voiceprint confirmed — it fills gaps only;
2. it may only choose from the roster and from names already present — never invent a person;
3. its output is `tentative`, `source='dialogue_inference'`, and **never enrols**. This is an
   inference about an inference; it is two steps further from a human than harvest.

The reasoning in the example is legible: Ivy asks whether the lift will work, `spk_0` asks
*"Will they be using it?"* — the same questioner following up — while `spk_1` answers three
times. Two people, and `spk_0` is Ivy.

**The risk to hold:** a language model will produce a confident wrong attribution as readily
as a right one. The constraints above are what bound it — a closed candidate set, permission
to refuse, and no path into the library.

**There is no host for this today, and that is a design task, not a detail.** org-api is
in-VPC with no NAT, so an HTTPS call to a model provider black-holes to timeout with zero log
output (BUG-36), and it does not import `llm_utils` at all. `SpeakerEmbedFunction` is
non-VPC and could reach the internet, but its environment carries only `S3_BUCKET`,
`ECAPA_MODEL_S3_KEY` and `VOICEPRINT_WRITER_FUNCTION` — no provider key — so
`llm_utils.api_key_configured()` is False, `call_llm` returns `(None, "not configured")`, and
the tier would refuse every window forever while every test stayed green. It also holds
`ReservedConcurrentExecutions: 5` against `HTTP_TIMEOUT` 150 s x `MAX_ATTEMPTS` 4, which can
exceed its own 600 s timeout while occupying one of five slots.

Step 4 therefore begins with choosing and wiring a host, with a stated timeout budget. Until
that is done it is not implementable, and any estimate that treats it as prompt work is
wrong.

---

## Step 1 — a profile belongs to a person, not to a string

**1a. Resolve the name against the directory.**

Case-folded, whitespace-trimmed, and **normalised the same way folder names are written** —
`re.sub(r'[<>:"/\\|?*\s]', '_', raw.strip())` (`lambda_org_api.py:2156`). Without that,
rule 1 below is dead for every multi-word name, since "Neil Blunden" is stored as
`Neil_Blunden` (review #7).

Order:

1. `folder_name` = safe_name(input)
2. `concat_ws(' ', first_name, last_name)` — **not** `first_name || ' ' || last_name`, which
   is NULL whenever `last_name` is NULL, and `last_name` is NULL in practice (review #8; the
   known `Ben_UCPK_` folder comes from exactly that)
3. `first_name` alone, only if unique in the company

Scoped to the caller's company, `archived_at IS NULL`.

**1b. Ambiguity refuses; it does not guess.** Two candidates, or none: create the profile with
`user_id = NULL`. It still works by display name. A wrong link files one person's voice under
another's identity, and the site filter then hides it from the site they are on while offering
it on one they are not — invisible to everybody.

**1c. The answer says what happened.** The 202 gains
`linkedTo: {userId, matchedOn} | null` with a reason (`ambiguous`, `not-in-directory`). A
silent NULL is what made the filter a no-op once already.

**1d. Linking must also apply to profiles that already exist** (review #3). `upsert_profile`
returns early when it finds a profile by name (`repositories/voiceprints.py:122-129`) and
never touches `user_id`, so passing `user_id=` links **only at creation**. The found-branch
needs `UPDATE speaker_voiceprints SET user_id = %s WHERE id = %s AND user_id IS NULL`, and a
one-off backfill for the existing population — otherwise step 2 stays inert for everyone who
already has a profile, which is the identical failure this spec was written to fix.

**Dropped claim.** The first draft said linking "strengthens the same-name guard". It does
not (review #11): `upsert_profile`'s lookup still keys on
`(company_id, display_name, consented_by)`, so two Leos attested by the same person still
merge. Changing that lookup is a separate question and is **not** proposed here.

---

## Step 2 — score against the people who could plausibly be there

**2a. Send the site.** `speaker_match`'s artifact gains `site_id`, resolved by the
**established authority chain**, not a shortcut (review #9). The chain in
`lambda_item_writer.py:645-649` is, in order: `site_for_media` (session-exact) →
`meeting_session.site_id` → `site_for_day` (day majority) → membership. A match request holds
a `session_base`, so starting at the day-majority rung inverts the order: somebody who
recorded at two sites in one day gets the majority site, the roster narrows to the wrong
people, the right person is dropped, and the result is a refusal — indistinguishable from
"the model got worse".

`resolve_site` is **not** available here: it lives in `lambda_ingest.py:231`, takes a report
document, and org-api does not import that module.

**2b. Fix the branch before using it** (review #2). As written, linking a profile *removes*
its unconditional escape — and `users.upsert_field_only_user`
(`repositories/users.py:63-84`) writes only `users`, no membership. So a `field_only` person
becomes **less** matchable after step 1 than before: matchable while unlinked, invisible once
linked. That is a net regression introduced by the fix.

The branch must become:

```sql
AND (p.user_id IS NULL
     OR NOT EXISTS (SELECT 1 FROM memberships m2
                     WHERE m2.user_id = p.user_id AND m2.archived_at IS NULL)
     OR EXISTS (SELECT 1 FROM memberships m
                 WHERE m.user_id = p.user_id AND m.site_id = %s
                   AND m.archived_at IS NULL))
```

"Belongs to no site" and "belongs to this site" both stay; only "belongs to other sites"
is excluded. The `archived_at` filters are not optional garnish — **every other membership
query in the repo has one** (`repositories/memberships.py:29, 52, 71, …`), and without it a
person removed from a site keeps matching there: a guard satisfied and ineffective.

**2c. Membership, not the daily roster, for now.** "Who recorded here that day" is tighter and
is the better answer eventually, but it derives from `recordings.site_id` — 97 % on PROD,
**54 % on TEST**. A filter that silently drops half the candidates is indexed as poor
recognition. Log the difference between the two rosters before switching.

**Why this matters more than any threshold.** `decide_name` takes the runner-up as the maximum
over every other candidate. A larger pool raises the chance a stranger scores high, the margin
collapses, and everything degrades to `tentative`.

---

## Step 3 — one naming gesture enrols a person, and it is a machine-fed path

**This is the part the review broke, and the honest version is smaller in its claims.**

A correction supplies **one** human-vouched turn. Every other turn in the cluster is assigned
by `cluster_turns`, and `_propagate` writes them `tentative` on purpose. Harvesting them as
enrolment samples promotes a machine suggestion to permanent biometric ground truth. It also
routes around both devices that hold the line today:

- propagated rows carry `voiceprint_id=None` **specifically** so machine output cannot reach a
  profile (`lambda_voiceprint_writer.py:92`) — a harvested sample writes into
  `speaker_voiceprint_samples` directly;
- samples have their own `source` column and the only writer passes `source="correction"`
  (`lambda_voiceprint_writer.py:115, :158`), so harvested and human-anchored samples would be
  **indistinguishable in the database**.

### The version that ships

Harvest is worth having: one turn is often shorter than 10 s and a single sample makes a weak
profile. But it is described truthfully and bounded.

- **A distinct source.** Harvested samples are `source='correction_propagation'`. Human-anchored
  ones stay `'correction'`. The two populations must be separable forever — for audit, for
  measurement, and so a bad batch can be deleted without touching what a person vouched for.
- **A profile built only from harvest cannot be promoted.** `status` stays `tentative` until it
  holds at least one `'correction'` sample. A tentative profile can already only produce a
  tentative name (`lambda_speaker_embed.py` caps it), so the loop cannot bootstrap confidence
  from its own inference.
- **The admission bar is the one threshold that was measured.** Cluster membership under
  complete linkage already implies agreement ≥ τ with **every** member including the anchor,
  and τ = 0.85 is frozen from Gate A across two Phase 0 sessions. That is a real bar — it is
  simply not a human one, and the spec no longer pretends otherwise.

### Admission rules

For each cluster member, in descending agreement with the leave-one-out centroid:

- `duration ≥ ENROL_MIN_TURN_S` = **10 s** (review #4). Not 5. `window_is_homogeneous` returns
  `None` for fewer than two frames (`voiceprint_utils.py:151-153`), frames are cut at
  `FRAME_SECONDS = 5.0`, so a 5–9.99 s window is *unjudgeable* and the "refuse None" rule
  rejects it. A 5 s floor admits nothing at all. The codebase already knew this:
  `lambda_org_api.py:1548-1549` documents *"a window under ten seconds cannot be judged
  homogeneous and is not enrolled"*. Any change to `FRAME_SECONDS` also moves
  `DEFAULT_MAX_FRAME_SPREAD = 0.35`, which was measured at 5 s frames.
- `window_is_homogeneous` is `True` — not `None`.
- `add_sample`'s existing rule passes: not closer to another profile than to this one.

Stop at `ENROL_MAX_SAMPLES` = **6** or `ENROL_MAX_SECONDS` = **60**.

**Several samples, not one stitched clip.** A profile is already a *set* of sample rows and
`aggregate_scores` takes the max per person, so contiguity buys nothing and costs three
things: samples stop being individually removable (§6 withdrawal requires it), diversity is
flattened, and every splice introduces a spectral discontinuity present in no recording.

### What this costs to build (review #5, #6)

Not a small change, and the first draft implied it was:

- `_propagate` returns only `{turn_ref, state, cluster_ref, asserted, display_name}`
  (`lambda_speaker_embed.py:440-442`). The vectors, the usable turns, their
  `source_filename`/`start_sec`/`end_sec`, and the cluster labels are local and discarded.
  `turn_ref` carries no duration, so the 10 s / 60 s budget cannot even be computed from what
  it returns. It must return more.
- Harvest needs **frame** embeddings for the homogeneity check; `_propagate` computes one
  whole-turn embedding per turn and drops the clip. Each harvested turn must be re-fetched and
  re-framed. Raw audio is cached (`_AUDIO_CACHE_MAX = 8`) so S3 cost is small; ONNX cost is
  not — ~98 ms per second of audio, measured.
- The writer reads `event["enrol"]` as a **single dict** and calls `add_sample` once
  (`lambda_voiceprint_writer.py:105-118`). A list needs a new payload shape and per-sample
  handling of `EnrolmentBelongsToSomebodyElse`; today one refusal is caught for the whole
  enrolment.
- **Ordering.** `_propagate` runs at `:487`, *before* the anchor enrolment's own homogeneity
  and `between-voices` checks at `:505-526`. Harvest must be gated on the anchor surviving
  those — otherwise six samples get stored for a profile whose own corrected window was judged
  to hold two voices, which is the exact condition one sample is refused for.

---

## Step 5 — names spoken in the room (later)

*"Neil, can you check that"* is evidence that **Neil is present**, not evidence about **which
voice is Neil**. The honest use is as a **source of candidates** — a name spoken in the session
adds that directory entry to the pool even if they are not a site member.

---

## What the loop must never do

The first draft's absolute rule does not survive step 3. The rule that does:

> **Confidence comes only from people. Coverage may come from inference, labelled as such.**

A profile may be *built* from machine-selected audio, but it cannot be *promoted* by it: only
a `'correction'` sample moves a profile past `tentative`, and `confirmations_count` continues
to count only `source='correction'` turn names. So a wrong harvest costs recall and a
correctable label — never a confident wrong identification, and never a profile whose
provenance cannot be traced and deleted.

---

## Two decisions that are not implementation details

**1. Harvesting stores biometric data for people who were never asked.** Under the NZ Privacy
Act a voiceprint is biometric information. Per the standing instruction (ship first, formalise
later, state the risk), this is built with three properties that cost nothing now: a separate
switch defaulting off; every harvested sample traceable to the correction that caused it via
`correction_ref`; withdrawal reaching the whole inheritance in one act.

**2. A harvested profile is an identity claim about a real person.** DECIDED: link it.

The claim already exists — the directory entry, the name on reports and emails. A voiceprint
does not create it; it adds a biometric link to it. Refusing to link costs the site narrowing
that is the single largest accuracy lever, and reinstates the same-name collision fixed on
2026-08-14. So the link is made, and three columns record **how**, on the same discipline as
`consented_by`: `linked_by` (whose correction), `linked_at`, `linked_on` (which rule matched).
When somebody eventually asks why the system believes a voice is Neil Blunden, there is an
answer — and the cost of recording it today is zero.

`users.kind = 'field_only'` means this reaches people with no account, who cannot see or
object to it. The person who can act for them is the site manager who put them in the
directory, so withdrawal must be reachable by that role — verified: `_CORRECTION_ROLES`
includes `site_manager`.

---

## Order

| | | why here |
|---|---|---|
| **0a** | **Write-time precedence in `record_turn_name`** | Nothing else may add a source until a weaker row can no longer supersede a stronger one |
| **0b** | **Label inheritance** | Deterministic and needs no model — but not free: withdrawal reach, an unname tombstone, split awareness and `TOLERANCE_SEC` all move with it |
| 1 | Link profiles to `users.id` (+ backfill) | Everything after it is inert without it |
| 2 | Fix the site branch, then send `site_id` | Two independent inertnesses; both must go |
| 3 | Harvest the cluster, labelled as inference | Largest build; multiplies step 1 |
| 4 | Dialogue inference (LLM) | Only for what inheritance cannot reach |
| 5 | Names in transcript as candidates | Second-order |

---

## Verification — by removal, because every guard here fails silently

| step | removed | must go red |
|---|---|---|
| 0a | the precedence check in the supersede | a weaker row buries a human correction |
| 0b | the speaker label in `_session_turns` | inheritance has nothing to group by |
| 0b | grouping on `(source_filename, label)` | two calls' `spk_0` merge into one person |
| 0b | the duration floor exemption | inheritance rejects the very turns it exists for |
| 0b | provenance on inherited rows | withdrawal returns 200 and the names stay |
| 0b | the unname tombstone | a rejected name is re-derived on the next run |
| 0b | grouping before the budget split | a group split across runs inherits nothing, silently |
| 0b | the explicit label-presence check | an undiarised file becomes one speaker |
| 1 | the directory lookup | a profile links to nobody |
| 1 | the safe_name normalisation | a multi-word name resolves to nobody |
| 1 | `concat_ws` (back to `\|\|`) | a NULL last name resolves to nobody |
| 1 | the ambiguity refusal | two same-named people resolve to one |
| 1 | the found-branch UPDATE | an existing profile never links |
| 1 | `linkedTo` in the response | a silent NULL becomes invisible again |
| 2 | `site_id` in the **artifact** | narrowing reverts to a no-op |
| 2 | the "belongs to no site" arm | a `field_only` person becomes unmatchable |
| 2 | the `archived_at` filters | a removed member keeps matching |
| 3 | `source='correction_propagation'` | harvested and vouched samples become indistinguishable |
| 3 | the tentative cap on harvest-only profiles | the loop promotes its own inference |
| 3 | the homogeneity check | a two-voice window enrols |
| 3 | the `correction_ref` | withdrawal cannot reach the inheritance |
| 3 | the anchor-survived gate | harvest runs after its own anchor was refused |

**One live check no unit test can make.** After step 2, a match run on TEST must show a
**smaller number of distinct people** than the same run without `site_id`. Count
`len({p['person_key']})` — **not** `len(profiles)`, which is a *sample* count, since
`profiles_for_matching` JOINs `speaker_voiceprint_samples` (review #12). That number is already
mislabelled "profile(s)" in three log lines, and after step 3 it decouples from people
entirely — it would move *upward* as harvest lands, for the wrong reason.
