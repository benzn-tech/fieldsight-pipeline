"""Whether a turn gets a name, and whether a window may be enrolled.

Spec:     docs/superpowers/specs/2026-08-09-speaker-identity-v2.md
Findings: docs/superpowers/specs/2026-08-11-speaker-phase0-results.md
Plan:     docs/superpowers/plans/2026-08-11-speaker-identity-implementation.md (phase 1)

Pure numpy. No torch, no speechbrain, no onnxruntime — not even lazily, because every
Lambda imports this module and none of them should pay for a model runtime to decide a
comparison. The embedder lives behind its own boundary; this is only the arithmetic.

Phase 0 attributed 31 of 32 turns correctly with two people five metres away. The single
miss is what this module is shaped around:

    session A, turn 13 — Zoe, 2.1 s, "Don't know. Maybe the facade subbie."
    Zoe +0.104   Ben +0.111   → named Ben

Two rules follow, and both are refusals rather than improvements:

1. **A duration floor.** That turn is also the lowest same-person score in the entire set.
   Short turns are not weak evidence, they are unusable evidence, so below the floor no
   name is offered at all — whatever the scores say.
2. **A margin, never an absolute cut.** Same-person scores ran 0.104…0.639 and
   different-person −0.114…0.205: the distributions *overlap*. The best absolute threshold
   measured, +0.262 for 99%, was fitted on the material that produced it and is an upper
   bound, not a setting. So the decision is nearest-profile plus a required gap to the
   runner-up, and anything closer than that gap is offered as a guess rather than an answer.

Everything ambiguous degrades to `tentative` or `unknown`. A wrong confident name costs
much more than a missing one (v2 §1): the missing one is visibly missing.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# The floor the Phase 0 miss put here. 2.1 s failed; 3.0 s is the nearest round number
# above it, and deliberately not tuned finer than the evidence supports.
DEFAULT_MIN_TURN_S = 3.0

# How far the nearest profile must beat the runner-up. NOT the fitted +0.262 — that number
# is an absolute cut measured on its own data, and shipping it would be shipping an
# overfit. This is a gap between two scores of the same turn, which is a different and
# much more stable quantity, and it is still provisional until held-out material exists.
DEFAULT_MIN_MARGIN = 0.15

# How far apart two frames of one window may be before the window is treated as holding
# more than one voice (v2 §6). Cosine distance, i.e. 1 - similarity.
DEFAULT_MAX_FRAME_SPREAD = 0.35


@dataclass
class Decision:
    """`confirmed` names the speaker; `tentative` shows a guess as a guess; `unknown`
    offers nothing. `name` is filled for tentative too — withholding the lean entirely
    helps nobody, and the viewer renders it as unconfirmed (v2 §1)."""
    status: str
    name: str | None
    margin: float | None
    reason: str


def cosine(a, b) -> float:
    """Cosine similarity. Loudness-invariant on purpose: across 0–6 m the level moves by
    ~20 dB, and a score that moved with it would be measuring the microphone rather than
    the speaker. A zero vector scores 0.0 rather than raising — an empty window is a thing
    that happens, and it is not an error worth failing an extraction over."""
    a = np.asarray(a, dtype=np.float64).ravel()
    b = np.asarray(b, dtype=np.float64).ravel()
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def decide_name(scores, duration_s: float,
                min_turn_s: float = DEFAULT_MIN_TURN_S,
                min_margin: float = DEFAULT_MIN_MARGIN) -> Decision:
    """Who this turn belongs to, or an honest refusal.

    `scores` maps a profile name to its similarity with this turn. The order of the checks
    is the point: duration first, so that no score — however emphatic — can name a turn too
    short to carry the evidence.
    """
    if duration_s is None or duration_s < min_turn_s:
        return Decision("unknown", None, None,
                        f"too short to attribute ({duration_s}s < {min_turn_s}s); the one "
                        f"Phase 0 miss was a 2.1s turn and it scored its own speaker lowest")
    if not scores:
        return Decision("unknown", None, None, "no enrolled profiles to compare against")

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best_name, best = ranked[0]
    if len(ranked) == 1:
        # Nothing to be better than. Confirming here would be confirming on an absolute
        # score, which is exactly what the overlapping distributions forbid.
        return Decision("tentative", best_name, None,
                        "only one enrolled profile, so there is no runner-up to beat")

    margin = best - ranked[1][1]
    if margin >= min_margin:
        return Decision("confirmed", best_name, margin,
                        f"clear of the runner-up by {margin:.3f}")
    return Decision("tentative", best_name, margin,
                    f"only {margin:.3f} clear of {ranked[1][0]}; below the {min_margin} "
                    f"margin this is a lean, not an identification")


def window_is_homogeneous(frame_embeddings,
                          max_spread: float = DEFAULT_MAX_FRAME_SPREAD):
    """Does this window hold one voice? True / False / None for "cannot tell".

    The enrolment contamination guard (v2 §6). A window that holds two people poisons the
    profile with someone else's voice, and a poisoned profile cannot be cleaned — only the
    whole contributing sample deleted, which is why each contribution is stored separately
    in the first place.

    None rather than True for zero or one frame: a single frame is trivially consistent
    with itself, and treating that as evidence would let a one-frame window enrol
    unchecked. "I could not check" and "I checked and it is fine" must not be the same
    answer — that conflation is how a guard becomes decoration.
    """
    frames = [np.asarray(f, dtype=np.float64).ravel() for f in (frame_embeddings or [])]
    if len(frames) < 2:
        return None
    for i in range(len(frames)):
        for j in range(i + 1, len(frames)):
            if 1.0 - cosine(frames[i], frames[j]) > max_spread:
                return False
    return True
