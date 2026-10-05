"""Which of the company's checklist templates a spoken check is.

"Starting the pre-pour concrete check" has to find the company's "Pre-pour
Inspection Checklist" -- and "level two steel installation inspection" its
"Reinforcing Steel" one -- without the model ever seeing the company's
templates (extraction has no database). So the model names the check and its
kind, and this matches words, in code, the same way every time.

The words that say "this is a check" (inspection, check, checklist...) and the
place (level 2, room...) are left out: they are in every name, so they would
make every template match every check. What is left is the TYPE -- "pre pour",
"steel", "fire stopping" -- and that is what is compared. A word counts when it
is the same, or the same with one or two letters wrong ("pre-poor").

Pure: no database, so the rules are tested to the word.
"""
import difflib
import re

_WORD = re.compile(r"[a-z0-9]+")
GENERIC = frozenset((
    "inspection", "inspections", "check", "checks", "checklist", "checklists", "checking",
    "the", "a", "an", "of", "on", "for", "and", "to", "at", "in", "my", "our", "this", "that",
    "level", "levels", "floor", "room", "area", "zone", "block", "stage", "site", "here", "is",
    "starting", "start", "doing", "now", "new", "form", "sheet", "record", "report", "daily",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "ground",
))
MIN_SCORE = 0.5


def words(text):
    """The words that say WHAT KIND of check it is."""
    return [w for w in _WORD.findall((text or "").lower())
            if w not in GENERIC and not w.isdigit()]


def _same(a, b):
    if a == b:
        return True
    if min(len(a), len(b)) < 4:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def score(spoken, template_words):
    """Share of the spoken check's type-words found among the template's."""
    if not spoken or not template_words:
        return 0.0
    hit = sum(1 for w in spoken if any(_same(w, t) for t in template_words))
    return hit / len(spoken)


def is_checklist(body):
    return any(str(s.get("kind") or "").strip().lower() == "checklist"
               for s in (body or {}).get("sections") or [] if isinstance(s, dict))


def template_words(template):
    """A template is known by its name and its checklist sections' titles."""
    body = template.get("body") or {}
    titles = [s.get("title") or "" for s in body.get("sections") or []
              if isinstance(s, dict) and str(s.get("kind") or "").lower() == "checklist"]
    return words(" ".join([template.get("name") or ""] + titles))


def match(inspection, templates):
    """(template, score) for the best checklist template, or (None, best score).

    The KIND is what is matched ("pre-pour"); the name ("level one pre-pour
    inspections") only when the model gave no kind. Ties go to the template
    whose words the check covers more of (the more specific one), then to the
    org's over a personal one, then by name -- the same answer every run."""
    spoken = words(inspection.get("kind")) or words(inspection.get("name"))
    best, best_key = None, None
    top = 0.0
    for t in templates:
        if not is_checklist(t.get("body")):
            continue
        tw = template_words(t)
        s = score(spoken, tw)
        top = max(top, s)
        if s < MIN_SCORE:
            continue
        cover = score(tw, spoken)
        key = (-s, -cover, 0 if t.get("scope") == "org" else 1, t.get("name") or "")
        if best_key is None or key < best_key:
            best, best_key = t, key
    return (best, -best_key[0]) if best is not None else (None, top)
