# Gate A — does clustering separate the speakers? Measured

**Date:** 2026-08-13
**Gate:** `../plans/2026-08-13-correction-propagation-implementation.md` § Gate A
**Reproduce:** `scripts/speaker_session_eval.py --cluster --onnx <model>` against the two
Phase 0 sessions and `scripts/fixtures/2026-08-11-blockv-scripts.json`

## Verdict: it separates them. τ = 0.85.

The kill criterion was *"if no merge threshold separates three speakers whose ground truth is
known, the mechanism does not ship."* One does, in both sessions, at the same value.

| τ window giving k = 3 at 100% purity | |
|---|---|
| session `318601b2` (Ben / Mike / Zoe) | 0.82 – 0.88 |
| session `b49627d7` (Ben / Joe / Leo) | 0.81 – 0.93+ |
| **intersection** | **0.82 – 0.88** |

**Frozen at τ = 0.85** — the middle of the intersection, 0.03 of margin either way. Below 0.82
a speaker splits in two; above 0.88 two speakers merge in the first session.

Measured with **onnxruntime on the exported model** — the engine the Lambda runs — not with
speechbrain. A threshold frozen on one engine and applied by the other is a threshold nobody
checked.

## The result worth stating on its own

In session `b49627d7` the transcription provider returned **two speaker labels for three
people**. Clustering the same audio by voiceprint recovered **all three, at 100% purity**.

That is the mechanism doing the thing it was designed for, on real site audio, against ground
truth — not an argument that it should work.

## The distribution nobody had measured

Phase 0 measured turn-vs-**profile**. Clustering runs on turn-vs-**turn**, which is a
different and much noisier quantity:

| cosine distance | same speaker | different speaker |
|---|---|---|
| session 1 | 0.245 / **0.538** / 0.813 | 0.747 / 0.897 / 1.039 |
| session 2 | 0.255 / **0.495** / 0.807 | 0.687 / 0.919 / 1.114 |

(min / median / max)

**The bands overlap** — by 0.066 and 0.120. Clustering works anyway, and the reason matters:
`max(same) < min(diff)` is *sufficient* for a threshold to exist, not *necessary*. Complete
linkage does not need every same-speaker pair closer than every different-speaker pair, only a
merge order that never crosses people. My first pass printed "separable at all: NO" from the
sufficient condition and would have failed the gate on a mechanism that works.

## What this measurement kills: the second threshold

The plan required a homogeneity bound τ′ **tighter than τ**, so the check is a real second
opinion rather than a tautology (at complete linkage, "all pairs within τ" holds by
construction).

**There is no room for it.** Same-speaker pairs reach 0.813, and τ is 0.85 — any τ′ tight
enough to be meaningful fires on legitimate same-speaker clusters. The gap between "as far
apart as one person's turns actually get" and "the merge threshold" is 0.037, which is noise.

So the homogeneity check on clusters is **dropped**, not tuned. Keeping it would have meant
shipping a guard that either never fires or refuses real speakers, and the plan's `False` →
cap-at-tentative rule would have been dead code. The review predicted the check would be
decoration; the data says there is nowhere to stand.

## Limits, stated because the numbers look better than the evidence

* **n = 16 labelled turns per session, two sessions.** Same day, same room, same device, same
  three-metre-ish geometry.
* One of the three speakers is the wearer (device on his chest); the other two stood side by
  side at 5 m. A different geometry is not covered.
* Same-speaker median distance is ~0.5 — high for ECAPA, and a fair description of what short
  turns at 5 m look like. τ = 0.85 is tuned to *that*, and a closer, quieter meeting may want
  a different value. The per-row `cluster_threshold` audit column exists for exactly this.
* 100% purity at k = 3 with 0 singletons means perfect separation **of the turns that got a
  ground-truth label** — turns under the duration floor were never embedded and are not in
  this measurement.

## Consequences for the plan

1. Gate A passes. Propagation may be built.
2. τ = 0.85, frozen, recorded per row.
3. τ′ and the cluster homogeneity check are removed from the plan.
4. The "kill criterion" step is closed. What remains open is the **margin** for step 2, which
   this measurement does not supply and was never expected to.
