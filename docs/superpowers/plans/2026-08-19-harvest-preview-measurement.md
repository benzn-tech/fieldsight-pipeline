# Plan: measure the harvest selection before changing it

**Spec:** `../specs/2026-08-18-enrolment-by-set-agreement.md`
**Decision taken:** measure first, store nothing.
**Date:** 2026-08-19

---

## The shape of the measurement

**Do not ship the change and watch it.** Add a read-only operation that computes what the new
rules *would* select, run it over recordings that already exist, and compare old rule against
new rule on the same audio.

That is better than the `VOICEPRINT_HARVEST_MODE` switch the spec proposed, for three reasons:

* it touches no production path, so there is no version of it that accidentally stores;
* it runs on **any historical session**, not only on sessions somebody happens to correct —
  and harvest today only runs when the anchor was accepted, which on real audio is never;
* it can run against recordings whose composition the owner has already attested, which is
  the closest thing to ground truth this problem has.

## What it returns, and why each column earns its place

`op: "harvest_preview"` — same discipline as `op: "spread"`: scalars leave, vectors never do.
Per candidate turn in a session:

| field | the question it answers |
|---|---|
| `seconds` | do five utterances actually reach thirty seconds? |
| `dbfs` | would the re-established level floor drop it? |
| `verdict` (`True`/`False`/`None`) + `spread` | **the decisive one — see below** |
| `cluster`, `is_anchor_cluster` | did session-wide clustering put it with the corrected turn? |
| `to_own_centroid` | leave-one-out agreement — the ordering harvest already uses |
| `margin_to_nearest_other` | the between-voices check the spec adds per candidate |

And per session: cluster sizes, and cumulative admissible seconds **under the old rule and
under the new rule**.

### The number that decides the design

Of the candidates the new rule admits and the old rule refused, **how many were refused
`None` versus `False`?**

* mostly `None` — the change admits material the guard never had an opinion about, because it
  was too short to have one. That is the claim the spec makes, and it would be confirmed.
* a meaningful share `False` — the change overrides a guard that actively distrusted those
  windows. That is a different proposition and the spec would have to be rewritten again.

Nothing in the spec's argument survives without this split, and it cannot be reasoned out —
`False` and `None` are indistinguishable today because both end in the same refusal.

## Which recordings, and what each one tests

Two sessions the owner has already attested to, which is why they are worth more than volume:

**TEST `Ben_UCPK2`, 2026-08-12 — recorded alone.**
Expected: one cluster, every turn in it.
- a second cluster is a **false split** — clustering inventing a speaker
- a turn refused `False` is the homogeneity guard calling single-speaker audio mixed, which is
  the failure the withdrawn threshold document suspected and never demonstrated
- this session measures the guard's false-refusal rate directly, on audio with no second voice
  in it to find

**PROD `Ben_UCPK2`, 2026-08-11 from 13:25 — Ben and Sam Yu.**
Expected: two clusters.
- a candidate inside the anchor's cluster with a **small `margin_to_nearest_other`** is exactly
  the wrong-speaker shape the spec is afraid of, and this is the only way to see whether it
  occurs at pool sizes that matter
- if the two clusters are clean and the margins are wide, the residual risk the spec declares
  is smaller than it feared

Read-only against PROD: no writes, no artifacts, scalars only. Same access pattern
`measure_frame_spread.py` already uses.

## What it cannot answer

Whether an admitted segment really is that person. There is no ground truth beyond the owner's
attestation of who was in the room, and this measurement does not manufacture one.

What it can do instead is **produce a shortlist to listen to**: the admitted candidates with
the lowest agreement to their own centroid, which is where a wrong-speaker segment would sit
if one got in. Five minutes of listening against a ranked list is a different proposition from
listening to a meeting.

## Steps

1. **`op: "harvest_preview"` in `lambda_speaker_embed.py`** — read-only; reuses `_propagate`'s
   embedding and clustering, computes both rules, returns scalars. No writer invoke, no S3
   write, no vectors in the response. A test asserts the response carries no `embedding` key,
   the same assertion the correction path already carries.
2. **`scripts/preview_harvest.py`** — takes `--user --date --env [--session]`, calls the op per
   session, prints the per-candidate table and the per-session summary, and ends with the
   old-vs-new admission counts split by `None` / `False`.
3. **Run it on the two sessions above.** Needs `aws login`; nothing else is blocked.
4. **Read the split.** If it is overwhelmingly `None`, implement the spec. If not, rewrite the
   spec.
5. **Only then** the change itself, in the order the spec gives, with the caps chosen from the
   duration distribution this run produces rather than guessed.

## What this deliberately does not do

- no storing, no profile creation, no writer invocation on any path;
- no change to `_admit_harvest`, `ENROL_MIN_TURN_S`, or the guard, until step 4 says which;
- no `VOICEPRINT_HARVEST_MODE` switch — if the live path ever needs observing, that is a later
  and separate decision, and this measurement is what would tell us whether it is worth it.
