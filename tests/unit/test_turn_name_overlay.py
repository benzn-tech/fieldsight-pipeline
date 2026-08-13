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
    """Both orderings and a query point far from the tie, because the first version of this
    test asked at 12.85 — right next to the row that happened to sort last — and passed while
    the implementation returned the FARTHER row for every other query point.

    Equal `created_at` is the normal case, not an edge one: Postgres `now()` is constant
    within a transaction, so one writer run stamps every row it writes identically.
    """
    for rows in ([_row("x.wav@12.4", "Ben L"), _row("x.wav@12.9", "Zoe")],
                 [_row("x.wav@12.9", "Zoe"), _row("x.wav@12.4", "Ben L")]):
        idx = tno.build(rows)
        assert tno.lookup(idx, "x.wav", 12.45)["display_name"] == "Ben L", (
            "the farther row won; distance is what identifies which turn a row is about")
        idx = tno.build(rows)
        assert tno.lookup(idx, "x.wav", 12.85)["display_name"] == "Zoe"


def test_distance_outranks_recency_but_not_the_source():
    """A newer row at a different offset is probably about a different TURN, so recency must
    not drag a name across turns. But a direct correction still beats a nearer inference."""
    old_near = _row("x.wav@12.4", "Near", created="2026-08-13T01:00:00")
    new_far = _row("x.wav@12.9", "Far", created="2026-08-13T09:00:00")
    assert tno.lookup(tno.build([old_near, new_far]), "x.wav", 12.45)["display_name"] == "Near"

    guess_near = _row("x.wav@12.4", "Guess", source="correction_propagation")
    said_far = _row("x.wav@12.8", "Said", source="correction")
    assert tno.lookup(tno.build([guess_near, said_far]),
                      "x.wav", 12.45)["display_name"] == "Said"


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


# ---- the two keys the halves must agree on ------------------------------
#
# Both of these were wrong in the shipped version, and both were invisible because the
# reader's fixture and the writer's fixture were written by the same hand on the same day.
# The strings below are copied VERBATIM from TEST, so they cannot agree with an assumption.

REAL_TRANSCRIPT = ("ben_ucpk2_2026-08-13_11-49-00_sid9db9293e82b94a4d9611572b1233f82d"
                   "_c0000_bn4_off0.0_to114.0_srcwav.json")
REAL_AUDIO = REAL_TRANSCRIPT[:-5] + ".wav"
REAL_SESSION = "ben_ucpk2_2026-08-13_11-49-00_sid9db9293e82b94a4d9611572b1233f82d"


def test_the_session_is_derived_from_a_real_transcript_filename():
    """The overlay looked rows up by the SEGMENT FILENAME, while every row is stored under
    the session id. Two different keys, so the query returned nothing, every time, silently
    — `unmatchedNames: 0` because there were no rows to orphan.

    This test originally asserted the whole prefix up to `_sid…`, which encoded a contract
    that turned out to be wrong: that prefix holds the CHUNK's start time, so the chunks of
    one recording keyed differently. The session is the sid.
    """
    assert tno.session_base(REAL_TRANSCRIPT) == tno.session_base(REAL_SESSION)
    assert tno.session_base(REAL_TRANSCRIPT) in REAL_SESSION


def test_a_per_chunk_filename_yields_the_same_session():
    plain = ("ben_ucpk2_2026-08-13_11-49-00_sid9db9293e82b94a4d9611572b1233f82d"
             "_c0000.wav")
    assert tno.session_base(plain) == tno.session_base(REAL_TRANSCRIPT)


def test_a_filename_with_no_session_id_yields_nothing_rather_than_a_guess():
    assert tno.session_base("Benl1_2026-03-20_12-18-34.json") is None


def test_the_audio_and_the_transcript_name_the_same_turn():
    """A correction carries the .wav; the transcript endpoint holds the .json. Matching on
    the raw string means the name never appears and nothing says why."""
    idx = tno.build([_row(f"{REAL_AUDIO}@35.0", "Ben L")])
    assert tno.lookup(idx, REAL_TRANSCRIPT, 35.0)["display_name"] == "Ben L"
    assert tno.lookup(idx, REAL_AUDIO, 35.0)["display_name"] == "Ben L"


# ---- what makes two files the same session -----------------------------
#
# The prefix before `_sid` carries the CHUNK's own start time, not the session's. So the
# per-chunk files of one recording — …_11-49-00_sid9db…_c0000, …_11-49-32_sid9db…_c0001 —
# produced DIFFERENT session keys, and a session of eleven chunks looked like eleven
# sessions of one turn each. Propagation then had nothing to cluster and correctly did
# nothing, which read as "the feature does not work".
#
# Two files are the same session iff their sid matches. Nothing else in the name is stable.

SID = "sid9db9293e82b94a4d9611572b1233f82d"
CHUNK_A = f"ben_ucpk2_2026-08-13_11-49-00_{SID}_c0000_bn4_off0.0_to114.0_srcwav.json"
CHUNK_B = f"ben_ucpk2_2026-08-13_11-49-32_{SID}_c0001.wav"


def test_two_chunks_of_one_recording_are_one_session():
    assert tno.session_base(CHUNK_A) == tno.session_base(CHUNK_B)


def test_the_session_key_is_the_sid():
    assert SID[3:] in tno.session_base(CHUNK_A)


def test_a_different_recording_is_a_different_session():
    other = "ben_ucpk2_2026-08-13_18-10-00_sida2d5c7ee7fa843959481f88040259d05_c0000.wav"
    assert tno.session_base(other) != tno.session_base(CHUNK_A)


def test_the_correction_path_parameter_resolves_to_the_same_session():
    """org-api receives the session in the URL as `{device}_{time}_sid{hex}` — the form the
    device reports. It has to land on the same key as the filenames do."""
    assert tno.session_base(f"ben_ucpk2_2026-08-13_11-49-00_{SID}") == tno.session_base(CHUNK_A)


# ---- one row describes one turn ----------------------------------------
#
# Per-segment lookup let ONE row be claimed by SEVERAL segments. On TEST that turned a
# single assertion into two confirmed names: the user marked the turn at 2.86 and the turn
# at 2.58 — a different speaker saying "Go." — came back confirmed too, because it sits
# within tolerance.
#
# A wrong CONFIRMED name is the failure this whole layer is shaped around, and it arrived
# from the direction nobody was watching: not a bad score, but a good row claimed twice.
# The offsets below are the real ones from that session.


def _seg(name, at):
    return {"source_filename": name, "chunk_start": at}


def test_a_row_names_the_nearest_turn_and_only_that_one():
    rows = [_row("x.wav@2.86", "Ben L", source="correction")]
    out = tno.resolve(tno.build(rows), [_seg("x.json", 2.58), _seg("x.json", 2.86)])
    assert out.get(1, {}).get("display_name") == "Ben L"
    assert 0 not in out, "the assertion bled onto a neighbouring turn"


def test_two_rows_claim_two_turns_not_the_same_one():
    rows = [_row("x.wav@50.26", "Ben L"), _row("x.wav@50.28", "Zoe")]
    out = tno.resolve(tno.build(rows),
                      [_seg("x.json", 50.26), _seg("x.json", 50.28)])
    assert out[0]["display_name"] == "Ben L"
    assert out[1]["display_name"] == "Zoe"


def test_the_closer_pairing_wins_when_two_rows_compete_for_one_turn():
    """Greedy by distance over every (row, turn) pair, so the tightest pairing is made first
    and the loser looks elsewhere rather than being dropped."""
    rows = [_row("x.wav@10.0", "Far"), _row("x.wav@10.4", "Near")]
    out = tno.resolve(tno.build(rows), [_seg("x.json", 10.45), _seg("x.json", 10.05)])
    assert out[0]["display_name"] == "Near"
    assert out[1]["display_name"] == "Far"


def test_a_row_with_no_turn_within_tolerance_is_still_an_orphan():
    idx = tno.build([_row("x.wav@2.0", "Ben L"), _row("x.wav@99.0", "Zoe")])
    out = tno.resolve(idx, [_seg("x.json", 2.0)])
    assert out[0]["display_name"] == "Ben L"
    assert tno.orphans(idx) == 1


def test_resolve_keeps_precedence_when_both_rows_reach_one_turn():
    rows = [_row("x.wav@5.0", "Guess", source="correction_propagation"),
            _row("x.wav@5.0", "Said", source="correction")]
    out = tno.resolve(tno.build(rows), [_seg("x.json", 5.0)])
    assert out[0]["display_name"] == "Said"


# ---- one function builds a turn reference -------------------------------
#
# The embedder built refs with two f-strings while the reader normalised with _stem, so a
# correction carrying `…_srcwav.wav` and a turn list carrying `…_srcwav.json` described the
# same turn and never compared equal. Propagation refused every real correction with
# "corrected turn not among the session's turns" — the fourth appearance tonight of two
# spellings of one thing.


def test_a_turn_reference_is_the_same_whichever_artifact_you_hold():
    assert tno.turn_ref("x_c0000_srcwav.wav", 2.86) == \
           tno.turn_ref("x_c0000_srcwav.json", 2.86)


def test_a_built_reference_parses_back_to_what_built_it():
    ref = tno.turn_ref("x_c0000_srcwav.wav", 2.86)
    idx = tno.build([_row(ref, "Ben L")])
    assert tno.lookup(idx, "x_c0000_srcwav.json", 2.86)["display_name"] == "Ben L"


def test_the_offset_survives_as_a_number_not_a_rendering():
    """2.0 and 2 must not be two different turns."""
    assert tno.turn_ref("x.wav", 2) == tno.turn_ref("x.wav", 2.0)


def test_nothing_in_production_asks_for_one_turn_at_a_time():
    """`lookup` is kept for exercising the ranking rules and is deliberately not the path
    the endpoint takes. Asking it per segment lets ONE row be claimed by SEVERAL turns, and
    on TEST that turned a single assertion into two `confirmed` names.

    A dead public entry point that reintroduces a killed defect is worth a guard rather than
    a comment — nothing else would notice a future caller.
    """
    import ast
    import os
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2] / "src"
    offenders = []
    for path in root.rglob("*.py"):
        if "__pycache__" in str(path) or path.name == "turn_name_overlay.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        # Every name this module is bound to in this file, not just its own. A guard that
        # only recognises one spelling is the night's own recurring defect wearing a test's
        # clothes: `import turn_name_overlay as tno` walked straight through the first
        # version of this.
        aliases = {"turn_name_overlay"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == "turn_name_overlay":
                        aliases.add(a.asname or a.name)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "lookup"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in aliases):
                offenders.append(os.path.relpath(path, root))
    assert not offenders, (
        f"{offenders} calls turn_name_overlay.lookup per segment; use resolve(), which pairs "
        f"rows and turns one-to-one")


# A guard asserting "only one place knows the session-id pattern" was written here and then
# removed. Four unrelated modules legitimately match `_sid[0-9a-f]{32}_c(\d+)` to pull a
# CHUNK index or a batch number out of a filename — they are not second copies of the session
# key, and a guard that demands unrelated rewrites is noise rather than protection.
# `lambda_org_api._session_of`, which really was a second copy, now delegates.
