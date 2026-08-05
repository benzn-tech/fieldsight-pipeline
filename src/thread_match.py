"""thread_match.py — find the earlier mention of the same subject.

A commitment made on site is restated on later days: "Sub A said the ground
floor walls finish Wednesday" comes back on Wednesday, done or slipped. Each
recording currently produces a brand-new topic with no link to the earlier
one, so nothing can notice that a thing has been promised three times.

Design notes that came out of measuring prod rather than reasoning about it
(docs/superpowers/specs/2026-08-05-recurring-item-threading-design.md):

  - **The unit is the SUBJECT, not the commitment.** Matching action items to
    each other produced the cross-product of two related topics -- "Check
    door stop availability" scored identically against "Install door handles"
    and against everything else -- because the similarity lives in the
    subject while the promises differ every time. Topic-level matching found
    ~8 real threads in its top 11 with nothing but the scorer below.
  - **Only topics carrying open work can thread.** A thread exists to track
    outstanding commitments. This is not a threshold hack: in the unfiltered
    probe EVERY false candidate had zero open items on both sides, because
    the extractor's recording-artifact topics ("Unclear Communication
    Recording", "Unintelligible Audio Segment") match each other perfectly
    and mean nothing.
  - **Cap the gap.** The surviving false positives matched on generic process
    words (`documentation`, `installation`, `floor`) across 128-135 days,
    while real threads sat at 4-21.

This is deliberately lexical. It runs inside the in-VPC writer, which cannot
make outbound calls (CLAUDE.md BUG-36), and pure string maths over rows
already in the database needs none. It produces CANDIDATES for confirmation,
never a committed link -- a wrong link silently closes or escalates the wrong
work, so the first join into a thread is a human's call. If precision needs
to be better, the upgrade path is the existing non-VPC
`lambda_programme_matcher` pattern (embedding shortlist, then one Claude call
that must pick from the shortlist or pick none).
"""
import math
import re
from datetime import date

# Real threads in the prod sample sat at 4-21 days. Beyond this the matches
# were generic process words, not the same job. Nearly free: on the full prod
# corpus this cap costs 2 links out of 11 and both were false (128 and 135
# days apart, matched on the word "documentation").
MAX_GAP_DAYS = 45

# Below this a pair is two phrases that share vocabulary, not one subject.
#
# Measured on the full prod corpus (140 topics, 88 with open work): this
# yields 9 candidate links at roughly 4-in-5 precision. Loosening it is the
# WRONG lever for recall -- 0.20 gives 15 links and 0.15 gives 44, and the
# probe showed the extra ones are exactly the generic-process-word matches
# ("documentation", "installation", "floor") that are not the same job.
# Recall comes from better similarity, not a lower bar: the upgrade is the
# non-VPC embedding shortlist the programme matcher already runs.
MIN_SCORE = 0.25

# The subject lives in the title; the summary only corroborates.
TITLE_WEIGHT = 0.7

_WORD = re.compile(r"[^a-z0-9]+")

# Words that are about the SHAPE of site talk rather than its subject. Left
# out of scoring entirely, because IDF alone cannot suppress them in a corpus
# this small -- a word appearing in 8 of 140 topics still looks rare.
_STOP = frozenset("""
the a an and or of for to in on at with from by is are be this that these those
it its as new all any each other more not no
check confirm ensure provide review discuss update follow arrange complete
completed finish site team work works job meeting general comment segment
""".split())


def _tokens(text):
    if not text:
        return set()
    return {w for w in _WORD.split(str(text).lower())
            if len(w) > 3 and w not in _STOP}


def _idf_map(corpus):
    """Document frequency over the whole corpus, so a word every topic uses
    contributes nothing. Computed per call rather than cached: the corpus is
    one site's topics, and it changes on every write."""
    df = {}
    for t in corpus:
        for w in _tokens(t.get("title")) | _tokens(t.get("summary")):
            df[w] = df.get(w, 0) + 1
    n = max(len(corpus), 1)
    return df, n


def _cosine(a, b, df, n):
    if not a or not b:
        return 0.0
    def w(x):
        # Smoothed IDF (the sklearn form), never zero. Plain log(n/df) hands
        # a weight of exactly 0 to any word present in every document, which
        # on a small corpus is most of them -- three topics about doors made
        # "door", "hardware" and "installation" all worthless and the cosine
        # collapsed to 0/0. Rarity should still rank, but no word that two
        # topics genuinely share should count for nothing. Suppressing the
        # vocabulary of site talk is _STOP's job, not IDF's.
        return 1.0 + math.log((1.0 + n) / (1.0 + df.get(x, 0)))
    num = sum(w(x) ** 2 for x in a if x in b)
    da = math.sqrt(sum(w(x) ** 2 for x in a))
    db = math.sqrt(sum(w(x) ** 2 for x in b))
    return (num / (da * db)) if da and db else 0.0


def score_pair(a, b, corpus):
    """Similarity of two topics as SUBJECTS, in [0, 1]. Pure — applies none
    of the eligibility rules, so it can be inspected on any pair."""
    df, n = _idf_map(corpus)
    title = _cosine(_tokens(a.get("title")), _tokens(b.get("title")), df, n)
    summary = _cosine(_tokens(a.get("summary")), _tokens(b.get("summary")), df, n)
    return TITLE_WEIGHT * title + (1 - TITLE_WEIGHT) * summary


def _as_date(v):
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def find_candidates(topic, corpus, min_score=MIN_SCORE, max_gap_days=MAX_GAP_DAYS):
    """Earlier topics that may be the same subject as `topic`, best first.

    Returns candidates for a human to confirm — never a link. Empty is the
    correct and common answer: most topics are new subjects, and inventing a
    parent for them is the failure mode this whole design is built to avoid.
    """
    when = _as_date(topic.get("report_date"))
    if when is None or not (topic.get("open_items") or 0):
        return []
    if not _tokens(topic.get("title")):
        return []

    df, n = _idf_map(corpus)
    mine = _tokens(topic.get("title"))
    mine_sum = _tokens(topic.get("summary"))

    out = []
    for other in corpus:
        if other is topic or other.get("id") == topic.get("id"):
            continue
        if not (other.get("open_items") or 0):
            continue
        if other.get("site_id") != topic.get("site_id"):
            continue
        theirs = _tokens(other.get("title"))
        if not theirs:
            continue
        then = _as_date(other.get("report_date"))
        if then is None:
            continue
        gap = (when - then).days
        # Strictly earlier: threading looks backwards, and two topics from one
        # day are two subjects rather than one restated.
        if gap <= 0 or gap > max_gap_days:
            continue
        score = (TITLE_WEIGHT * _cosine(mine, theirs, df, n)
                 + (1 - TITLE_WEIGHT) * _cosine(mine_sum, _tokens(other.get("summary")), df, n))
        if score >= min_score:
            out.append((score, gap, other))

    # Best score first; nearer in time breaks a tie, because the most recent
    # mention of a subject is the one still in play.
    out.sort(key=lambda r: (-r[0], r[1]))
    return [dict(o, match_score=round(s, 4), gap_days=g) for s, g, o in out]
