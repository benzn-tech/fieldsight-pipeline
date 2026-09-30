# Record the recording conditions of every voiceprint sample (design, 2026-09-30)

**Line:** voiceprint. Step 1 of 3 of "condition-aware profiles" (owner, 2026-09-29/30:
"one step at a time"). This step changes NO matching behaviour; it only records facts.

## Why (measured 2026-09-30 on the 42 owner-labelled clips)

For Ben Lin's 19 genuine clips against his live samples:
- signal-to-noise ratio vs score: Spearman rho = +0.76; still +0.63 after removing the
  effect of clip length. Background noise alone: rho = -0.70.
- clips recorded on the enrolment device (Ben_UCPK2, where every live sample came from)
  scored 0.540 on average; clips from 6 other devices 0.413; the two from Petros's
  device 0.221.
- energy below 200 Hz ("wind") is NOT a usable wind measure: it tracks voice pitch
  (rho +0.58 the wrong way). Not recorded.

So the two condition axes worth keeping are background noise and device. Without them
on the sample row, step 2 (does a profile need a sample per condition?) and step 3
(condition-matched comparison) cannot be measured on live data.

## What

Migration (next free number) adds nullable columns to `speaker_voiceprint_samples`:
`level_dbfs`, `noise_dbfs`, `snr_db` (double precision). NULL for rows before this.
Device is NOT a column: it is derivable from `s3_key` (`users/{folder}/...`) and a copy
would drift.

- **Embedder** computes the three numbers on the exact window it enrols (the 10 s window
  after narrowing) — RMS in dBFS; noise = 10th percentile of 50 ms frame RMS; snr =
  level − noise — and sends them on BOTH enrolment paths (the correction artifact's
  `enrol` and each `harvest` entry) and the standalone `op=enrol`.
- **Writer** passes them to `add_sample`, which stores them. The writer stays numpy-free.
- **Voices page / API:** nothing shown to customers.

## Tests that must go red without the change

- Pure function: known sine + known noise gives the expected level/noise/snr.
- Seam test through both real handlers (test_embedder_writer_contract.py): the numbers
  sent by the embedder reach `add_sample` on every enrolment path.
- `add_sample` SQL text includes the three columns in the INSERT; a conflict (repeat
  window) does not overwrite them with NULL.
- Real DB (orchestrator): migration + insert on fieldsight_test, rolled back.
