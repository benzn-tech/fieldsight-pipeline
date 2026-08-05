"""Finding the earlier mention of the same subject.

A commitment made on site comes back on a later day. Today the second
recording produces a brand-new topic with no link to the first, so nothing
can notice that a thing has been promised three times and slipped twice.

The shape of this was settled by a read-only probe over prod, and the probe
overturned the obvious design:

  - Matching ACTION ITEMS to each other does not work. Two clearly-related
    topics produced the cross-product of their commitments -- "Check door
    stop availability" scored the same against "Install door handles" as
    against everything else -- because the similarity lives in the SUBJECT
    and not in what was promised about it. Those are different commitments
    and pairing them would be wrong.
  - Matching TOPICS works. ~8 of the top 11 candidates were real.

Two filters earned their place there, and neither is a tuning knob:
  - only topics carrying OPEN work can thread (a thread exists to track
    outstanding commitments; every false candidate in the unfiltered run had
    zero open items on both sides);
  - cap the gap (the surviving false positives matched on generic process
    words across 128-135 days, while real threads sat at 4-21 days).
"""
import pytest

from thread_match import score_pair, find_candidates, MAX_GAP_DAYS


def topic(**over):
    t = {"id": "t1", "report_date": "2026-07-22", "site_id": "s1",
         "title": "Ground floor walls", "summary": "", "open_items": 2}
    t.update(over)
    return t


def corpus(*topics):
    return list(topics)


# ---- the unit is the subject ---------------------------------------------

def test_the_same_subject_restated_scores_high():
    a = topic(id="a", title="Door Hardware Specifications and Installation Requirements")
    b = topic(id="b", report_date="2026-08-01",
              title="Door Hardware Installation Progress")
    assert score_pair(a, b, corpus(a, b)) > 0.3


def test_unrelated_subjects_score_low():
    a = topic(id="a", title="Concrete pour Block 4 south footing")
    b = topic(id="b", report_date="2026-08-01", title="Radio equipment test")
    assert score_pair(a, b, corpus(a, b)) < 0.1


# ---- the filters ----------------------------------------------------------

def test_a_topic_with_no_open_work_cannot_thread():
    """A thread exists to track OUTSTANDING commitments. In the unfiltered
    probe every false candidate had zero open items on both sides -- the
    recording-artifact topics ("Unclear Communication Recording",
    "Unintelligible Audio Segment") match each other perfectly and mean
    nothing."""
    a = topic(id="a", title="Unclear Communication Recording", open_items=0)
    b = topic(id="b", report_date="2026-08-01",
              title="Unclear Communication Recording", open_items=0)
    assert find_candidates(b, corpus(a, b)) == []


def test_one_side_having_open_work_is_not_enough():
    a = topic(id="a", title="Door hardware install", open_items=0)
    b = topic(id="b", report_date="2026-08-01", title="Door hardware install")
    assert find_candidates(b, corpus(a, b)) == []


def test_the_same_day_is_not_a_recurrence():
    """Two topics from one recording are two subjects, not one restated."""
    a = topic(id="a", title="Door hardware install")
    b = topic(id="b", title="Door hardware install")
    assert find_candidates(b, corpus(a, b)) == []


def test_a_different_site_is_a_different_thread():
    a = topic(id="a", site_id="s1", title="Door hardware install")
    b = topic(id="b", site_id="s2", report_date="2026-08-01",
              title="Door hardware install")
    assert find_candidates(b, corpus(a, b)) == []


def test_a_gap_beyond_the_cap_is_refused():
    """The probe's surviving false positives all matched on generic process
    words across 128-135 days. Real threads sat at 4-21."""
    a = topic(id="a", report_date="2026-01-01", title="Documentation walkthrough")
    b = topic(id="b", report_date="2026-07-01", title="Documentation walkthrough")
    assert find_candidates(b, corpus(a, b)) == []

    near = topic(id="c", title="Documentation walkthrough",
                 report_date="2026-07-22")
    later = topic(id="d", title="Documentation walkthrough",
                  report_date=_plus(near["report_date"], MAX_GAP_DAYS - 1))
    assert [c["id"] for c in find_candidates(later, corpus(near, later))] == ["c"]


def _plus(iso, days):
    from datetime import date, timedelta
    return (date.fromisoformat(iso) + timedelta(days=days)).isoformat()


# ---- direction and ordering ----------------------------------------------

def test_only_EARLIER_topics_are_candidates():
    """Threading looks backwards: the new mention joins an existing thread.
    Offering a future topic as the parent would invert cause and effect."""
    earlier = topic(id="e", report_date="2026-07-01", title="Door hardware install")
    later = topic(id="l", report_date="2026-08-01", title="Door hardware install")
    assert [c["id"] for c in find_candidates(earlier, corpus(earlier, later))] == []
    assert [c["id"] for c in find_candidates(later, corpus(earlier, later))] == ["e"]


def test_candidates_come_back_best_first():
    strong = topic(id="strong", report_date="2026-07-01",
                   title="Door Hardware Installation Requirements")
    weak = topic(id="weak", report_date="2026-07-02",
                 title="Hardware delivery schedule")
    new = topic(id="new", report_date="2026-07-10",
                title="Door Hardware Installation Progress")
    got = [c["id"] for c in find_candidates(new, corpus(strong, weak, new))]
    assert got and got[0] == "strong"


# ---- refusing rather than guessing ---------------------------------------

def test_generic_words_alone_do_not_make_a_thread():
    """"Site work update" against "Update on site works" is two generic
    phrases, not a subject. IDF weighting is what keeps this out."""
    a = topic(id="a", title="Site work update")
    b = topic(id="b", report_date="2026-07-25", title="Update on site works")
    filler = [topic(id=f"f{i}", title="Site work update progress review")
              for i in range(12)]
    assert find_candidates(b, corpus(a, b, *filler)) == []


def test_an_empty_or_missing_title_never_threads():
    a = topic(id="a", title="")
    b = topic(id="b", report_date="2026-08-01", title=None)
    assert find_candidates(b, corpus(a, b)) == []
