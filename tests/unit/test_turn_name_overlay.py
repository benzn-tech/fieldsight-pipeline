"""Unit: resolving stored turn names onto the turns a reader is actually holding.

The names are an overlay, not text baked into the transcript artifact — because a correction
can be withdrawn, because a derived document may have exactly one writer, and because
re-running extraction rewrites the artifact.

That last reason is also this module's hard problem. A row's `turn_ref` is
`source_filename + start_sec`, and the live/final two-layer extraction re-assembles turns: a
seam dedup shifts `start_sec` by a fraction of a second. Under a strict join the row then
matches nothing and **the name silently disappears** — which is the same class of failure the
overlay was chosen to avoid. So the join is by proximity with a tolerance, and anything that
matches nothing is *counted and reported* rather than dropped.
"""
import pytest

tno = pytest.importorskip("turn_name_overlay")


def _row(ref, name, state="confirmed", source="correction_propagation", created="2026-08-13T01:00:00"):
    return {"turn_ref": ref, "display_name": name, "state": state, "source": source,
            "created_at": created, "cluster_ref": "C1"}


# ---- matching -----------------------------------------------------------


def test_an_exact_reference_names_the_turn():
    idx = tno.build([_row("x.wav@12.5", "Ben L")])
    got = tno.lookup(idx, "x.wav", 12.5)
    assert got["display_name"] == "Ben L"


def test_a_turn_that_moved_slightly_keeps_its_name():
    """The whole reason this is not a dictionary lookup. Re-extraction shifted the turn by
    0.2 s; a strict join would drop the name and nothing would say why."""
    idx = tno.build([_row("x.wav@12.5", "Ben L")])
    assert tno.lookup(idx, "x.wav", 12.7)["display_name"] == "Ben L"


def test_a_turn_that_moved_too_far_is_not_named():
    """Tolerance is not 'nearest wins'. A second away is a different turn, and inheriting a
    name across it would be a wrong confident name — the failure this whole layer avoids."""
    idx = tno.build([_row("x.wav@12.5", "Ben L")])
    assert tno.lookup(idx, "x.wav", 20.0) is None


def test_a_row_never_belongs_to_another_file():
    idx = tno.build([_row("x.wav@12.5", "Ben L")])
    assert tno.lookup(idx, "y.wav", 12.5) is None


def test_the_nearest_row_wins_when_two_are_within_tolerance():
    idx = tno.build([_row("x.wav@12.4", "Ben L"), _row("x.wav@12.9", "Zoe")])
    assert tno.lookup(idx, "x.wav", 12.85)["display_name"] == "Zoe"


# ---- precedence ---------------------------------------------------------


def test_a_direct_correction_beats_a_propagated_name_for_the_same_turn():
    """The user asserted one of these and the machine inferred the other.

    The propagated row is deliberately the LATER of the two, and listed second. With equal
    timestamps this test passed even with the source ranking deleted — insertion order alone
    happened to pick the right row, so it was asserting nothing. Mutation found it.
    """
    idx = tno.build([
        _row("x.wav@12.5", "Ben L", source="correction", created="2026-08-13T01:00:00"),
        _row("x.wav@12.5", "Machine Guess", source="correction_propagation",
             created="2026-08-13T09:00:00"),
    ])
    assert tno.lookup(idx, "x.wav", 12.5)["display_name"] == "Ben L"


def test_between_two_corrections_the_later_one_wins():
    idx = tno.build([
        _row("x.wav@12.5", "First", source="correction", created="2026-08-13T01:00:00"),
        _row("x.wav@12.5", "Second", source="correction", created="2026-08-13T02:00:00"),
    ])
    assert tno.lookup(idx, "x.wav", 12.5)["display_name"] == "Second"


def test_precedence_is_applied_at_read_not_only_by_the_index():
    """The database's partial unique index guarantees one live row per turn_ref STRING. With
    a tolerance join two rows whose strings differ slightly both match one physical turn, so
    the index cannot be the guarantee and the resolver has to decide again."""
    idx = tno.build([
        _row("x.wav@12.5", "Propagated", source="correction_propagation"),
        _row("x.wav@12.6", "Asserted", source="correction"),
    ])
    assert tno.lookup(idx, "x.wav", 12.55)["display_name"] == "Asserted"


# ---- what may reach a reader -------------------------------------------


def test_an_unnamed_cluster_produces_no_name_at_all():
    """`decide_name` returns the winning CLUSTER KEY as its name, and an unnamed cluster is a
    real answer — 'someone consistent, not identified'. `C_3` must never be rendered."""
    idx = tno.build([_row("x.wav@12.5", None, state="confirmed")])
    assert tno.lookup(idx, "x.wav", 12.5) is None, (
        "an unnamed cluster produced a result; `or not got['display_name']` used to make "
        "this pass with the guard deleted, because the row comes back carrying None")


def test_a_state_always_travels_with_a_name():
    """A caller that receives a bare name cannot tell tentative from confirmed, and tentative
    must not leave the transcript viewer."""
    idx = tno.build([_row("x.wav@12.5", "Ben L", state="tentative")])
    got = tno.lookup(idx, "x.wav", 12.5)
    assert got["state"] == "tentative"


def test_confirmed_only_filters_out_everything_unproven():
    """The boundary for minutes, email and the action-item responsible party. The email is
    the artifact that leaves the building."""
    rows = [_row("x.wav@1.0", "Ben L", state="confirmed"),
            _row("x.wav@9.0", "Maybe", state="tentative")]
    idx = tno.build(rows, confirmed_only=True)
    assert tno.lookup(idx, "x.wav", 1.0)["display_name"] == "Ben L"
    assert tno.lookup(idx, "x.wav", 9.0) is None


# ---- orphans ------------------------------------------------------------


def test_rows_that_match_nothing_are_counted_not_dropped():
    """An orphan means a name the user set is no longer being shown. Silence there reads as
    'this turn was never named', which is a different and wrong statement."""
    idx = tno.build([_row("x.wav@12.5", "Ben L"), _row("gone.wav@3.0", "Zoe")])
    tno.lookup(idx, "x.wav", 12.5)
    assert tno.orphans(idx) == 1


def test_an_orphan_stops_being_one_once_it_matches():
    idx = tno.build([_row("x.wav@12.5", "Ben L")])
    assert tno.orphans(idx) == 1
    tno.lookup(idx, "x.wav", 12.5)
    assert tno.orphans(idx) == 0


def test_a_malformed_reference_is_an_orphan_rather_than_an_exception():
    """One bad row must not take the whole transcript down with it."""
    idx = tno.build([_row("no-at-sign", "Ben L")])
    assert tno.lookup(idx, "x.wav", 1.0) is None
    assert tno.orphans(idx) == 1


def test_no_rows_at_all_is_cheap_and_quiet():
    idx = tno.build([])
    assert tno.lookup(idx, "x.wav", 1.0) is None
    assert tno.orphans(idx) == 0
