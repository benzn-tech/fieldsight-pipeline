"""Raw transcripts carry speech that is hidden only at topic level: a redacted topic
or one marked non-work. The topic list hides it; the transcript does not. If that
speech reaches the prompt it reaches the customer's report (spec 2026-09-15 §5.3)."""
import datetime as dt

import pytest

import transcript_window as tw


DATE = "2026-09-10"


def _turn(h, m, text="something"):
    return {"at": dt.datetime(2026, 9, 10, h, m), "line": "[%02d:%02d:00] Ben: %s" % (h, m, text)}


def test_a_redacted_topics_minutes_are_cut_out():
    spans = tw.excluded_spans(DATE, [{"id": "t1", "time_range": "10:00 - 10:20"}])
    turns = [_turn(9, 50), _turn(10, 5, "the private part"), _turn(10, 30)]
    kept = tw.drop_spans(turns, spans)
    assert [t["at"].hour * 60 + t["at"].minute for t in kept] == [9 * 60 + 50, 10 * 60 + 30]
    assert not any("private" in t["line"] for t in kept)


def test_a_turn_on_the_boundary_is_cut_not_kept():
    spans = tw.excluded_spans(DATE, [{"id": "t1", "time_range": "10:00 - 10:20"}])
    assert tw.drop_spans([_turn(10, 0), _turn(10, 20)], spans) == []


def test_an_excluded_topic_with_no_usable_time_fails_closed():
    with pytest.raises(tw.UnplaceableExclusion):
        tw.excluded_spans(DATE, [{"id": "t9", "time_range": ""}])
    with pytest.raises(tw.UnplaceableExclusion):
        tw.excluded_spans(DATE, [{"id": "t9", "time_range": "all morning"}])


def test_no_exclusions_keeps_everything():
    assert tw.drop_spans([_turn(9, 0), _turn(10, 0)], []) == [_turn(9, 0), _turn(10, 0)]


def test_a_turn_straddling_the_start_of_a_span_is_dropped():
    """`drop_spans` used to check only a turn's `at`, so a turn that STARTS before
    the excluded span but runs INTO it survived whole -- a sliver of hidden speech
    reached the prompt. It must be dropped on overlap, not on start alone."""
    spans = tw.excluded_spans(DATE, [{"id": "t1", "time_range": "10:00 - 10:20"}])
    straddler = {"at": dt.datetime(2026, 9, 10, 9, 58),
                "until": dt.datetime(2026, 9, 10, 10, 5),
                "line": "[09:58:00] Ben: the private part starts mid-turn"}
    before = _turn(9, 50)
    kept = tw.drop_spans([before, straddler, _turn(10, 30)], spans)
    assert straddler not in kept
    assert before in kept
