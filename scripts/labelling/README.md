# Voice labelling pack — what to do, and how long it takes

**Your part: about 45 minutes, in one sitting, offline.** Everything else is already done.

You will listen to short clips and say who is speaking. That is the whole task.

---

## Why this is the one thing that cannot be automated

Automatic cross-company renaming is blocked on a single missing input: **nobody has ever
listened to this audio.** The 2026-08-30 measurement says so in its own words — it argues by
contradiction instead, which cannot produce a ROC curve. Every threshold quoted so far has
been fitted to the material that produced it, which is the mistake that measurement itself
warns about.

With labels, the ~0.50 window seen on 2026-09-18 either holds up or it does not, and we find
out in an afternoon. Without them, `auto` stays shut indefinitely and the feature ships as a
suggestion list.

---

## The three steps

### 1. Fetch the clips (one command, needs AWS)

```bash
python scripts/labelling/select_turns.py --scan fieldsight-data-509194952652 transcripts/ \
    --out clips.json
```

It prints the pack and, if the sample falls short of any quota, says which one and exits 1.
It **reads no audio** and downloads nothing — it produces the list.

Then fetch the audio named in `clips.json`. **Take it from `audio_segments/`, not from
`users/.../audio/`**: after batching, a transcript's timeline belongs to the batch wav, and
the original 30-second upload it looks like it came from is a different file. Deriving the
key from the stem fetched 30-second files twice in September, and every turn past the first
30 seconds was silently cut from audio that was not its own.

```bash
aws s3 cp "s3://fieldsight-data-509194952652/audio_segments/<folder>/<date>/<stem>.wav" ./clips/
```

> If a listing comes back empty, check the grant before believing it. A missing
> `s3:ListBucket` returns **403, not 404**, and from here that looks exactly like "there is
> nothing there". It has happened eight times in this repo, once losing a day of data.

### 2. Label them (offline, in a browser)

Open `scripts/labelling/annotate.html` directly — no server, no network. Load `clips.json`,
then select the downloaded `.wav` files. Then:

- Number keys pick an answer, arrows move between clips.
- Answers are kept in the browser as you go, so you can stop and come back.
- Press **Download labels.json** when you are done and send that file back.

### 3. Send `labels.json` back. That is the end of your part.

---

## Three answers that are not "a name", and why each one matters

**"Not sure"** — use it freely. A forced guess and a confident label look identical once they
are in the file, and a ROC built on quietly-guessed labels is worse than no ROC because it
looks like one. Skipping is the honest answer and costs nothing.

**"A real person, none of the above"** — different from "not sure", and it is the single most
valuable answer in the pack. The measurement needs **negative controls**: sessions where
nobody in the enrolment library was present. Without them there is no way to tell "the system
recognised Ben" from "the system picks Ben out of anyone", which is exactly the failure
measured on 2026-09-10, when a stranger was confirmed at 0.445 with a wide margin.

**"Unusable clip"** — silence, noise, or cut off mid-word. Better recorded than laboured over.

---

## The clips that decide the answer

Two of these are worth more than the rest, and the tool marks them:

- **2026-09-10.** Nobody from the voice library attended. Every name the system produced for
  that day is a false positive *if* that is true — and nobody has confirmed it is. This is the
  clip set that decides whether a rejection floor is possible at all.
- **The turn the system called "Mike".** It scored Ben at 0.137 and Mike at 0.405. That may
  well be correct — the speaker may simply not have been Ben. Nobody has listened. Your answer
  settles it either way, and either answer is useful.

---

## Before any threshold is written down: re-run the ONNX parity check

Every number this pack exists to settle — `DEFAULT_MIN_MARGIN` (0.15),
`DEFAULT_MAX_FRAME_SPREAD` (0.35), the Phase 0 separation, and the ~0.50 window seen on
2026-09-18 — is a statement about **one particular ONNX export** of the ECAPA model. The
Lambda cannot carry torch and speechbrain, so it runs that export; an export that drifted
would move every similarity score a little, and every threshold would quietly stop meaning
what it says while nothing failed.

`tests/unit/test_voiceprint_onnx_parity.py` is the gate for exactly this. It **does not run
in CI**, deliberately: the comparison needs the 84 MB model from S3, which CI has no reason
to download. That is a sound trade and the file argues it well ("a guard that is skipped
wherever it would fire is not a guard", plus a companion test so the skip cannot become
silence). It was last executed against the real model on **2026-08-13**.

So: **run it against the model in S3 before quoting a threshold from this labelling round.**
Otherwise the threshold is fitted to one export and applied to another, and the drift is
invisible from every direction — it is the one failure mode that would make a clean ROC
wrong rather than merely weak.

```bash
ECAPA_ONNX_PATH=/path/to/ecapa_tdnn.onnx pytest tests/unit/test_voiceprint_onnx_parity.py -q
```

## What happens next

The labels go into the M2 analysis: cosine distributions split same-speaker / different-speaker,
stratified by clip length, enrolment length, same-vs-different device, and same-day / cross-day
/ cross-week. `auto` opens only if that clears **AUC ≥ 0.90 with TPR ≥ 50% at FPR ≤ 0.5%**, on
held-out data. If it does not clear it, the feature ships as a candidate list for a human to
confirm, and we will say so plainly rather than pick a threshold that fits.
