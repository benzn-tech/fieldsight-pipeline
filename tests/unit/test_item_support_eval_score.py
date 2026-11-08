"""Tests for scripts/item_support_eval (Track C Task 9): one test group per pre-registered bar,
pinned at its boundary with tiny hand-built fixtures, plus the glue that turns real run files
into those inputs. No AWS, no DB, no labeller. All text here is synthetic.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import item_support

from scripts.item_support_eval import build, label, run, score, store

P, F, I, R = score.PASS, score.FAIL, score.INSUFFICIENT, score.REPORTED


def _ok(n):
    return [{"correct": True}] * n


# =========================================================================================
# Clopper-Pearson
# =========================================================================================

def test_cp_upper_zero_errors_matches_closed_form():
    for n in (1, 20, 58, 59, 300):
        assert score.cp_upper(0, n) == pytest.approx(1 - 0.05 ** (1 / n), abs=1e-9)


def test_min_links_is_the_smallest_n_that_can_pass():
    assert score.cp_upper(0, score.MIN_LINKS - 1) > 0.05
    assert score.cp_upper(0, score.MIN_LINKS) <= 0.05


def test_cp_upper_one_error_large_n_does_not_overflow():
    ub = score.cp_upper(1, 2000)
    assert 0.0005 < ub < 0.003


# =========================================================================================
# Bar 1: link precision
# =========================================================================================

def test_bar1_58_of_58_is_insufficient():
    assert score.score_link_precision(_ok(58))["verdict"] == I


def test_bar1_59_of_59_passes():
    out = score.score_link_precision(_ok(59))
    assert out["verdict"] == P and out["cp_upper"] <= 0.05


def test_bar1_one_error_in_59_fails():
    out = score.score_link_precision(_ok(58) + [{"correct": False}])
    assert out["verdict"] == F and out["wrong"] == 1


def test_bar1_missing_gold_is_insufficient_even_with_enough_links():
    assert score.score_link_precision(_ok(100), missing_gold=1)["verdict"] == I


def _turn(text, speaker="spk_0", i=0):
    return {"text": text, "speaker": speaker, "start_sec": float(i), "end_sec": i + 1.0,
            "source_filename": "seg1.json",
            "abs_start": datetime(2026, 10, 5, 9, 0) + timedelta(seconds=i)}


def test_link_correct_exact_and_several():
    turns = [_turn("pour the slab", i=0), _turn("pour the slab today", i=1)]
    assert score.is_link_correct(0, {"label": "turn", "turns": [0]}, turns, "pour slab")
    assert score.is_link_correct(1, {"label": "several", "turns": [0, 1]}, turns, "pour slab")


def test_link_to_none_gold_is_wrong():
    turns = [_turn("pour the slab")]
    assert not score.is_link_correct(0, {"label": "none", "turns": []}, turns, "pour slab")


def test_adjacent_same_speaker_same_words_counts_as_correct():
    turns = [_turn("pour the slab level two", i=0), _turn("so pour the slab level two", i=1)]
    gold = {"label": "turn", "turns": [0]}
    assert score.is_link_correct(1, gold, turns, "pour slab level two")


def test_adjacent_other_speaker_is_wrong():
    turns = [_turn("pour the slab", "spk_0", 0), _turn("pour the slab", "spk_1", 1)]
    assert not score.is_link_correct(1, {"label": "turn", "turns": [0]}, turns, "pour slab")


def test_adjacent_missing_some_of_the_gold_turns_words_is_wrong():
    turns = [_turn("pour the slab level two", i=0), _turn("pour it", i=1)]
    assert not score.is_link_correct(1, {"label": "turn", "turns": [0]}, turns,
                                     "pour slab level two")


def test_two_away_is_wrong():
    turns = [_turn("pour slab", i=0), _turn("x", i=1), _turn("pour slab", i=2)]
    assert not score.is_link_correct(2, {"label": "turn", "turns": [0]}, turns, "pour slab")


# =========================================================================================
# Bar 2: twin hard negatives (mechanical check + owner confirmation)
# =========================================================================================

@pytest.mark.parametrize("item,turn,expected", [
    ("unit 3 pre-line done", "unit 4 pre-line is done", ["unit"]),
    ("unit 3 pre-line done", "unit 3 pre-line is done", []),
    ("Level 2 slab", "floor 3 slab", ["level"]),
    ("level 12th done", "level 12 done", []),
    ("east wall cracked", "the west wall is cracked", ["direction"]),
    ("东侧墙裂缝", "西侧墙裂缝", ["direction"]),
    ("3楼漏水", "4层漏水", ["level"]),
    ("三楼漏水", "3楼漏水", []),
    ("十二楼", "12楼", []),
    ("二十三栋", "23区", []),
    ("三号楼", "四号楼", ["number"]),
    ("两层", "三层", ["level"]),
    ("Block C formwork", "Block D formwork", ["block"]),
    ("check the unit I said", "check the unit A", []),
    ("pour the slab", "pour the slab", []),
    ("unit 3 pre-line", "pour the slab", []),
])
def test_twin_flags(item, turn, expected):
    assert score.twin_flags(item, turn) == expected


def test_cn_to_int():
    assert [score.cn_to_int(s) for s in ("3", "三", "十", "十二", "二十", "二十三", "两")] == \
        [3, 3, 10, 12, 20, 23, 2]
    assert score.cn_to_int("十十") is None


def test_bar2_confirmed_flag_fails():
    flags = [{"flag_id": "a", "confirmed": True}, {"flag_id": "b", "confirmed": False}]
    assert score.score_twins(flags)["verdict"] == F


def test_bar2_unanswered_flag_is_insufficient():
    assert score.score_twins([{"flag_id": "a", "confirmed": None}])["verdict"] == I


def test_bar2_no_flags_or_all_rejected_passes():
    assert score.score_twins([])["verdict"] == P
    assert score.score_twins([{"flag_id": "a", "confirmed": False}])["verdict"] == P


# =========================================================================================
# Bar 3: run-to-run
# =========================================================================================

def test_bar3_identical_digests_pass_and_different_fail():
    assert score.score_run_to_run("ab", "ab")["verdict"] == P
    assert score.score_run_to_run("ab", "ac")["verdict"] == F
    assert score.score_run_to_run(None, "ab")["verdict"] == I


def test_bar3_run_digest_is_byte_level(tmp_path):
    rows = [{"item_ref": "r:0:findings:0", "status": "linked", "turn": 1}]
    a = tmp_path / "item_support_eval" / "runs" / "a"
    b = tmp_path / "item_support_eval" / "runs" / "b"
    run.write_point_run(a, rows, {"floor": 0.5})
    run.write_point_run(b, rows, {"floor": 0.5})
    assert run.run_digest(a) == run.run_digest(b)
    run.write_point_run(b, rows, {"floor": 0.55})
    assert run.run_digest(a) != run.run_digest(b)
    assert b"\r\n" not in (a / "rows.jsonl").read_bytes()


# =========================================================================================
# Bar 4: agent-owner agreement
# =========================================================================================

def _g(label, turns=()):
    return {"label": label, "turns": list(turns)}


def test_bar4_85_percent_passes_just_below_fails():
    same = [(_g("turn", [1]), _g("turn", [1]))]
    diff = [(_g("turn", [1]), _g("none"))]
    assert score.score_agreement(same * 17 + diff * 3)["verdict"] == P      # 0.85
    assert score.score_agreement(same * 16 + diff * 4)["verdict"] == F      # 0.80


def test_bar4_several_sets_must_match_exactly():
    pairs = [(_g("several", [1, 2]), _g("several", [1, 3]))]
    assert score.score_agreement(pairs)["agreement"] == 0.0


def test_bar4_empty_or_incomplete_sample_is_insufficient():
    assert score.score_agreement([])["verdict"] == I
    assert score.score_agreement([(_g("none"), _g("none"))], missing=1)["verdict"] == I


# =========================================================================================
# Bar 5: inferred precision
# =========================================================================================

def test_bar5_under_20_is_reported_only():
    assert score.score_inferred([{"supported": True}] * 19)["verdict"] == R


def test_bar5_needs_29_clean_items_to_pass():
    assert score.score_inferred([{"supported": False}] * 29)["verdict"] == P
    assert score.score_inferred([{"supported": False}] * 28)["verdict"] == F
    assert score.score_inferred([{"supported": False}] * 20)["verdict"] == F


def test_bar5_one_supported_needs_more_items():
    rows = [{"supported": True}] + [{"supported": False}] * 60
    assert score.score_inferred(rows)["verdict"] == P
    rows = [{"supported": True}] + [{"supported": False}] * 30
    assert score.score_inferred(rows)["verdict"] == F


def test_bar5_reported_does_not_enter_the_overall_verdict():
    assert score.overall_verdict([P, P, P, P, R, P], n_sessions=40) == P


# =========================================================================================
# Bar 6: per path
# =========================================================================================

def test_bar6_failing_path_is_disabled_and_the_other_ships():
    links = [{"path": "quote", "correct": True}] * 59 + \
            [{"path": "window", "correct": True}] * 58 + [{"path": "window", "correct": False}]
    out = score.score_paths(links, [])
    assert out["per_path"]["quote"]["verdict"] == P
    assert out["per_path"]["window"]["verdict"] == F
    assert out["ship_paths"] == ["quote"] and out["verdict"] == P


def test_bar6_confirmed_twin_disables_its_path_only():
    links = [{"path": "quote", "correct": True}] * 59 + [{"path": "window", "correct": True}] * 59
    flags = [{"flag_id": "x", "path": "window", "confirmed": True}]
    out = score.score_paths(links, flags)
    assert out["ship_paths"] == ["quote"]


def test_bar6_insufficient_path_does_not_ship():
    links = [{"path": "quote", "correct": True}] * 59 + [{"path": "window", "correct": True}] * 3
    out = score.score_paths(links, [])
    assert out["per_path"]["window"]["verdict"] == I and out["ship_paths"] == ["quote"]


def test_bar6_no_surviving_path():
    fail = [{"path": "quote", "correct": False}] * 59
    assert score.score_paths(fail, [])["verdict"] == F
    assert score.score_paths([], [])["verdict"] == I


def test_overall_fail_beats_insufficient_and_sessions_floor():
    assert score.overall_verdict([P, F, I], 50) == F
    assert score.overall_verdict([P, I], 50) == I
    assert score.overall_verdict([P, P], 39) == I


# =========================================================================================
# Fit
# =========================================================================================

def _ev(coverage, lp=1.0, ip=1.0, missing=0):
    return {"coverage": coverage, "link_precision": lp, "inferred_precision": ip,
            "missing_gold": missing}


def test_fit_highest_feasible_coverage_wins():
    pts = [((0.3, 0.05, 0.1), _ev(0.9, lp=0.96)),       # infeasible: link precision
           ((0.4, 0.05, 0.1), _ev(0.8, ip=0.94)),       # infeasible: inferred precision
           ((0.5, 0.05, 0.1), _ev(0.7)),
           ((0.6, 0.05, 0.1), _ev(0.6))]
    assert score.choose_point(pts)[0] == (0.5, 0.05, 0.1)


def test_fit_boundaries_are_inclusive_and_none_is_feasible():
    assert score.feasible(_ev(0.5, lp=0.97, ip=0.95))
    assert score.feasible(_ev(0.0, lp=None, ip=None))
    assert not score.feasible(_ev(0.5, missing=1))


def test_fit_ties_go_to_higher_floor_then_margin_then_nothing_close():
    pts = [((0.5, 0.1, 0.1), _ev(0.7)), ((0.55, 0.05, 0.1), _ev(0.7))]
    assert score.choose_point(pts)[0] == (0.55, 0.05, 0.1)
    pts = [((0.5, 0.1, 0.1), _ev(0.7)), ((0.5, 0.15, 0.1), _ev(0.7))]
    assert score.choose_point(pts)[0] == (0.5, 0.15, 0.1)
    pts = [((0.5, 0.1, 0.25), _ev(0.7)), ((0.5, 0.1, 0.1), _ev(0.7))]
    assert score.choose_point(pts)[0] == (0.5, 0.1, 0.25)


def test_fit_no_feasible_point_is_none():
    assert score.choose_point([((0.3, 0.05, 0.1), _ev(0.9, lp=0.5))]) is None


def test_evaluate_point_counts():
    rows = [{"status": "linked"}, {"status": "linked"}, {"status": "none_close"},
            {"status": "ambiguous"}]
    verdicts = iter([True, False])
    ev = score.evaluate_point(rows, lambda r: next(verdicts), lambda r: False)
    assert ev["coverage"] == 0.5 and ev["link_precision"] == 0.5
    assert ev["inferred_precision"] == 1.0 and ev["missing_gold"] == 0


def test_grid_is_the_registered_208_points():
    g = run.grid()
    assert len(g) == 208
    assert run.FLOORS[0] == 0.30 and run.FLOORS[-1] == 0.90 and len(run.FLOORS) == 13
    assert run.MARGINS == (0.05, 0.10, 0.15, 0.20)
    assert run.NOTHING_CLOSES == (0.10, 0.15, 0.20, 0.25)


# =========================================================================================
# Store, split, guard
# =========================================================================================

def test_turn_round_trip_keeps_window_hits(tmp_path):
    t = _turn("pour slab", i=30)
    path = tmp_path / "item_support_eval" / "data" / "t.json"
    store.write_json(path, {"turns": [store.encode_turn(t)]})
    back = store.decode_turn(store.read_json(path)["turns"][0])
    assert back == t and back["abs_start"].tzinfo is None
    assert item_support._window_turns("09:00 – 09:01", "2026-10-05", [back]) == [0]


def test_write_guard_refuses_outside_the_ignored_dirs(tmp_path):
    with pytest.raises(SystemExit):
        store.require_eval_dir(tmp_path / "results" / "x.json")
    with pytest.raises(SystemExit):
        store.require_eval_dir(tmp_path / "item_support_eval" / "results" / "x.json")
    store.require_eval_dir(tmp_path / "item_support_eval" / "labels" / "x.jsonl")


def test_split_is_seeded_balanced_and_stable():
    keys = [f"f/2026-10-{d:02d}/s{d}" for d in range(1, 12)]
    a, b = store.split_sessions(keys), store.split_sessions(list(reversed(keys)))
    assert a == b
    assert sum(1 for v in a.values() if v == "fit") == 6
    assert store.split_sessions(keys, seed="other") != a


def test_gitignore_covers_data_runs_labels():
    lines = (Path(store.PACKAGE_DIR) / ".gitignore").read_text().split()
    assert {"data/", "runs/", "labels/"} <= set(lines)


def test_build_eligibility():
    ev = {"tier": "final", "topics": [{"evidence": [{"status": "verified"}]}]}
    assert build.DEFAULT_SINCE == "2026-08-01"
    assert build.eligible(ev, "2026-08-01") is None
    assert build.eligible(ev, "2026-07-31") == "before_since"
    assert build.eligible({**ev, "tier": "live"}, "2026-10-05") == "not_final"
    assert build.eligible({"tier": "final", "topics": [{"evidence": [{"status": "unmatched"}]}]},
                          "2026-10-05") == "no_evidence"
    assert build.parse_extraction_key("extractions/f/2026-10-05/b.json") == \
        ("f", "2026-10-05", "b")
    assert build.parse_extraction_key("extractions/f/b.json") is None


# =========================================================================================
# run.py + label.py on a synthetic session
# =========================================================================================

def _session(split="heldout", ref="abc"):
    turns = [_turn("morning all", i=0),
             _turn("unit 3 pre-line is done", i=10),
             _turn("unit 4 pre-line is done", "spk_1", i=20),
             _turn("lunch soon", i=200)]
    topic = {"time_range": "09:00 – 09:00", "evidence_status": "verified",
             "evidence": [{"status": "verified", "segment_key": "transcripts/f/d/seg1.json",
                           "offset_sec": 10.0}],
             "findings": [{"observation": "unit 3 pre-line done"}],
             "decisions": ["crane hire booked friday"]}
    return {"ref": ref, "split": split, "date": "2026-10-05", "folder": "f", "base": "b",
            "topics": [topic], "turns": turns}


def test_run_session_links_through_the_real_rule_and_restores_paths():
    before = item_support.PATHS
    rows = run.run_point([_session()], 0.5, 0.1, 0.2, ("quote", "window"))
    assert item_support.PATHS == before
    assert item_support.overlap.__name__ == "overlap" and \
        not hasattr(item_support.overlap, "cache_info")
    by_kind = {r["kind"]: r for r in rows}
    assert by_kind["findings"]["status"] == "linked"
    assert by_kind["findings"]["turn"] == 1 and by_kind["findings"]["path"] == "quote"
    assert by_kind["decisions"]["status"] == "none_close"
    assert by_kind["decisions"]["claim_type"] == "inferred"
    assert all("text" not in k for r in rows for k in r)


def test_run_point_window_only_still_links():
    rows = run.run_point([_session()], 0.5, 0.1, 0.2, ("window",))
    f = next(r for r in rows if r["kind"] == "findings")
    assert f["status"] == "linked" and f["path"] == "window" and f["turn"] == 1


def test_blind_view_widens_the_range_and_shows_no_heuristic_output():
    rows = label.blind_rows([_session()])
    finding = next(r for r in rows if r["kind"] == "findings")
    # 09:00-09:00 plus 2 minutes either side: turns 0-2, not the 09:03:20 turn.
    assert [t["i"] for t in finding["turns"]] == [0, 1, 2]
    assert set(finding) == {"item_ref", "split", "kind", "item_text", "turns"}
    assert set(finding["turns"][0]) == {"i", "speaker", "time", "text"}


def test_owner_sample_is_seeded_and_heldout_only():
    rows = [{"item_ref": f"r{i}", "split": "heldout" if i % 2 else "fit"} for i in range(200)]
    a, b = label.owner_sample(rows), label.owner_sample(list(reversed(rows)))
    assert {r["item_ref"] for r in a} == {r["item_ref"] for r in b}
    assert len(a) == 45 and all(r["split"] == "heldout" for r in a)


def test_flag_rows_catch_a_twin_link():
    s = _session()
    rows = [{"item_ref": "abc:0:findings:0", "session": "abc", "status": "linked", "turn": 2,
             "path": "window"}]
    out = label.flag_rows(rows, {"abc": s})
    assert len(out) == 1 and out[0]["classes"] == ["unit"]


def test_adjudication_rows_only_disagreements():
    s = _session()
    todo = [{"item_ref": "abc:0:findings:0", "item_text": "unit 3 pre-line done"},
            {"item_ref": "abc:0:decisions:0", "item_text": "crane hire booked friday"}]
    agent = {"abc:0:findings:0": _g("turn", [2]), "abc:0:decisions:0": _g("none")}
    run_rows = [{"item_ref": "abc:0:findings:0", "session": "abc", "status": "linked", "turn": 1},
                {"item_ref": "abc:0:decisions:0", "session": "abc", "status": "none_close",
                 "turn": None}]
    out = label.adjudication_rows(todo, agent, run_rows, {"abc": s})
    assert [r["item_ref"] for r in out] == ["abc:0:findings:0"]


def test_gold_validation():
    assert score.normalise_gold({"label": "turn", "turns": [3]}) == _g("turn", [3])
    for bad in ({"label": "turn", "turns": [1, 2]}, {"label": "several", "turns": [1]},
                {"label": "none", "turns": [1]}, {"label": "maybe", "turns": []}):
        with pytest.raises(ValueError):
            score.normalise_gold(bad)
    merged = score.merge_gold({"a": _g("none")}, {"a": _g("turn", [1])}, {"a": _g("turn", [2])})
    assert merged["a"] == _g("turn", [2])


# =========================================================================================
# build_summary glue
# =========================================================================================

def test_build_summary_end_to_end_counts_only(tmp_path):
    root = tmp_path / "item_support_eval"
    data, runs, labels = root / "data", root / "runs", root / "labels"
    s = _session()
    rec = {**s, "turns": [store.encode_turn(t) for t in s["turns"]]}
    store.write_json(data / "sessions" / "abc.json", rec)
    store.write_json(data / "manifest.json", {"seed": store.SPLIT_SEED, "sessions": ["abc"]})
    digest = store.manifest_digest(data)
    sessions = store.load_sessions(data)
    rows = run.run_point(sessions, 0.5, 0.1, 0.2, ("quote", "window"))
    meta = {"split": "heldout", "paths": ["quote", "window"], "floor": 0.5, "margin": 0.1,
            "nothing_close": 0.2, "manifest_digest": digest}
    run.write_point_run(runs / "a", rows, meta)
    run.write_point_run(runs / "b", rows, meta)
    store.write_jsonl(labels / "todo.agent.jsonl", [
        {"item_ref": "abc:0:findings:0", "manifest_digest": digest},
        {"item_ref": "abc:0:decisions:0", "manifest_digest": digest}])
    store.write_jsonl(labels / "done.agent.jsonl", [
        {"item_ref": "abc:0:findings:0", "label": "turn", "turns": [1]},
        {"item_ref": "abc:0:decisions:0", "label": "none", "turns": []}])
    store.write_jsonl(labels / "todo.owner.jsonl",
                      [{"item_ref": "abc:0:findings:0", "manifest_digest": digest}])
    store.write_jsonl(labels / "done.owner.jsonl", [
        {"item_ref": "abc:0:findings:0", "label": "turn", "turns": [1]}])
    out = tmp_path / "results" / "summary.json"
    summary = score.build_summary(run_a=runs / "a", run_b=runs / "b", out_path=out,
                                  data_dir=data, labels_dir=labels,
                                  fit_choice=runs / "fit" / "choice.json")
    b = summary["bars"]
    assert b["link_precision"]["accepted"] == 1 and b["link_precision"]["wrong"] == 0
    assert b["link_precision"]["verdict"] == I
    assert b["run_to_run"]["verdict"] == P
    assert b["agent_owner_agreement"]["agreement"] == 1.0
    assert b["inferred_precision"]["verdict"] == R
    assert summary["verdict"] == I            # 1 session, 1 link
    text = out.read_text(encoding="utf-8")
    for leak in ("pre-line", "unit 3", "abc", "seg1", "crane hire"):
        assert leak not in text
    assert summary["reported"]["find_items_latency"] == "not measured"
    assert summary["reported"]["coverage"]["all"]["linked"] == 1


def test_nested_datetimes_round_trip():
    t = {**_turn("x"), "words": [{"at": datetime(2026, 10, 5, 9, 0, 1)}]}
    assert store.decode_turn(json.loads(json.dumps(store.encode_turn(t)))) == t


# =========================================================================================
# Fix round 1
# =========================================================================================

def test_unanswered_or_non_boolean_flag_answers_stay_unanswered():
    rows = [{"flag_id": "a", "confirmed": None}, {"flag_id": "b", "confirmed": "no"},
            {"flag_id": "c", "confirmed": 0}, {"flag_id": "d", "confirmed": False},
            {"flag_id": "e", "confirmed": True}, {"flag_id": "f"}]
    assert score.flag_decisions(rows) == {"d": False, "e": True}


@pytest.mark.parametrize("answer", [None, "no", "yes"])
def test_bar2_non_boolean_answer_on_a_real_flag_is_insufficient(answer):
    s = _session()
    rows = [{"item_ref": "abc:0:findings:0", "session": "abc", "status": "linked", "turn": 2,
             "path": "window"}]
    fid = score.flag_id(rows[0])
    judge = score.Judge([s], {})
    flags = judge.flags(rows, score.flag_decisions([{"flag_id": fid, "confirmed": answer}]))
    assert len(flags) == 1
    assert score.score_twins(flags)["verdict"] == I


def test_bar2_rejected_flag_passes_and_confirmed_fails_through_the_glue():
    s = _session()
    rows = [{"item_ref": "abc:0:findings:0", "session": "abc", "status": "linked", "turn": 2,
             "path": "window"}]
    fid = score.flag_id(rows[0])
    judge = score.Judge([s], {})
    for answer, verdict in ((False, P), (True, F)):
        flags = judge.flags(rows, score.flag_decisions([{"flag_id": fid, "confirmed": answer}]))
        assert score.score_twins(flags)["verdict"] == verdict


def test_paths_match_is_a_set_comparison():
    assert score.paths_match(["window", "quote"], ["quote", "window"])
    assert score.paths_match(["quote"], ["quote"])
    assert not score.paths_match(["quote", "window"], ["quote"])
    assert not score.paths_match(None, ["quote"])


def test_view_widening_clamps_at_the_ends_of_the_day():
    assert label._hhmm_shift(0, 1, -label.VIEW_PAD) == "00:00"
    assert label._hhmm_shift(23, 58, label.VIEW_PAD) == "23:59"
    assert label._hhmm_shift(9, 0, -label.VIEW_PAD) == "08:58"


def _frozen_fixture(tmp_path):
    root = tmp_path / "item_support_eval"
    data, runs = root / "data", root / "runs"
    store.write_json(data / "manifest.json", {"seed": store.SPLIT_SEED, "sessions": []})
    return data, runs


def test_point_refuses_a_manifest_that_differs_from_the_fit(tmp_path):
    data, runs = _frozen_fixture(tmp_path)
    choice = runs / "fit" / "choice.json"
    store.write_json(choice, {"manifest_digest": store.manifest_digest(data)})
    run.check_fit_manifest(data, choice)                       # same manifest: fine
    store.write_json(data / "manifest.json", {"seed": "rebuilt", "sessions": []})
    with pytest.raises(SystemExit):
        run.check_fit_manifest(data, choice)


def test_summary_refuses_runs_made_on_another_manifest(tmp_path):
    data, runs = _frozen_fixture(tmp_path)
    meta = {"split": "heldout", "paths": ["quote", "window"],
            "manifest_digest": store.manifest_digest(data)}
    run.write_point_run(runs / "a", [], meta)
    run.write_point_run(runs / "b", [], meta)
    store.write_json(data / "manifest.json", {"seed": "rebuilt", "sessions": []})
    with pytest.raises(SystemExit):
        score.build_summary(run_a=runs / "a", run_b=runs / "b",
                            out_path=tmp_path / "results" / "s.json", data_dir=data,
                            labels_dir=tmp_path / "item_support_eval" / "labels",
                            fit_choice=runs / "fit" / "choice.json")


def test_fit_refuses_a_grid_not_on_the_fit_split_and_records_the_digest(tmp_path):
    data, runs = _frozen_fixture(tmp_path)
    labels = tmp_path / "item_support_eval" / "labels"
    grid_dir = runs / "grid"
    store.write_jsonl(grid_dir / "rows.jsonl", [])
    store.write_json(grid_dir / "meta.json",
                     {"split": "heldout", "manifest_digest": store.manifest_digest(data)})
    with pytest.raises(SystemExit):
        score.fit(grid_dir, data, labels, out_path=runs / "fit" / "choice.json")
    store.write_json(grid_dir / "meta.json",
                     {"split": "fit", "manifest_digest": store.manifest_digest(data)})
    out = score.fit(grid_dir, data, labels, out_path=runs / "fit" / "choice.json")
    assert out["manifest_digest"] == store.manifest_digest(data)
    assert store.read_json(runs / "fit" / "choice.json")["manifest_digest"] ==         store.manifest_digest(data)


# =========================================================================================
# Final fix: labels are pinned to the manifest
# =========================================================================================

def test_a_missing_manifest_raises_never_none(tmp_path):
    data = tmp_path / "item_support_eval" / "data"
    with pytest.raises(SystemExit):
        store.manifest_digest(data)
    with pytest.raises(SystemExit):
        store.require_same_manifest(None, data)


def _labelled_fixture(tmp_path):
    root = tmp_path / "item_support_eval"
    data, labels = root / "data", root / "labels"
    s = _session()
    store.write_json(data / "sessions" / "abc.json",
                     {**s, "turns": [store.encode_turn(t) for t in s["turns"]]})
    store.write_json(data / "manifest.json", {"seed": store.SPLIT_SEED, "sessions": ["abc"]})
    return data, labels


def test_export_records_the_manifest_digest_in_both_todo_files(tmp_path):
    data, labels = _labelled_fixture(tmp_path)
    label.export(data, labels)
    digest = store.manifest_digest(data)
    agent = store.read_jsonl(labels / "todo.agent.jsonl")
    assert agent and all(r["manifest_digest"] == digest for r in agent)
    assert all(r["manifest_digest"] == digest
               for r in store.read_jsonl(labels / "todo.owner.jsonl"))
    assert store.require_labels_on_manifest(labels, data) == digest


def test_score_refuses_labels_made_on_another_manifest(tmp_path):
    data, labels = _labelled_fixture(tmp_path)
    label.export(data, labels)
    store.write_jsonl(labels / "done.agent.jsonl",
                      [{"item_ref": "abc:0:findings:0", "label": "turn", "turns": [1]}])
    score.load_gold(labels, data)                                # same manifest: fine
    store.write_json(data / "manifest.json", {"seed": "rebuilt", "sessions": ["abc"]})
    with pytest.raises(SystemExit):
        score.load_gold(labels, data)
    # ...and re-exporting cannot launder the old answers onto the new manifest.
    with pytest.raises(SystemExit):
        label.export(data, labels)


def test_score_refuses_answers_whose_todo_file_is_missing_or_unpinned(tmp_path):
    data, labels = _labelled_fixture(tmp_path)
    store.write_jsonl(labels / "done.owner.jsonl",
                      [{"item_ref": "abc:0:findings:0", "label": "turn", "turns": [1]}])
    with pytest.raises(SystemExit):
        score.load_gold(labels, data)
    store.write_jsonl(labels / "todo.owner.jsonl", [{"item_ref": "abc:0:findings:0"}])
    with pytest.raises(SystemExit):
        score.load_gold(labels, data)


def test_a_done_row_carrying_another_digest_is_refused(tmp_path):
    data, labels = _labelled_fixture(tmp_path)
    label.export(data, labels)
    store.write_jsonl(labels / "done.agent.jsonl",
                      [{"item_ref": "abc:0:findings:0", "label": "turn", "turns": [1],
                        "manifest_digest": "0" * 64}])
    with pytest.raises(SystemExit):
        score.load_gold(labels, data)
