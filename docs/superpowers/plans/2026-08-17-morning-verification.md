# Morning verification — 2026-08-17

Tonight's work was almost entirely on **speaker naming**, and almost all of it is on TEST
only: `PROD_SPEAKER_IDENTITY_MODE` is `off`, so on prod every route below returns 404 and
none of the code paths execute. Testing this means testing the **dev site against TEST**.

Two things to know before you start, because both make a working system look broken:

1. **You cannot create a voiceprint yet.** The enrolment guard refuses every window of real
   site audio, and no number that would fix it survived measurement (see
   `docs/superpowers/specs/2026-08-17-homogeneity-threshold-measured.md`). Step 0 below
   loosens it on TEST so the rest of the chain can be exercised at all. Without step 0,
   "the profile has no samples" is the **expected** outcome, not a fault.
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
| the name attaches to a real person | `linkedOn: folder_name` in `GET /api/org/voiceprints` | `userId: null` → no roster match on that name, which is allowed |

The example that drove this work named **2 turns directly, 6 by propagation, and 22 by
inheritance**. If you see the first two and not the third, that is the specific regression to
report.

---

## Step 3 — after step 0, the part that has never worked

Make another correction. Then:

```bash
python scripts/verify_speaker_chain.py --sub {sub} --user {folder} --date {date} --env test
```

`samples` should now be non-zero for that profile, and `humanSamples` at least 1.

**If it is still 0**, read `lastAttemptDetail`. `"this window does not hold one voice"` means
the guard still refused at 0.7 — worth reporting, because every window measured sat between
0.36 and 0.78. `"this window has too little speech to judge"` means the window was under 10
seconds, which is by design.

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
3. Enrolment succeeding at 0.7. Predicted from measured spreads of 0.36–0.78; not observed,
   because the guard has refused every window ever tried.
