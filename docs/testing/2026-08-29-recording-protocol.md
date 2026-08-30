# Recording protocol — what to record on TEST, and why each take exists

**Date:** 2026-08-29. **Audience:** Ben, holding the device.
**Purpose:** produce the audio that lets speaker identity + session brief be turned on in
production. Every take below has an acceptance check; a take that fails its check is worth
re-recording, and a take that passes is evidence the corresponding prod switch can be flipped.

---

## Before you press record

**Record exactly the way you recorded the 2026-08-27 session** — same device, same app
flavour. That session landed in `fieldsight-data-test-509194952652`, which is the bucket TEST
reads. If a recording lands in the prod bucket instead, none of the checks below can run.

**Select the site in the app before starting.** `recordings.site_id` is the authoritative site
source; when it is missing, all three fallbacks can miss at once (BUG-43) and the items land
with no site.

---

## Take 1 — Voiceprint enrolment (each person separately, 60–90 s)

This is the one with a counter-intuitive rule, and it is the reason the library is still empty.

**Speak in stretches of 8–15 seconds, with a clear pause between each.** Not one continuous
90-second monologue.

Measured on 78 real windows: a 5–10 s window is homogeneous 83 % of the time (pair-median
0.152); a 20–30 s window, **0 %** (pair-median 0.477). Long windows drift — room, distance,
posture — and the guard reads that drift as "more than one voice here". Short stretches are
not a compromise; they are the material the guard was built for.

**Use your ordinary speaking voice. Do not act.** The last enrolment attempt failed its
homogeneity check not because it was too quiet and not because the guard was wrong — it was a
multi-role script read in different voices. One person doing two voices is, to a voiceprint,
two people.

**Say ordinary work sentences, not a word list.** Suggested, ~10 s each, pause between:

> "This is Ben Lin. I'm on the Ellesmere College site this morning, walking the east
> elevation."

> "The scaffold on grid line four came down yesterday and the handrail is still off."

> 「我是 Ben，今天早上在工地东侧巡查，脚手架昨天拆了一部分。」

> "We're waiting on the electrician before the ceiling grid can close up."

> 「这一段的防水还没做完，下周一之前应该可以完成。」

Six to eight such stretches is enough. **Do one take per person**, alone, no background
conversation.

**Acceptance:** at least one turn ≥ 5 s whose frame spread is below 0.35, and a row appearing
in `speaker_voiceprint_samples`. I check this and report the measured spread per person.

---

## Take 2 — A real multi-person meeting (10–20 minutes, 2–3 people)

This is what proves speaker matching and produces a session brief worth reading.

**Each person says their own name in their first sentence** — "Ben here", "James speaking".
That is the ground truth I check the diarisation against; without it, "wrong name" and "no
name" are indistinguishable afterwards.

**Talk normally.** Interruptions and overlap are wanted: they are what the site sounds like,
and the batching path merges speaker-label namespaces across chunks, which is exactly the
thing that needs testing.

**Cover at least four distinct subjects** so the brief has something to section. Anything real
works — programme slippage, a variation, a delivery, a safety observation.

**Include at least three decisions and three follow-up actions**, said out loud as decisions:
"so we'll hold the pour until Thursday", "James, can you chase the steel delivery". The brief's
`tasks` and their `why` come from exactly this phrasing.

**Acceptance:** a `session_brief/.../latest.json` with ≥ 4 sections and ≥ 3 tasks, and speaker
names attached to the right speakers. For reference, the 2026-08-27 session produced 6
sections, 18 entities and 4 tasks.

---

## Take 3 — Proper nouns (3–5 minutes, one person is fine)

The entities feature exists because a rare brand name is usually **absent from the indexed
text entirely** — extraction keeps roughly 5 % of a transcript — and where it survives, a
vector search is a poor instrument for a token whose whole meaning is that it is unusual.

**Say each name two or three times, in different sentence positions**, inside ordinary
sentences rather than as a list. Names worth including, because they are the ones ASR mangles:

- supplier and brand names: PB Tech, Plaud, Fireflies, Procore, Bunnings, Hilti, Ramset
- the ones already known to mangle: FieldSight (has come back as *FieldSync*, *FieldSight
  Visual*), Fireflies (as *Firefire*)
- two or three of your actual subcontractor company names
- a person who is **not** in the system — a subcontractor with no account. This is the case
  the enrolment rules were rewritten for.

Example shape:

> "We ordered the brackets from PB Tech, and PB Tech said Thursday."
> 「这个是 Hilti 的锚栓，跟上次 Ramset 那批不一样。」

**Acceptance:** the brief's `entities` contains these names with their real spellings, and the
`aliases` field captures at least one mangling. Alias capture is the half that makes the search
work for the wrong spelling.

---

## Take 4 — Mixed Chinese and English (3–5 minutes)

**Code-switch inside sentences**, not paragraph by paragraph. This is the highest-risk input
in the whole pipeline: measured hallucination on mixed-language audio runs around 4 %, and it
is the dangerous kind — fluent, plausible, invented.

> 「那个 slab 的 rebar spacing 是 200，不是 150。」
> "客户说 the variation 要走 formal RFI，不然 QS 不认。"
> 「Site manager 讲的是 next Tuesday，但 programme 上写的是 Wednesday。」

**Acceptance:** I compare this take against a second transcription engine and report the
disagreement rate. This one is diagnostic rather than pass/fail — its purpose is to tell us
whether mixed-language content is safe to put in front of a customer.

---

## What I do with it

1. Read each take's transcript, brief and extraction directly from the TEST bucket.
2. Run the enrolment correction against Take 1 and report the measured frame spread per
   person — pass or refuse, with the number.
3. Check Take 2's speaker names against the ground truth you spoke.
4. Report Take 3's entity and alias capture.
5. Second-engine comparison on Take 4.

Then I tell you which of the four production switches the evidence supports turning on.

---

## The part only you can do

**1. Merge six pull requests.** Nothing below can run until these land, and my tooling refuses
to merge. All six are green and all six merge cleanly into the current `develop`; the full
suite passes with all of them applied together (3349 tests).

| PR | Without it |
|---|---|
| **#595** | enrolment cannot store anything — this is why the library is empty |
| **#593** | a cross-company recording files a voiceprint under the wrong company |
| **#596** | the brief endpoint returns 500 for every session, on TEST **and** prod |
| **#591** | a deleted recording's speech can still be named |
| **#592** | the retention sweep crashes the first time it finds a row |
| **#598** | spec + plan only, no code |

**2. Record the four takes above.**

**3. Release to production — and understand that merging is not enough.**

`develop` deploys to the **TEST** stack; `main` deploys to **prod**. Tonight's earlier work
already reached prod through the develop→main release (#597), and it changed nothing a
customer can see. That is not a bug: **the prod switches do not exist**, so the workflow's
defaults apply.

```
EnableSessionBrief = ${{ vars.PROD_ENABLE_SESSION_BRIEF || 'false' }}
SpeakerIdentityMode = ${{ vars.PROD_SPEAKER_IDENTITY_MODE || 'off' }}
EnrolOnCorrection  = ${{ vars.PROD_ENROL_ON_CORRECTION || 'false' }}
```

None of those three variables is set on the repository today. So the order is:

1. merge the six → TEST deploys automatically
2. record → I verify and report
3. open a `develop` → `main` release PR → prod deploys the code
4. **set the prod variables** — this is the step that actually delivers the feature:
   - `PROD_ENABLE_SESSION_BRIEF = true`
   - `PROD_SPEAKER_IDENTITY_MODE = shadow` first, then `on`
   - `PROD_ENROL_ON_CORRECTION = true` only after the consent question below
5. redeploy prod (a variable change alone does nothing until the next deploy — a stale env var
   is invisible and has cost this project a week before)

**`shadow` before `on` is deliberate.** In shadow the matcher computes and logs and writes no
name, so we see what it would have said against real customer audio before it says it to a
customer.

**4. The one decision I cannot make for you.** `PROD_ENROL_ON_CORRECTION = true` means naming a
speaker creates biometric data on the strength of the company's standing basis — the site
induction — rather than the subject's own account. That is the model you described, and the
code records `consent_basis` and `asserted_by` on every row so it stays auditable. Turning it
on for a customer's company is your call, not mine, and it should follow whatever you have
settled with that customer in the subcontract.

---

## Where this file is

`C:\Users\camil\Dropbox\wt-batchmap\docs\testing\2026-08-29-recording-protocol.md`
