# Morning verification — 2026-08-17

Tonight's work was almost entirely on **speaker naming**, and almost all of it is on TEST
only: `PROD_SPEAKER_IDENTITY_MODE` is `off`, so on prod every route below returns 404 and
none of the code paths execute. Testing this means testing the **dev site against TEST**.

Two things to know before you start, because both make a working system look broken:

1. **You cannot create a voiceprint from the dev site at all**, and for two independent
   reasons — a review of the first draft of this document caught the second one, which had
   been left out entirely:
   - **there is no consent surface.** Enrolment only happens when the correction carries
     `consent_given: true` and `consented_by` (`lambda_org_api.py:1646`), and everything
     inside that branch — including creating the profile row — is skipped otherwise. The UI
     ships no such control by design, and enrolment UI is explicitly out of scope in the
     frontend spec. **So a correction made in the browser creates no profile and attempts no
     enrolment**, and after it `GET /api/org/voiceprints` shows *nothing new at all* rather
     than an empty profile.
   - **the guard refuses site audio.** When enrolment *is* requested, the homogeneity guard
     has so far refused every window of real audio, and no number that would fix it survived
     measurement (`docs/superpowers/specs/2026-08-17-homogeneity-threshold-measured.md`).

   Step 0 addresses only the second. Step 3 gives the API call that addresses the first.
2. **Naming a speaker does not mean they will be recognised next time.** Recognition needs a
   stored voiceprint, and there are none. What a correction does today is name this meeting —
   which is real, and checkable below.

Throughout, `{sub}` is your Cognito sub and `{folder}` a user folder such as `Ben_UCPK2`.
**Substitute before running.** An unsubstituted placeholder in braces leaves bash to pass it
literally, the command succeeds, and it returns nothing — which looks exactly like the empty
result the check exists to detect.

---

## Step 0 — two commands, or most of this document is untestable

```bash
gh variable set TEST_VOICEPRINT_MAX_FRAME_SPREAD --body 0.7
gh workflow run "Deploy FieldSight TEST (SAM)" --ref develop
```

Wait for the deploy (~10 min). To undo: `gh variable delete
TEST_VOICEPRINT_MAX_FRAME_SPREAD`, redeploy.

**What 0.7 is and is not.** It is not a measured threshold — three attempts to justify one
were withdrawn, and the compiled-in 0.35 has not moved. It is a setting that lets enrolment
succeed so the *rest* of the chain can be observed. Every log line for an enrolment admitted
under it says so, and each sample records the limit it came in under in
`speaker_voiceprint_samples.admitted_max_spread` (NULL under the ordinary guard). So the
profiles you create this morning are evidence about **plumbing**, never evidence that the
window held one voice.

---

## Step 1 — one command that answers "is any of this even wired"

```bash
python scripts/verify_speaker_chain.py --sub {sub} --user {folder} --date 2026-08-12 --env test
```

Reads only. It prints, in order:

| what it prints | what a bad value means |
|---|---|
| `SPEAKER_IDENTITY_MODE` from the **deployed function** | `off` → nothing below can work; the repo variable is not the same thing as what deployed |
| `VOICEPRINT_MAX_FRAME_SPREAD` | `(unset -> 0.35)` after step 0 → the deploy did not carry it |
| `S3 trigger on voiceprint_requests/` | **`NO S3 NOTIFICATION`** → every request below returns 202 and nothing runs. Hand-wired outside the template (BUG-33), so it can be lost by a bucket change unrelated to this feature. Fix this before reading anything else as evidence. |
| each profile's `samples` / `humanSamples` / last attempt | `samples: 0` with a refusal reason is the pre-step-0 state |

**This script has never run against a live stack** — credentials were expired all night. It
was reviewed and three defects were fixed, but if it fails in a way that looks like a bug in
the script rather than in the system, that is the likelier reading. Fall back to the web UI.

---

## Step 2 — the thing you actually want to see

On the dev site, open a **chunk-session** recording (filename contains `sid<32 hex>`; a
legacy RealPTT recording has no sid and every write endpoint will correctly decline).

Rename one speaker on a turn of **at least 3 seconds**.

| check | pass | fail |
|---|---|---|
| that turn shows the name | `confirmed` | nothing renders → the viewer is not reading `speaker_name` |
| **other long turns of the same voice** change too | several more turns named, `tentative` | only the one turn → propagation found no cluster; normal if that voice spoke once |
| **turns under 3 seconds** change too | more turns named, `tentative` | none → label inheritance did not run; this is the thing #557 fixed |
| a profile appears in `GET /api/org/voiceprints` | **nothing new appears** | a profile *does* appear → the UI has grown a consent control since this was written, which changes what step 3 tests |

The example that drove this work named **2 turns directly, 6 by propagation, and 22 by
inheritance**. If you see the first two and not the third, that is the specific regression to
report.

---

## Step 3 — after step 0, the part that has never worked

**Not from the browser.** The dev site sends no consent, so a correction made there creates no
profile and requests no enrolment; repeating step 2 would prove nothing about this. Enrolment
has to be asked for explicitly:

```bash
aws lambda invoke --function-name fieldsight-test-org-api \
  --cli-binary-format raw-in-base64-out --payload '{
    "httpMethod": "POST",
    "path": "/api/org/sessions/{session}/speaker-corrections",
    "requestContext": {"authorizer": {"claims": {"sub": "{sub}"}}},
    "body": "{\"display_name\":\"{name}\",\"source_filename\":\"{file}\",\"start_sec\":0.0,\"end_sec\":15.0,\"consent_given\":true,\"consented_by\":\"{who}\"}"
  }' /tmp/corr.json && cat /tmp/corr.json
```

Pick a window of **at least 10 seconds** — under that, `window_is_homogeneous` sees fewer than
two frames, cannot judge, and refuses rather than assuming.

`consented_by` records **whose voice it is**, not who is doing the labelling. No code can
verify that claim; recording it makes it attributed, which is the most an API can do.

Then:

```bash
python scripts/verify_speaker_chain.py --sub {sub} --user {folder} --date {date} --env test
```

`samples` should now be non-zero for that profile, and `humanSamples` at least 1.

**If it is still 0**, read `lastAttemptDetail`:

- `"this window has too little speech to judge"` — the window was under 10 seconds, or the
  speech-gate dropped frames below −55 dBFS. By design; pick a longer, louder window.
- `"this window does not hold one voice"` — the guard refused at 0.7. **This is not
  automatically a defect.** Of the 23 one-voice windows ever measured, three sat above 0.7
  (0.715, 0.730, 0.777), so some windows are expected to be refused at this setting. It is
  worth reporting only if *every* window you try is refused, which would suggest the override
  did not take — check step 1's second row before concluding anything.

---

## Step 4 — naming a whole session at once

```
POST /api/org/sessions/{session}/speaker-match     body: {"user": "{folder}"}
```

Read `willWriteNames` in the response, **not the 202**. It is `true` only in mode `on`; in
`shadow` the endpoint accepts, computes every score, and writes nothing. A UI that reports
success on the status code would tell you your meeting was named when it was not.

Two mechanisms ride on this one request and only one needs a profile: voice matching does,
label inheritance does not. So **a match on a session with zero profiles can still raise the
named count** — that is #557, and before it, this call did nothing at all in the only state
the system is ever in.

---

## What is deliberately not on this list

- **Whether a name is correct.** Everything above counts names and reports states. Whether
  `spk_1` is really that person needs somebody who can recognise the voice.
- **Anything on prod.** `SPEAKER_IDENTITY_MODE=off` there, confirmed in the deploy log
  alongside `VoiceprintMaxFrameSpread=default`. Migration 0047 applied to both databases.
- **The threshold question.** It needs three different people recorded individually for
  30–60 seconds each, in real site conditions, speaking naturally. Not analysis — material.

## Known-unverified, in the order I would trust them least

1. `speaker-match` end to end. PR #539 fixed a defect that made it inert in production, and
   **that fix has never been exercised against a live stack.** Step 1 and step 4 are the test.
2. `verify_speaker_chain.py` itself — written, reviewed, never executed against AWS.
3. Enrolment succeeding at 0.7. Predicted from measured spreads, and only partially — three
   of 23 measured one-voice windows are above 0.7 — and never observed, because the guard has
   refused every window ever tried.
4. **That anyone has ever exercised the consent-carrying path from outside my own hand-built
   calls.** The first draft of this document told you to make a correction in the browser and
   then read `samples`, which cannot work; the omission was caught in review, not by running
   anything.
