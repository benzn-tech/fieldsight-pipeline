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


def aggregate_scores(rows) -> dict:
    """One score per PERSON, from one row per SAMPLE.

    `profiles_for_matching` JOINs `speaker_voiceprint_samples`, so a person with three
    enrolments arrives as three rows. `decide_name` compares a flat mapping and cannot see
    that two of its entries are the same voice, so ungrouped rows make a person their own
    runner-up: Phase 0's Ben holds an English and a Chinese profile ~0.08 apart, which is
    below the 0.15 margin, so he would be reported `tentative` against himself.

    That is also why Phase 0's 31 of 32 is a NEAREST-PROFILE figure and not what this rule
    confirms. Aggregation has to happen before the margin means anything.

    **Max, not mean.** Max is what "nearest profile" already did implicitly, and a mean
    would dilute a genuinely matching sample against a weak one — the enrolment that fits
    this turn is the evidence, and averaging it with an unrelated one throws that away.

    `person_key` is supplied by the caller and must be an identity, never a display name:
    two people share a first name in this data already.
    """
    out: dict = {}
    for row in rows or []:
        key = row["person_key"]
        score = float(row["score"])
        if key not in out or score > out[key]:
            out[key] = score
    return out


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


# --- clustering a session's own turns -------------------------------------
#
# The propagation mechanism's unit is a VOICE, not a turn. Two spec revisions died on the
# same failure — scoring each turn against the corrected window gives `decide_name` a single
# candidate, and the single-candidate branch above refuses to confirm on principle. Adding
# the session's clusters as rivals did not help either, because the corrected person's OWN
# cluster was among them and he competed with himself at margin ~0.
#
# Clustering first and letting the correction NAME a cluster removes the failure structurally:
# the reference selects a cluster rather than standing beside it, and per-turn confidence
# becomes an ordinary multi-candidate question between genuinely different voices.
#
# tau = 0.85, frozen 2026-08-13 from the two Phase 0 sessions (Gate A): [0.82, 0.88] gives
# k = 3 at 100% purity in both, and 0.85 is the middle. Measured with THIS code — a threshold
# measured against a different implementation is not a threshold for this one.

DEFAULT_CLUSTER_TAU = 0.85


def cluster_turns(vectors, tau=DEFAULT_CLUSTER_TAU) -> list:
    """Group a session's turn embeddings into voices. Returns one label per vector.

    Complete linkage on cosine distance: a merge happens only when EVERY pair across the two
    clusters is within `tau`. Single linkage would chain A→B→C and put two people who sound
    nothing alike into one cluster — for naming that is the expensive direction, because the
    runner-up that would have caught the mistake disappears along with them.

    Pure numpy, like everything else here: this ships to a Lambda whose layer carries no
    scipy and no sklearn, and an implementation that cannot run there would not be the one
    the threshold was measured against.

    O(n^3) in the worst case, which is fine for the hundreds of turns a session holds and
    would not be for thousands.
    """
    n = len(vectors)
    if n == 0:
        return []
    dist = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            dist[i][j] = dist[j][i] = 1.0 - cosine(vectors[i], vectors[j])
    groups = [[i] for i in range(n)]
    while len(groups) > 1:
        best = None
        for a in range(len(groups)):
            for b in range(a + 1, len(groups)):
                worst = max(dist[x][y] for x in groups[a] for y in groups[b])
                if best is None or worst < best[0]:
                    best = (worst, a, b)
        if best is None or best[0] > tau:
            break
        _, a, b = best
        groups[a] = groups[a] + groups[b]
        del groups[b]
    labels = [0] * n
    for k, g in enumerate(groups):
        for i in g:
            labels[i] = k
    return labels


def leave_one_out_centroid(members, index):
    """The cluster's centre with `index` taken out of it.

    A turn is part of the centroid it is scored against, which inflates its own similarity —
    and most in the smallest clusters, where a two-member cluster scores its members highly
    even when they are barely alike. Margins would then be largest exactly where the evidence
    is weakest, and the calibration step would freeze that cluster-size dependence into the
    threshold it produces.

    Raises at n=1 rather than returning a zero vector: zero scores 0.0 against everything and
    reads as "nobody", when the truth is "cannot say".
    """
    import numpy as np
    if len(members) < 2:
        raise ValueError(
            "a singleton cluster has no leave-one-out centroid — it names only the turn the "
            "user corrected and propagates nothing")
    arr = np.asarray(members, dtype=np.float64)
    total = arr.sum(axis=0) - arr[index]
    return total / (len(members) - 1)


# `assign(reference, centroids)` lived here until 2026-08-14 and is deleted rather than kept.
# It scored a reference against every centroid, which is precisely the self-comparison
# `_propagate` stopped doing when it began reading the corrected turn's cluster LABEL — so
# it had no caller, and its docstring went on describing one ("the caller refuses below
# DEFAULT_MIN_MARGIN") that no longer existed. Dead code that documents an imaginary caller
# is the same failure this module keeps producing, one level up.
