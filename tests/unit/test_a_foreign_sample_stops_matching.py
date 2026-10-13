"""Unit: a sample that does not sound like its profile's person stops counting.

## The failure, measured on TEST 2026-09-23

A profile was enrolled from a genuine recording of the right person that was full of wind
noise. The stored vector carried no speaker information at all — it sat at **−0.020** from
that person's own clean samples, **0.037** from a second person's and **0.038** from a
third's. Equidistant from everybody is what a vector describing the CONDITION rather than the
voice looks like.

`aggregate_scores` averages a person's samples, so that one vector dragged every score for
them down — mean 0.257 with it removed, 0.192 with it in — and the profile looked healthy
from every angle available: twelve samples, the most of anyone in the company.

Neither existing guard could catch it, and both failures are structural rather than bugs:

* the homogeneity guard asks "does this window hold ONE voice", and wind noise is uniform, so
  every frame agrees with every other. It passed at 0.335 against a 0.35 limit. Consistent
  rubbish is still consistent.
* `add_sample`'s agreement guard refuses a vector closer to ANOTHER profile than its own —
  but at that moment no other profile in the company held a single vector. **A company's
  first enrolments are unprotected by construction.**

Both judge a sample alone, when it arrives. This judges it against the profile it joined, and
can be re-run later — the only way to catch a sample that was the only evidence at the time.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

try:
    # A module from this repository: a missing one is a wrong path or a moved file, and both
    # should be red rather than skipping the file into a green report.
    from repositories import voiceprints as vp
except ImportError as exc:  # pragma: no cover - the message IS the point
    raise AssertionError(f"repositories.voiceprints is not importable: {exc}")

CO = "11111111-1111-1111-1111-111111111111"
VP = "22222222-2222-2222-2222-222222222222"


def cosine(a, b):
    """The fixture's own metric: samples are named points, similarity is a lookup.

    Real 192-dimension vectors would make the intent unreadable and would tie the test to
    whichever arrangement happens to produce the numbers it needs.

    Points are one-element TUPLES rather than strings: `_decode` parses a string embedding as
    a pgvector literal on the way out of the database, so a string here fails inside the
    module under test for a reason that has nothing to do with what is being tested.
    """
    x, y = a[0], b[0]
    return SIM.get((x, y), SIM.get((y, x), 1.0 if x == y else 0.0))


SIM: dict = {}


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def execute(self, sql, params=None):
        self.conn.sql.append((sql, params))
        if sql.lstrip().startswith("SELECT id, embedding"):
            self._rows = [dict(r) for r in self.conn.rows]
        elif "UPDATE speaker_voiceprint_samples" in sql:
            self.conn.quarantined.append(params[3])
        return self

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.sql = []
        self.quarantined = []

    def cursor(self, row_factory=None):
        return FakeCursor(self)


def sample(sid, point, source="correction_propagation", quarantined=None):
    return {"id": sid, "embedding": (point,), "source": source,
            "quarantined_at": quarantined}


def test_the_wind_noise_sample_is_set_aside():
    """The measured case, with the measured numbers: four clean samples that agree with each
    other around 0.6, and two that sit near zero against everything."""
    global SIM
    clean = ["c1", "c2", "c3", "c4"]
    wind = ["w1", "w2"]
    SIM = {}
    for i, a in enumerate(clean):
        for b in clean[i + 1:]:
            SIM[(a, b)] = 0.62
    SIM[("w1", "w2")] = 0.59          # the two wind vectors resemble EACH OTHER
    for a in clean:
        for b in wind:
            SIM[(a, b)] = -0.02       # and nothing else

    rows = [sample(a, a, "correction" if a == "c1" else "correction_propagation")
            for a in clean] + [sample(b, b, "correction") for b in wind]
    conn = FakeConn(rows)
    out = vp.requarantine_profile(conn, CO, VP, cosine=cosine)
    assert out["quarantined"] == 2, out
    assert set(conn.quarantined) == {"w1", "w2"}


def test_human_backing_only_breaks_a_tie():
    """The rule this file was written with, and the reason it was reversed.

    "Trust what a person vouched for" is the obvious ranking and it loses on the measured
    case: the wind-noise enrolment was made by TWO explicit corrections while the four good
    samples beside it were one correction and three propagations. Ranking by human count
    would have kept the noise and set aside the person.

    A person vouching for a window says they recognised the SPEAKER. It says nothing about
    whether the audio carries enough of that speaker to be a sample — a different question
    they were never asked. So human count breaks ties and does not decide them.
    """
    global SIM
    SIM = {}
    bad = ["m1", "m2"]                 # two machine samples that agree with each other
    good = ["h1", "h2"]                # two the user vouched for -- the same size
    for i, a in enumerate(bad):
        for b in bad[i + 1:]:
            SIM[(a, b)] = 0.70
    for i, a in enumerate(good):
        for b in good[i + 1:]:
            SIM[(a, b)] = 0.70
    for a in bad:
        for b in good:
            SIM[(a, b)] = 0.0

    rows = [sample(a, a, "correction_propagation") for a in bad] + \
           [sample(b, b, "correction") for b in good]
    conn = FakeConn(rows)
    out = vp.requarantine_profile(conn, CO, VP, cosine=cosine)
    assert out["core"] == 2, "the two clusters were not the same size — the tie is the test"
    assert set(conn.quarantined) == set(bad), (
        "with the sizes equal, the machine-made cluster won and the human-vouched one was "
        "set aside")


def test_a_profile_whose_samples_mostly_disagree_is_reported_not_halved():
    """Emptying a library because the clustering went wrong would hide the problem behind a
    working-looking profile. An incoherent profile should be visible as incoherent."""
    global SIM
    SIM = {}                                   # everything 0.0 against everything
    rows = [sample(f"s{i}", f"s{i}") for i in range(6)]
    conn = FakeConn(rows)
    out = vp.requarantine_profile(conn, CO, VP, cosine=cosine)
    assert out["quarantined"] == 0
    assert conn.quarantined == []
    assert "cap" in out["reason"], out


def test_two_samples_are_never_judged_against_each_other():
    """Quarantining one of a pair is a coin toss dressed as a judgement: with two samples
    there is no majority to be the core."""
    global SIM
    SIM = {("a", "b"): -0.5}
    conn = FakeConn([sample("a", "a"), sample("b", "b")])
    out = vp.requarantine_profile(conn, CO, VP, cosine=cosine)
    assert out["quarantined"] == 0
    assert "fewer than 3" in out["reason"]


def test_a_coherent_profile_is_left_alone():
    """Guard the guard. A rule that quarantined something every time would pass the tests
    above while quietly shrinking every profile in the system."""
    global SIM
    SIM = {}
    names = [f"s{i}" for i in range(5)]
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            SIM[(a, b)] = 0.66
    conn = FakeConn([sample(a, a) for a in names])
    out = vp.requarantine_profile(conn, CO, VP, cosine=cosine)
    assert out["quarantined"] == 0
    assert conn.quarantined == []
    assert "agree" in out["reason"]


def test_already_quarantined_samples_do_not_re_enter_the_judgement():
    """They are inert by definition, and letting them vote would let a set-aside cluster
    decide which of the live samples is the odd one out."""
    global SIM
    SIM = {}
    live = ["a", "b", "c"]
    for i, x in enumerate(live):
        for y in live[i + 1:]:
            SIM[(x, y)] = 0.7
    rows = [sample(x, x) for x in live] + [sample("old", "old", quarantined="2026-01-01")]
    conn = FakeConn(rows)
    out = vp.requarantine_profile(conn, CO, VP, cosine=cosine)
    assert out["checked"] == 3, "a quarantined row was counted as live"


def test_the_matching_query_excludes_quarantined_samples():
    """The whole point. A sample set aside must stop pulling the person's mean down, and the
    filter belongs in the SQL for the reason the consent filter does: a quarantined vector
    that still matched would keep naming people, correctly as far as anything downstream can
    tell."""
    import re
    src = open(os.path.join(ROOT, "src", "repositories", "voiceprints.py"),
               encoding="utf-8").read()
    body = src[src.index("def profiles_for_matching"):]
    body = body[:body.index("\ndef ", 1)]
    assert re.search(r"quarantined_at IS NULL", body), (
        "profiles_for_matching still scores against quarantined samples")


def test_the_listing_reports_quarantined_separately():
    """'4 samples' and '4 samples, 2 set aside as not sounding like this person' are
    different facts, and a listing that shows only the first hides the reason a profile is
    behaving badly — which is the whole job of that listing."""
    src = open(os.path.join(ROOT, "src", "repositories", "voiceprints.py"),
               encoding="utf-8").read()
    body = src[src.index("def list_profiles"):]
    assert "quarantined" in body, "list_profiles does not report the set-aside count"
