"""Resolve stored speaker names onto the turns a reader is holding.

The names live in `speaker_turn_names` and are applied at read time. They are not baked into
the transcript artifact, for three reasons that have each already cost something here:

  * a correction can be withdrawn, and withdrawal has to reach everything the correction
    justified — enumerable only if the names are rows;
  * a derived document may have exactly one writer (`programme.json` records what happens
    when it gets a second);
  * re-running extraction rewrites the artifact, and an overlay survives that.

The third reason is also this module's hard problem. A row's `turn_ref` is
`source_filename + start_sec`, and the live/final two-layer extraction re-assembles turns —
a seam dedup shifts `start_sec` by a fraction of a second. Under a strict join the row then
matches nothing and **the name silently disappears**, which is the same class of failure the
overlay was chosen to avoid. So:

  * the join is by proximity within `TOLERANCE_SEC`, nearest wins;
  * a row that never matches anything is **counted**, and the caller reports the count.

Precedence has to be applied HERE and not left to the database. The partial unique index
guarantees one live row per `turn_ref` string; with a tolerance join, two rows whose strings
differ slightly can both match one physical turn, so the index is a backstop rather than the
guarantee.

Spec: docs/superpowers/specs/2026-08-13-speaker-correction-propagation.md
Plan: docs/superpowers/plans/2026-08-13-correction-propagation-implementation.md (P6)
"""

# Wide enough to survive a seam dedup, far short of a turn. Phase 0's shortest usable turn is
# 3 s and the duration floor is 3 s, so half a second cannot reach a neighbouring turn.
import re

TOLERANCE_SEC = 0.5

# A direct correction is a claim a person made; propagation and matching are inferences.
_SOURCE_RANK = {"correction": 2, "correction_propagation": 1, "match": 0}


def _stem(name):
    """A turn is named by its RECORDING, not by which artifact you happen to be holding.

    A correction carries the audio (`…_srcwav.wav`); the transcript endpoint holds the
    transcript (`…_srcwav.json`). Matching on the raw string means the name never appears and
    nothing anywhere says why — which is exactly what shipped, and what a real correction on
    TEST exposed.
    """
    n = str(name or "")
    for ext in (".json", ".wav", ".mp4", ".m4a"):
        if n.endswith(ext):
            return n[: -len(ext)]
    return n


def session_base(filename):
    """The session a turn belongs to: everything up to and including `_sid{32hex}`.

    Rows are stored under the session id, and the overlay was looking them up by the segment
    FILENAME — two different keys, so the query returned nothing every time, and returned it
    quietly: no rows means no orphans, so `unmatchedNames` read 0 and the failure looked like
    "this session was never corrected".

    None rather than a guess when there is no session id: a wrong session key reads another
    session's names onto this one, which is worse than no names.
    """
    m = re.match(r"^(.*_sid[0-9a-f]{32})", str(filename or ""))
    return m.group(1) if m else None


def _parse_ref(ref):
    """`{source_filename}@{start_sec}` → (filename, seconds), or None.

    Returns None rather than raising: one malformed row must not take a whole transcript
    down with it, and an unparseable row is exactly an orphan — a name that cannot be shown.
    """
    if not ref or "@" not in str(ref):
        return None
    name, _, tail = str(ref).rpartition("@")
    try:
        return _stem(name), float(tail)
    except ValueError:
        return None


def build(rows, confirmed_only=False):
    """An index over live rows, carrying its own orphan bookkeeping.

    `confirmed_only` is the v2 §1 boundary: only confirmed names may reach minutes, email and
    the action-item responsible party. Tentative names exist for the transcript viewer alone,
    and the email is the artifact that leaves the building — so the filter is a build-time
    argument rather than something each caller remembers to apply.
    """
    by_file = {}
    orphaned = set()
    for i, r in enumerate(rows or []):
        if confirmed_only and r.get("state") != "confirmed":
            continue
        parsed = _parse_ref(r.get("turn_ref"))
        if parsed is None:
            orphaned.add(i)
            continue
        fname, at = parsed
        by_file.setdefault(fname, []).append((at, i, r))
        orphaned.add(i)
    for entries in by_file.values():
        entries.sort(key=lambda e: e[0])
    return {"by_file": by_file, "unmatched": orphaned}


def _rank(row, distance):
    """A total order over the rows that could describe one turn. Higher sorts first.

    The order matters more than it looks, and an earlier version got it wrong in a way that
    tests agreed with: `_better` returned the incumbent on a tie, and `created_at` ties are
    the NORMAL case, because Postgres `now()` is constant within a transaction and one
    writer run stamps every row it writes identically. Querying 12.45 against rows at 12.4
    and 12.9 returned the row at 12.9.

    1. **Source.** A direct correction is a claim a person made; propagation and matching are
       inferences.
    2. **Distance.** Distance is what identifies WHICH TURN a row is about. It has to outrank
       recency: two rows at different offsets are probably about different turns, and the
       newer one being about a different turn is no reason to prefer it here.
    3. **Recency.** Only ever a tie-break between rows about the same turn — a re-correction.
    """
    return (_SOURCE_RANK.get(row.get("source"), -1),
            -float(distance),
            str(row.get("created_at") or ""))


def lookup(index, source_filename, start_sec):
    """The name for this turn, or None.

    Every row within tolerance is considered, not merely the nearest: the nearest may be a
    propagated guess sitting beside the correction the user actually made.
    """
    entries = index["by_file"].get(_stem(source_filename))
    if not entries:
        return None
    candidates = [(at, i, r) for at, i, r in entries
                  if abs(at - float(start_sec)) <= TOLERANCE_SEC]
    if not candidates:
        return None
    at, i, row = max(candidates, key=lambda c: _rank(c[2], abs(c[0] - float(start_sec))))
    index["unmatched"].discard(i)
    if not row.get("display_name"):
        # An unnamed cluster is a real answer — "someone consistent, not identified" — and
        # `decide_name` hands back the CLUSTER KEY as its name. `C_3` must never render.
        return None
    return {"display_name": row["display_name"], "state": row.get("state"),
            "source": row.get("source"), "cluster_ref": row.get("cluster_ref")}


def orphans(index):
    """Rows that matched no turn.

    Reported rather than dropped: an orphan is a name the user set that is no longer being
    shown, and silence there reads as "this turn was never named" — a different and wrong
    statement.
    """
    return len(index["unmatched"])
