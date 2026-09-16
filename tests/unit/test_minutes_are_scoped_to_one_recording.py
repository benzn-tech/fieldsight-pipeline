"""Unit: one recording session is one set of minutes.

The owner settled the boundary: a meeting starts when recording starts and ends
when it stops. The generator does not implement that -- it collects every
transcript for a DATE, so two meetings on one day are merged into a single
document with one title, one attendee list and one set of decisions, and there
is no way to tell from the output that it happened.

That mattered less while nothing invoked this function at all (no `Events:`
block, no caller in the repo, zero prod invocations in 30 days, and a docstring
claiming API Gateway and EventBridge triggers it). Wiring it to session close is
the next step and it cannot be done until the function can be asked about one
session.

Session ids are already in the filenames -- `..._sid<32 hex>_c0008_...`. So the
scoping is a key filter, which is why the matching is pinned as an exact token
below: `sid` followed by the id followed by `_`. A substring match would make
one session's minutes silently include another's chunks whenever one id is a
prefix of the other, and the ids are generated on the device.

MEASURED while writing this, and it killed the other half of the plan: the
criteria proposed for deciding which recordings deserve minutes were "speakers
>= 2 and segments <= 20". `results.audio_segments` is ABSENT from 30 of 30 real
prod chunks across two sessions -- it is an AWS Transcribe field and the pipeline
no longer runs on Transcribe. The segment criterion is not strict or lenient, it
is uncomputable. Speaker labels do exist (`spk_0..spk_3`), but they are a
per-chunk namespace rather than a headcount, so they are not used here either.
The boundary the owner gave needs no such criterion, which is why this file
tests scoping and nothing about "is it a meeting".
"""
import os

import pytest

os.environ.setdefault("S3_BUCKET", "b")
os.environ.setdefault("ANTHROPIC_API_KEY", "k")

mm = pytest.importorskip(
    "lambda_meeting_minutes",
    reason="requires the meeting minutes lambda's dependencies (installed in CI)")

SID = "d740cd5fcaea4140bace19039dcc8649"
OTHER = "6237afecc49003fd264d66cf9db607d9"

# Verbatim shapes from prod (transcripts/Ben_Lin/2026-09-11/).
def _key(sid, chunk="c0008", date="2026-09-11"):
    return ("transcripts/Ben_Lin/%s/ben_lin_%s_13-44-26_sid%s_%s"
            "_bn4_off0.0_to114.0_srcwav.json" % (date, date, sid, chunk))


ALL_KEYS = [
    _key(SID, "c0000"), _key(SID, "c0003"),
    _key(OTHER, "c0101"), _key(OTHER, "c0105"),
    # A prefix collision, which is the reason the match is a token and not a
    # substring. Device-generated ids are not guaranteed to differ early.
    _key(SID[:8], "c0001"),
    # Not a transcript at all.
    "transcripts/Ben_Lin/2026-09-11/notes.txt",
]


@pytest.fixture
def s3ed(monkeypatch):
    """Every listed key downloads as the same minimal, valid transcript."""
    seen = []
    monkeypatch.setattr(mm, "list_s3_objects",
                        lambda bucket, prefix: [{"key": k} for k in ALL_KEYS
                                                if k.startswith(prefix)])
    monkeypatch.setattr(mm, "load_user_mapping", lambda bucket: {})

    def _dl(bucket, key):
        seen.append(key)
        return {"results": {"transcripts": [{"transcript": "the slab pour finished"}],
                            "items": []}}

    monkeypatch.setattr(mm, "download_json_from_s3", _dl)
    # The agent-turn filter reads the voice-ask sidecar from S3 on every
    # transcript. Stubbed, not mocked at the boto layer: this file is about
    # which KEYS are collected, and a test that also exercises the sidecar would
    # go red for reasons that have nothing to do with scoping.
    monkeypatch.setattr(mm.agent_turn_filter, "apply_agent_filter",
                        lambda turns, *a, **k: (turns, None))
    return seen


def test_without_a_session_id_nothing_changes(s3ed):
    """Every existing caller passes no session. A function that silently
    narrowed would turn today's whole-day minutes into one arbitrary session's,
    and the output would look perfectly normal."""
    mm.collect_transcripts("b", "2026-09-11", user_filter="Ben_Lin")
    assert len([k for k in s3ed if k.endswith(".json")]) == 5


def test_a_session_id_keeps_only_that_session(s3ed):
    mm.collect_transcripts("b", "2026-09-11", user_filter="Ben_Lin",
                           session_id=SID)
    assert sorted(s3ed) == sorted([_key(SID, "c0000"), _key(SID, "c0003")])


def test_the_other_session_is_not_in_it(s3ed):
    """THE test. Two meetings in one day is the ordinary case the date-scoped
    version merges, and a merged document is wrong in a way nobody can see: one
    title, one attendee list, decisions from two rooms."""
    mm.collect_transcripts("b", "2026-09-11", user_filter="Ben_Lin",
                           session_id=SID)
    assert not any(OTHER in k for k in s3ed)


def test_a_shorter_id_is_not_a_prefix_match(s3ed):
    """`sid` + id + `_`, never a substring. Device-generated ids are not
    guaranteed to differ in their first bytes, and a substring match would put
    one session's chunks in another's minutes with no error anywhere."""
    mm.collect_transcripts("b", "2026-09-11", user_filter="Ben_Lin",
                           session_id=SID[:8])
    assert s3ed == [_key(SID[:8], "c0001")]


def test_an_unknown_session_collects_nothing_rather_than_everything(s3ed):
    """Fails CLOSED. An id that matches no chunk must produce no minutes, not a
    document about the whole day -- the caller can tell "nothing to report" from
    "here is someone else's meeting" only if this returns empty."""
    out = mm.collect_transcripts("b", "2026-09-11", user_filter="Ben_Lin",
                                 session_id="0" * 32)
    assert out == []
    assert s3ed == []


def test_a_blank_session_id_means_no_scoping(s3ed):
    """An empty parameter is an absent one. A client that computed no session
    must get the existing behaviour, not an empty document."""
    for bad in ("", "   ", None):
        del s3ed[:]
        mm.collect_transcripts("b", "2026-09-11", user_filter="Ben_Lin",
                               session_id=bad)
        assert len([k for k in s3ed if k.endswith(".json")]) == 5, bad


@pytest.mark.parametrize("hostile", [
    "../../other-user", "a/b", "*", SID + "/..", "sid" + SID,
])
def test_a_session_id_cannot_widen_the_scope(s3ed, hostile):
    """It is a filter over keys the ACL already produced, never a path. A value
    containing a separator or a wildcard must match nothing -- narrowing is the
    only thing this parameter is allowed to do."""
    out = mm.collect_transcripts("b", "2026-09-11", user_filter="Ben_Lin",
                                 session_id=hostile)
    assert out == []
    assert s3ed == []


def test_scoping_composes_with_the_date_not_instead_of_it(s3ed):
    """A session id is not a licence to read another day. The date filter still
    runs, so an id that exists under a different date collects nothing."""
    mm.collect_transcripts("b", "2026-09-10", user_filter="Ben_Lin",
                           session_id=SID)
    assert s3ed == []


# ── the output key, which scoping made load-bearing ─────────────────────────
#
# Scoping collection without changing the key would make things WORSE, not
# better: the key is `meeting_minutes/<date>/<stem>.json` and the default title
# is `Meeting - <date>` for every session, so the second meeting of a day
# overwrote the first. Both documents look complete, so the loss is invisible
# until someone goes looking for a decision that was minuted and is now gone.


def test_two_sessions_on_one_day_do_not_share_a_filename():
    """THE overwrite test."""
    a = mm._output_stem("Meeting - 2026-09-11", SID)
    b = mm._output_stem("Meeting - 2026-09-11", OTHER)
    assert a != b


def test_without_a_session_the_stem_is_unchanged():
    """The whole-day caller keeps the key it has always written. A changed key
    would orphan every existing document rather than fix anything."""
    assert mm._output_stem("Meeting - 2026-09-11") == "Meeting_-_2026-09-11"


def test_the_stem_stays_a_filename():
    """It goes straight into an S3 key. A title is user-supplied and a session
    id arrives over HTTP, so neither may contribute a separator."""
    stem = mm._output_stem("../../etc/passwd", "a/b/../c")
    assert "/" not in stem
    assert ".." not in stem


def test_a_named_meeting_is_still_recognisable():
    """A listing is read by people. The id is a suffix, not a replacement."""
    stem = mm._output_stem("Tuesday Site Walk", SID)
    assert stem.startswith("Tuesday_Site_Walk")
    assert SID[:8] in stem


def test_the_same_session_always_lands_on_the_same_key():
    """Idempotent, so a re-run replaces its own document rather than
    accumulating near-duplicates nobody can choose between."""
    assert (mm._output_stem("Meeting - 2026-09-11", SID)
            == mm._output_stem("Meeting - 2026-09-11", SID))


def test_a_malformed_session_id_does_not_reach_the_key():
    """Collection already refuses one of these outright. If the stem accepted it
    anyway, a failed run would still write a file named after a hostile value."""
    stem = mm._output_stem("Meeting", "../../x")
    assert stem == "Meeting"
