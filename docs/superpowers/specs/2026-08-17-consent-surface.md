# Spec: consent, and the other three things between a rename and a self-filling library

**Status:** proposal. Needs one decision from the owner before any code.
**Date:** 2026-08-17
**Repo:** `fieldsight-ui` (the surface) with a `fieldsight-pipeline` half.

> **The first draft of this document claimed the consent surface was "the whole remaining
> distance". That was wrong on three independent counts, found in review and corrected
> below.** Consent is the *first* gap, and closing it alone changes nothing anybody can
> observe. Keeping the correction visible because the mistake is the useful part: I had spent
> two days on the guard, then on consent, and each time mistook the thing in front of me for
> the last thing in the way.

---

## What actually has to happen, end to end

For "rename `spk_1` to Neo, and the next meeting recognises Neo":

| # | step | state |
|---|---|---|
| 1 | the correction requests enrolment | **missing** — needs `consent_given` + `consented_by`, and no surface sends them |
| 2 | a sample survives the guards | **unreliable** — homogeneity refuses real site windows; under 10 s is unjudgeable; `EnrolmentBelongsToSomebodyElse` refuses a sample closer to another profile |
| 3 | the next meeting is matched against stored profiles | **missing entirely** — matching runs only from `POST /sessions/{s}/speaker-match`, called by hand. Nothing schedules it, and nothing calls it at finalize. `lambda_org_api.py:1443` is the only writer of a match artifact. |
| 4 | matched names reach the transcript | **switched off** — writing needs `SPEAKER_IDENTITY_MODE=on`; `shadow` computes and writes nothing, and `on` is prohibited until a matching margin is calibrated, which has not happened |

This document is about **step 1 only**. Steps 3 and 4 are separate pieces of work, and step 4
is a decision rather than an implementation. Shipping consent on its own produces a library
that fills up and is never consulted — which is worth knowing before choosing how much to
invest in the surface.

### The ordering is forced, and the appealing shortcut does not exist

Step 1 needs a decision that is not mine to make, so the obvious move is to do step 3 first —
automatic matching is pure backend, needs no privacy decision, and after the 2026-08-17 fix a
match run does useful work even with **zero** profiles, because label inheritance rides on the
same call.

**That reasoning is wrong, and I got as far as picking the implementation site before checking
it.** Inheritance spreads names a session *already holds* to its short turns
(`_inherit_labels` skips any label group with no named member). A session that has just
finalized holds no corrections, so there is nothing to spread. Inheritance pays off on the
session somebody has just corrected — which is the on-demand call that already exists — and
pays nothing on a fresh one.

So automatic matching delivers exactly nothing until profiles exist, profiles need step 1, and
step 1 needs a decision. The chain cannot be reordered to avoid the decision, and the useful
thing to record is that the shortcut looked real enough to start building.

For whoever does eventually build step 3: `lambda_finalize_claim` is the site. It is in-VPC,
already claims the session and already has folder, date, site and recipient from Aurora — the
same inputs `speaker_match` gathers. The artifact contract (`turns`, `label_map`, `site_id`)
would then have two producers, so it needs to move to a shared module rather than be written
twice, which is the defect shape this feature has produced five times.

---

## Step 1, precisely

Enrolment is requested only when a correction carries `consent_given: true` and
`consented_by` (`lambda_org_api.py:1647`). Everything inside that branch is skipped
otherwise, including creating the profile row at all. `upsert_profile` **raises** if asked to
create a *named* profile without it (`voiceprints.py:107`); an unnamed profile needs none,
which is what `user_id` being nullable is for.

So a rename in the browser names the meeting and stores nothing, and the observable is not an
empty profile but **no profile**.

*(That the product sends `consent_given: false` is from the frontend repo, which I have not
verified against its `origin/main` — the local checkout is not trustworthy. What is certain
from this side is that nothing has ever arrived carrying consent except hand-built calls.)*

---

## The constraint that shapes every option

**`consented_by` is a `uuid`, and it identifies the subject — the person whose voice it is.**
Not the labeller. The endpoint's own 400 says so: *"record whose voice this is, not who is
doing the labelling"* (`lambda_org_api.py:1657`). Migration 0042 exists because a timestamp
alone could not tell the subject agreeing apart from the wearer clicking a box for them.

Two consequences that the first draft of this document missed, and that rule out the design
it proposed:

1. **It is the deduplication key.** `upsert_profile` reuses a profile on
   `(company_id, display_name, consented_by)` (`voiceprints.py:132`). Repurposing the column
   to mean "the labeller" would give one person a separate profile per labeller — and two
   profiles of the same voice are exactly what `EnrolmentBelongsToSomebodyElse` was written
   to catch, so they would then start refusing each other's samples.
2. **A uuid only exists for people who have an account.** For a subcontractor with no
   FieldSight login there is no id to record. The column is nullable and unvalidated, so
   *something* could be written there — but writing a fabricated id into the column built to
   record who agreed is worse than the silence it replaced.

**So today, enrolment is only expressible for people who have accounts.** That is not a bug
to fix on the way past; it is the shape of the problem, and it is the same population
question `0045` raises: somebody with no account cannot see the name, cannot object to it,
and cannot withdraw it.

---

## Three bases, and what each buys

### A. Attestation — the labeller states that the subject agreed

Needs a new `consent_basis` column and, for subjects without accounts, somewhere to put an
identity that is not a uuid. **Not "one dialog change"** — that was the first draft's error.

- **Buys:** the loop's first step closes for account-holders today.
- **Does not buy:** any evidence the person agreed. It records a claim, made by someone with
  an interest in making it.
- **Only honest with `consent_basis` shipped in the same change.** Without it an attestation
  writes `consent_at` and `consented_by` indistinguishably from a real agreement, and
  `profiles_for_matching` gates on `consent_at IS NOT NULL` alone (`voiceprints.py:302`) — so
  everything downstream would treat the two as the same thing forever.
- **UI wording:** "I have asked this person and they agreed", not "consent given". The second
  reads as something the system verified.

### B. The subject confirms on their own device

- **Buys:** agreement from the right person, and a `consented_by` that means what the schema
  says.
- **Costs:** works only for people reachable by account, e-mail or SMS — and the population
  most often named is the least reachable. Pending states that may never resolve.
- **Blocked today for anyone without an account:** SES is still sandboxed, so the system
  cannot mail an unverified address at all. That has to be solved first or B is
  account-holders-only, which is the set that least needs it.

### C. Site notice with an opt-out register

- **Buys:** the only basis that scales to a site full of subcontractors nobody has contact
  details for.
- **Costs:** it puts the burden on the individual to object, and it needs induction material
  that actually exists.
- **Where the check goes, and it is not one place.** The first draft said "checked in
  `upsert_profile`". That is necessary and not sufficient: samples are added by the writer
  through `add_sample`, by **two** enrolment paths plus harvest (`voiceprints.py:242`), and
  the profile row is created before the embedder ever runs. Someone opting out after their
  profile exists would keep accumulating samples. The register has to be checked at
  `add_sample` as well.
- **Withdrawal is not already this.** `withdraw` deletes the vectors, keeps the audit row,
  and un-names the turns inside itself (`voiceprints.py:368`) — but `upsert_profile` excludes
  withdrawn rows from reuse (`voiceprints.py:133`), so the *next* correction naming that
  person creates a fresh profile. Today withdrawal is an erasure, not a standing objection.
  Only the register makes it standing.

---

## Recommendation

**C first, then A on top of it.** The first draft said "A now, C as the standing basis" and
then, two paragraphs later, "C first if it is going to exist at all". Those contradict each
other and the second one is right: an opt-out register that arrives after a thousand profiles
have been built is a register of people whose data was already collected.

B where the person has an account, whenever the account-holder path is worth building — it is
strictly better than A for that set, and it is the only option whose `consented_by` means what
the column says.

**A alone, first, is the option I would argue against**, having proposed it in the first
draft. It closes step 1 for account-holders only, records a claim rather than an agreement,
and — because steps 3 and 4 are still missing — produces nothing observable in exchange.

---

## Privacy: considerations, not advice

I am not able to give legal advice and this section should not be read as any. What I can do
is point at the questions and at what this repository already assumed:

- migration `0038` cites both the Privacy Act and the **Biometric Processing Privacy Code** —
  the Code is the more specific instrument and the first draft of this document omitted it
  entirely while asserting what the Act requires;
- whether a third party's attestation is a sufficient basis, and whether a site notice is,
  are questions for someone qualified. The design should make the *basis* explicit and
  recorded either way, which is what `consent_basis` is for;
- proportionality and retention are unaddressed by all three options. Nothing here says how
  long a voiceprint lives without a fresh basis.

---

## Also worth deciding, but not by this document

- **Existing TEST profiles** were created by API calls carrying a consent claim I typed
  myself. They are test fixtures. Delete them rather than migrating them to a basis.
- **Ambiguous names.** `resolve_display_name` returns `(None, "ambiguous")` for a name
  matching two people (`users.py:275`), and today enrolment **proceeds anyway** with an
  unlinked profile (`lambda_org_api.py:1663`). An opt-out register keyed on a name would need
  ambiguity to refuse instead — a behaviour change, not current behaviour.
- **`GET /api/org/voiceprints` returns `consentAt` but not `consentedBy`** (`:1333`). If the
  basis matters operationally, the listing has to expose both. The first draft claimed it
  already showed who consented; it does not.

---

## The decision I need

**Which basis, and whether step 1 is worth closing before steps 3 and 4 exist.** If the answer
to the second is no, this document should sit until automatic matching does.
