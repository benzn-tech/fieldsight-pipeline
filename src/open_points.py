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

__all__ = ["has_uncertainty_marker"]

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
