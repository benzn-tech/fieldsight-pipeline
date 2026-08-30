"""open_points.py — a question the meeting left open, and what may be said about it.

A speaker asserts a fact and marks it uncertain: "I think the pile is 150 in 3604,
but I'll have to check." That sentence is not an action item, a finding or a
decision, so the extraction pass -- which keeps about 5% of the transcript's
characters and buckets the rest into those fields -- has no field for it and drops
it. It survives only in the narrative, which is why this runs inside session_brief.

THE GATE IS RULES AND THE FIELDS ARE NOT, and the distinction is load-bearing
rather than stylistic. A marker list that misses yields nothing; a classifier that
misfires yields a confident invention, and a confident invention here is a
fabricated uncertainty attributed to a named person in a record that may be read
back in a dispute. So the marker decides whether an open point may EXIST, the
model fills in what it is about, and `subject` -- the one string permitted to
leave the building later -- is constrained back to the transcript in code.

PURE: no boto3, no psycopg, no network. session_brief imports this at module scope
and must stay pure at import.

Spec: docs/superpowers/specs/2026-08-30-open-questions-pre-resolved-design.md
"""
import re
from datetime import datetime, timedelta

__all__ = ["has_uncertainty_marker", "admit"]

# Deliberately narrow. A marker is a speaker flagging their OWN recall as
# unreliable -- not politeness, not softening, and not a question put to someone
# else ("can you check the stock?" is a task, and the task extractor owns it).
#
# Widening this list is a product decision measured against real briefs, not a
# tidy-up: every term added here admits a class of sentence, and the cost of a
# wrong admission is a fabricated uncertainty with a person's name on it.
_MARKERS = re.compile(
    # English. `I` is required on the recall verbs so that "can you check" and
    # "someone should confirm" -- both tasks, not open points -- do not match.
    r"\bi think\b|\bi believe\b"
    r"|\bi (can'?t|cannot|do ?n'?t) (remember|recall)\b"
    r"|\bi'?m not (quite )?sure\b|\bnot (quite )?sure (whether|if|about|what)\b"
    r"|\bunsure (if|whether|about)\b"
    r"|\bi'?ll (have to |need to )?(check|confirm|look it up|look that up)\b"
    r"|\bi will (have to |need to )?(check|confirm|look it up|look that up)\b"
    r"|\bfrom memory\b|\boff the top of my head\b|\bfrom recollection\b"
    # Chinese. No subject marker is available -- Chinese drops pronouns freely --
    # so the interrogative particles are excluded instead: 吗/呢 turn 查一下 into
    # a request rather than a note-to-self.
    r"|我记得|我觉得|记不清|记不得|不确定|不太确定"
    r"|回头(查|确认)|回去(查|确认)|再确认"
    r"|大概是|应该是|好像是",
    re.IGNORECASE,
)

# 查一下 on its own is ambiguous: a note-to-self when stated, a request when
# asked. The particle is what separates them, and it is cheaper to exclude the
# question than to try to detect the speaker.
_SELF_CHECK = re.compile(r"查一下(?![^。！？]*[吗呢?？])")


def has_uncertainty_marker(text):
    """Does this line carry a speaker flagging their own recall as unreliable?

    Non-strings are False rather than an error: this reads a model-produced
    field, which may be null, a number, or a list on any given run, and a brief
    must not be lost to one malformed candidate.
    """
    if not isinstance(text, str) or not text.strip():
        return False
    if _MARKERS.search(text):
        return True
    return bool(_SELF_CHECK.search(text))


# The same window, floor and threshold the extraction path verifies its
# citations with (lambda_extract_session.py:380-382). Matching them is
# deliberate: two verifiers with different tolerances would call one quote
# verified on one path and unverified on the other, and the difference would
# read as a property of the quote rather than of the caller.
_W_SECONDS = 60.0
_FLOOR_TOKENS = 5
_FUZZY = 0.80

# `weak` is admitted. It means the quote matched but is shorter than the token
# floor, which describes most real uncertainty lines ("I think it's 150") --
# excluding it would reject the feature's central example.
_ACCEPTED_STATUSES = ("verified", "verified_fuzzy", "weak")

_KINDS = ("standard", "supply", "in_corpus", "needs_a_person")
_HHMMSS = re.compile(r"^\s*(\d{1,2}):([0-5]\d):([0-5]\d)\s*$")


def _norm(s):
    """Case-folded, whitespace-collapsed. Used ONLY to compare `subject` against
    its quote: the model normalises capitalisation and spacing, and that is not
    the model inventing a term. Anything beyond this -- stemming, punctuation
    stripping -- would start admitting compositions."""
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _anchor(at_str, turns):
    """The model's bare HH:MM:SS as an absolute datetime, or None.

    Turns carry full datetimes and the model returns a wall-clock time, so the
    DATE has to come from the session. For a session crossing midnight the same
    HH:MM:SS exists on two dates, and resolving it to the wrong one puts the
    quote outside the verification window -- which does not read as a bad
    anchor, it reads as a fabricated quote. So the occurrence NEAREST the
    session's own span wins.

    This mirrors `lambda_extract_session._parse_at` on purpose and does not
    import it: that module pulls boto3, and this one is imported at module scope
    by session_brief, which must stay pure at import. Two copies of a nine-line
    rule is the cheaper of the two costs; if a third appears, move it out.
    """
    m = _HHMMSS.match(at_str or "") if isinstance(at_str, str) else None
    if not m:
        return None
    anchored = [t.get("abs_start") for t in (turns or []) if t.get("abs_start")]
    if not anchored:
        return None
    mid = anchored[len(anchored) // 2]
    h, mi, s = (int(g) for g in m.groups())
    if h > 23:
        return None
    same_day = datetime(mid.year, mid.month, mid.day, h, mi, s, tzinfo=mid.tzinfo)
    return min((same_day, same_day + timedelta(days=1), same_day - timedelta(days=1)),
               key=lambda c: abs((c - mid).total_seconds()))


def admit(candidates, turns, *, check=None):
    """Filter model-produced open points down to the ones that survive.

    Returns `(admitted, stats)` where stats is
    `{"admitted": int, "rejected": {reason: count}}`. Rejections are COUNTED and
    never raised: this runs inside the brief, which is what the confirmation
    email and the website stand on, and losing it to one malformed candidate is
    a far worse trade than losing one open point.

    The stats are returned even when nothing was rejected, because the caller
    logs them and "it ran and rejected nothing" must not look like "it never
    ran".

    `check` is injectable so a test can drive the branches without the
    verifier's tuning; the default imports `evidence_match` lazily, which keeps
    THIS module importable with no dependencies at all.
    """
    if check is None:
        import evidence_match

        def check(quote, at):
            return evidence_match.check_quote(
                quote, turns, at, w_seconds=_W_SECONDS,
                floor_tokens=_FLOOR_TOKENS, fuzzy_threshold=_FUZZY)

    admitted, rejected = [], {}

    def reject(reason):
        rejected[reason] = rejected.get(reason, 0) + 1

    for c in candidates or []:
        if not isinstance(c, dict):
            reject("malformed")
            continue
        quote = c.get("quote")
        subject = c.get("subject")
        if not (isinstance(quote, str) and quote.strip()
                and isinstance(subject, str) and subject.strip()):
            reject("malformed")
            continue

        # 1. The gate, and it is on the QUOTE rather than on the model's
        #    paraphrase of it. A model deciding that a flat statement was
        #    uncertain does not get to say so.
        if not has_uncertainty_marker(quote):
            reject("no_marker")
            continue

        # 2. `subject` is the only string permitted to leave the building later,
        #    so it must be something the speaker actually said -- section 6 of
        #    the spec: a free composition is a free exfiltration.
        if _norm(subject) not in _norm(quote):
            reject("subject_not_in_quote")
            continue

        at = _anchor(c.get("at"), turns)
        if at is None:
            reject("bad_anchor")
            continue

        # 3. The quote was actually said. NOTHING on the brief path checks this
        #    today: `_snap_to_quote` re-anchors a timestamp and leaves unmatched
        #    quotes in the brief, so an open point built on an invented sentence
        #    would be indistinguishable from a real one -- and it would carry a
        #    speaker's name into a record that gets read back in a dispute.
        try:
            status = (check(quote, at) or {}).get("status")
        except Exception:
            reject("verifier_error")
            continue
        if status not in _ACCEPTED_STATUSES:
            reject("quote_unverified")
            continue

        kind = c.get("kind")
        admitted.append({
            "quote": quote,
            "at": c.get("at"),
            "claim": c.get("claim") if isinstance(c.get("claim"), str) else "",
            # An unknown kind falls to the one that promises nothing. Routing a
            # made-up kind to a resolver would answer a question nobody asked,
            # confidently.
            "kind": kind if kind in _KINDS else "needs_a_person",
            "subject": subject,
        })

    return admitted, {"admitted": len(admitted), "rejected": rejected}
