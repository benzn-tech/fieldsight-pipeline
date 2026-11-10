"""Tests for the locator measurement's labels, owner sample and pre-registered bars (item quote
locator spec 2026-11-10 section 6; Task 7). Synthetic run files only: no AWS, no model call, no
labeller. All text is synthetic.
"""
from __future__ import annotations

import copy
import json
import string
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from turn_name_overlay import _stem

from scripts.item_support_eval import label, score, store

P, F, I = score.PASS, score.FAIL, score.INSUFFICIENT
RO, PO = score.REPORTED_ONLY, score.PENDING_OWNER
DATE = "2026-07-06"
PRIMARY = "primary-model"
OTHER = "other-model"
SRC = "seg_a.json"


# =========================================================================================
# Synthetic builders
# =========================================================================================

def _letters(i):
    out = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        out = string.ascii_lowercase[r] + out
    return out


def _text(i):
    """Distinct tokens per index, so different items never reach the match threshold."""
    w = _letters(i)
    return f"zeta{w} omega{w} kappa{w}"


def _turns(n, base=datetime(2026, 7, 6, 10, 0, 0)):
    out = []
    for i in range(n):
        at = base + timedelta(seconds=10 * i)
        out.append({"speaker": "spk_0", "text": _text(i), "start_sec": float(10 * i),
                    "end_sec": float(10 * i + 8), "abs_start": at,
                    "abs_end": at + timedelta(seconds=8),
                    "abs_start_str": at.strftime("%H:%M:%S"), "source_filename": SRC})
    return out


def _session(ref, split, n_turns=40):
    return {"ref": ref, "split": split, "date": DATE, "folder": "Synthetic_User",
            "base": f"base_{ref}", "topics": [], "turns": _turns(n_turns)}


def _support(status, turn=None, turns=None, range_checked=True):
    if status != "linked":
        return {"status": status, "method": "locator_v1", "rule_version": "locator_v1"}
    t = turns[turn]
    return {"status": "linked", "source_stem": _stem(t["source_filename"]),
            "start_sec": t["start_sec"], "end_sec": t["end_sec"], "speaker_label": t["speaker"],
            "quote_status": None, "method": "locator_v1", "rule_version": "locator_v1",
            "score": None, "range_checked": range_checked}


_FIELD = {"action_items": "action", "findings": "observation", "decisions": "decision",
          "questions": "question"}


def _it(i, status="linked", turn=None, kind="action_items", said_at="auto", text=None):
    """One item spec: text of index i, linked to turn `turn` (default i)."""
    return {"i": i, "status": status, "turn": i if turn is None else turn, "kind": kind,
            "said_at": said_at, "text": text}


def _rec(ref, arm, rep, items, turns, *, model=PRIMARY, error=None, completion=100,
         latency=10.0, evidence=(), plain_strings=False, stripped=0):
    topic = {"topic_title": "Synthetic", "time_range": "10:00 - 10:30",
             "evidence": list(evidence), "action_items": [], "findings": [], "decisions": [],
             "questions": []}
    raw = []
    for it in items:
        kind = it["kind"]
        text = it["text"] if it["text"] is not None else _text(it["i"])
        if plain_strings and kind in ("decisions", "questions") and arm == "A":
            topic[kind].append(text)
            continue
        child = {_FIELD[kind]: text}
        ci = len(topic[kind])
        if arm == "B":
            child["support"] = _support(it["status"], it["turn"], turns)
            said = it["said_at"]
            if said == "auto":
                said = None if it["status"] == "no_locator" else \
                    turns[it["turn"] % len(turns)]["abs_start_str"]
            raw.append({"item": f"0:{kind}:{ci}", "said_at": said})
        topic[kind].append(child)
    rec = {"ref": ref, "stage": "x", "arm": arm, "rep": rep, "prompt_sha": f"sha-{arm}",
           "manifest_digest": "digest", "topics": None if error else [topic],
           "usage": {"prompt_tokens": 1000, "completion_tokens": completion},
           "model": model, "latency_s": latency, "error": error, "shown": list(range(len(turns))),
           "status_suggestions_stripped": stripped}
    if arm == "B":
        rec["_raw_said_at"] = raw
    return rec


def _four(ref, turns, a_items, b_items=None, *, b2_items=None, models=None, **kw):
    b_items = a_items if b_items is None else b_items
    b2_items = b_items if b2_items is None else b2_items
    models = models or {}
    return {"A1": _rec(ref, "A", 1, a_items, turns, model=models.get("A1", PRIMARY), **kw),
            "A2": _rec(ref, "A", 2, a_items, turns, model=models.get("A2", PRIMARY), **kw),
            "B1": _rec(ref, "B", 1, b_items, turns, model=models.get("B1", PRIMARY), **kw),
            "B2": _rec(ref, "B", 2, b2_items, turns, model=models.get("B2", PRIMARY), **kw)}


def _gold_for(sessions, runs, wrong=()):
    """Gold = the B1 linked turn (correct) for every B1 item; `wrong` item_refs get another turn."""
    gold = {}
    for s in sessions:
        rec = runs[s["ref"]]["B1"]
        for row in score.run_items(rec):
            t = score.linked_turn(row["support"], s["turns"])
            if t is None:
                gold[row["item_ref"]] = {"label": "none", "turns": []}
            elif row["item_ref"] in wrong:
                gold[row["item_ref"]] = {"label": "turn", "turns": [t + 1]}
            else:
                gold[row["item_ref"]] = {"label": "turn", "turns": [t]}
    return gold


def _set(n_sessions, items_per, split="fit", n_turns=40, **kw):
    sessions, runs = [], {}
    for k in range(n_sessions):
        ref = f"{split[0]}{k:03d}"
        s = _session(ref, split, n_turns)
        sessions.append(s)
        runs[ref] = _four(ref, s["turns"], [_it(i) for i in range(items_per)], **kw)
    return sessions, runs


# =========================================================================================
# Matching (bar 1's rule)
# =========================================================================================

def test_match_is_one_to_one_same_kind_at_half_overlap():
    x = [{"kind": "action_items", "text": "zeta omega kappa"},
         {"kind": "findings", "text": "zeta omega kappa"}]
    y = [{"kind": "findings", "text": "zeta omega"},          # overlap(y, x) = 1.0 -> s = 1.0
         {"kind": "action_items", "text": "zeta delta gamma"}]   # 1/3 both ways -> no match
    assert score.match_items(x, y) == [(1, 0)]


def test_match_uses_the_larger_of_both_containments():
    x = [{"kind": "action_items", "text": "alpha bravo charlie delta"}]
    y = [{"kind": "action_items", "text": "alpha bravo"}]       # 0.5 one way, 1.0 the other
    assert score.match_items(x, y) == [(0, 0)]


def test_match_is_greedy_by_score_then_position():
    x = [{"kind": "action_items", "text": "alpha bravo charlie"},
         {"kind": "action_items", "text": "alpha bravo charlie"}]
    y = [{"kind": "action_items", "text": "alpha bravo charlie"}]
    assert score.match_items(x, y) == [(0, 0)]
    x2 = [{"kind": "action_items", "text": "alpha bravo xray"},   # s = 2/3 with y
          {"kind": "action_items", "text": "alpha bravo charlie"}]  # s = 1.0 with y
    assert score.match_items(x2, y) == [(1, 0)]


def test_plain_string_decisions_in_a_match_dict_decisions_in_b():
    s = _session("f000", "fit")
    items = [_it(0, kind="decisions"), _it(1, kind="questions")]
    a = _rec("f000", "A", 1, items, s["turns"], plain_strings=True)
    b = _rec("f000", "B", 1, items, s["turns"])
    ax, bx = score.run_items(a), score.run_items(b)
    assert [r["kind"] for r in ax] == ["decisions", "questions"]
    assert len(score.match_items(ax, bx)) == 2


# =========================================================================================
# Bar 1: admission unchanged
# =========================================================================================

def test_bar1_identical_runs_pass():
    sessions, runs = _set(3, 10)
    out = score.score_locator(sessions, runs, gold=_gold_for(sessions, runs), stage="fit")
    b1 = out["bars"]["admission"]
    assert b1["share"]["A1_A2"] == 1.0 and b1["share"]["A1_B1"] == 1.0
    assert b1["verdict"] == P


def test_bar1_b_dropping_30_percent_of_items_fails():
    sessions, runs = [], {}
    for k in range(3):
        s = _session(f"f{k:03d}", "fit")
        sessions.append(s)
        runs[s["ref"]] = _four(s["ref"], s["turns"], [_it(i) for i in range(10)],
                               [_it(i) for i in range(7)])
    out = score.score_locator(sessions, runs, gold=_gold_for(sessions, runs), stage="fit")
    b1 = out["bars"]["admission"]
    assert b1["share"]["A1_B1"] == pytest.approx(7 / 8.5)
    assert b1["items_per_session"]["B"] == 7 and b1["items_per_session"]["A"] == 10
    assert b1["verdict"] == F


def test_bar1_item_count_band_alone_can_fail():
    # One extra B item per 10: share 10 / 10.5 = 0.952 >= 0.95 and count +10%, both at the
    # boundary -> pass. Two extra: count +20% -> fail.
    sessions, runs = [], {}
    for k in range(2):
        s = _session(f"f{k:03d}", "fit")
        sessions.append(s)
        runs[s["ref"]] = _four(s["ref"], s["turns"], [_it(i) for i in range(10)],
                               [_it(i) for i in range(11)])
    out = score.score_locator(sessions, runs, gold=_gold_for(sessions, runs), stage="fit")
    assert out["bars"]["admission"]["verdict"] == P
    for ref in runs:
        s = next(x for x in sessions if x["ref"] == ref)
        runs[ref] = _four(ref, s["turns"], [_it(i) for i in range(10)],
                          [_it(i) for i in range(12)])
    out = score.score_locator(sessions, runs, gold=_gold_for(sessions, runs), stage="fit")
    assert out["bars"]["admission"]["verdict"] == F


# =========================================================================================
# Bar 3: link precision, strict
# =========================================================================================

def test_bar3_adjacent_same_speaker_turn_is_wrong():
    gold = {"label": "turn", "turns": [5]}
    assert score.locator_link_correct(5, gold) is True
    assert score.locator_link_correct(4, gold) is False
    assert score.locator_link_correct(6, gold) is False
    assert score.locator_link_correct(7, {"label": "several", "turns": [3, 7]}) is True
    assert score.locator_link_correct(None, gold) is False
    assert score.locator_link_correct(5, {"label": "none", "turns": []}) is False


def test_bar3_counts_and_cp_bound_match_cp_upper():
    sessions, runs = _set(6, 10)            # 60 links
    refs = [r["item_ref"] for r in score.run_items(runs["f000"]["B1"])][:2]
    gold = _gold_for(sessions, runs, wrong=set(refs))
    b3 = score.score_locator(sessions, runs, gold=gold, stage="fit")["bars"]["link_precision"]
    assert b3["accepted"] == 60 and b3["wrong"] == 2
    assert b3["cp_upper"] == score.cp_upper(2, 60)
    assert b3["verdict"] == F


def test_bar3_missing_gold_is_insufficient():
    sessions, runs = _set(6, 10)
    gold = _gold_for(sessions, runs)
    gold.pop(next(iter(gold)))
    b3 = score.score_locator(sessions, runs, gold=gold, stage="fit")["bars"]["link_precision"]
    assert b3["missing_gold"] == 1 and b3["verdict"] == I


# =========================================================================================
# Fit gate
# =========================================================================================

def test_fit_gate_58_links_is_insufficient_and_59_passes():
    sessions, runs = _set(2, 29)                        # 58 links
    out = score.score_locator(sessions, runs, gold=_gold_for(sessions, runs), stage="fit")
    assert out["fit_gate"]["links"] == 58
    assert out["fit_gate"]["verdict"] == I
    s = _session("f999", "fit")
    sessions.append(s)
    runs["f999"] = _four("f999", s["turns"], [_it(0)])
    out = score.score_locator(sessions, runs, gold=_gold_for(sessions, runs), stage="fit")
    assert out["fit_gate"]["links"] == 59
    assert out["fit_gate"]["verdict"] == P
    assert set(out["fit_gate"]["bars"]) == {"admission", "link_precision", "twin_hard_negatives"}


def test_fit_gate_ignores_heldout_sessions():
    fit, fruns = _set(2, 29, split="fit")
    held, hruns = _set(2, 10, split="heldout")
    runs = {**fruns, **hruns}
    out = score.score_locator(fit + held, runs, gold=_gold_for(fit + held, runs), stage="all")
    assert out["fit_gate"]["links"] == 58
    assert out["per_half"]["heldout"]["link_precision"]["accepted"] == 20
    assert out["bars"]["link_precision"]["accepted"] == 78


# =========================================================================================
# Bar 4: twin hard negatives (owner-confirmed flags)
# =========================================================================================

def _twin_set():
    s = _session("f000", "fit")
    s["turns"][0]["text"] = "fix the scaffold on level 3"
    items = [_it(0, text="fix the scaffold on level 2")] + [_it(i) for i in range(1, 5)]
    return [s], {"f000": _four("f000", s["turns"], items)}


def test_bar4_flags_need_a_json_boolean_and_a_confirmed_flag_fails():
    sessions, runs = _twin_set()
    gold = _gold_for(sessions, runs)
    fid = "f000:0:action_items:0|0"
    b4 = score.score_locator(sessions, runs, gold=gold, stage="fit")["bars"]["twin_hard_negatives"]
    assert b4["flags"] == 1 and b4["verdict"] == I
    b4 = score.score_locator(sessions, runs, gold=gold, stage="fit",
                             flag_decisions=score.flag_decisions(
                                 [{"flag_id": fid, "confirmed": "no"}]))["bars"]["twin_hard_negatives"]
    assert b4["verdict"] == I
    b4 = score.score_locator(sessions, runs, gold=gold, stage="fit",
                             flag_decisions={fid: False})["bars"]["twin_hard_negatives"]
    assert b4["verdict"] == P
    b4 = score.score_locator(sessions, runs, gold=gold, stage="fit",
                             flag_decisions={fid: True})["bars"]["twin_hard_negatives"]
    assert b4["verdict"] == F


# =========================================================================================
# Bar 5: coverage; bar 2 follow-through
# =========================================================================================

def test_bar5_coverage_counts_refusals_in_the_denominator():
    s = _session("f000", "fit")
    b = [_it(i) for i in range(6)] + [_it(6, "no_locator"), _it(7, "no_line"),
                                     _it(8, "ambiguous_stamp"), _it(9, "not_shown")]
    runs = {"f000": _four("f000", s["turns"], [_it(i) for i in range(10)], b)}
    out = score.score_locator([s], runs, gold=_gold_for([s], runs), stage="fit")
    assert out["bars"]["coverage"]["items"] == 10
    assert out["bars"]["coverage"]["linked"] == 6
    assert out["bars"]["coverage"]["verdict"] == P            # 60% is the owner's line
    b2 = out["bars"]["follow_through"]
    assert b2["with_said_at"] == 9 and b2["linked"] == 6
    assert b2["refusals"] == {"ambiguous_stamp": 1, "no_line": 1, "not_shown": 1}
    assert b2["no_locator"] == 1 and b2["no_support"] == 0
    assert b2["verdict"] == score.REPORTED


def test_bar5_below_60_percent_fails():
    s = _session("f000", "fit")
    b = [_it(i) for i in range(5)] + [_it(i, "no_line") for i in range(5, 10)]
    runs = {"f000": _four("f000", s["turns"], [_it(i) for i in range(10)], b)}
    out = score.score_locator([s], runs, gold=_gold_for([s], runs), stage="fit")
    assert out["bars"]["coverage"]["verdict"] == F


# =========================================================================================
# Bar 6: inferred precision among no_locator items
# =========================================================================================

def test_bar6_thresholds():
    rows = lambda sup, n: [{"supported": i < sup} for i in range(n)]   # noqa: E731
    out = score.score_no_locator(rows(0, 28))
    assert out["verdict"] == RO and out["inferred_on"] is False
    out = score.score_no_locator(rows(0, 29))
    assert out["verdict"] == P and out["inferred_on"] is True
    assert out["cp_upper"] == pytest.approx(score.cp_upper(0, 29)) and out["cp_upper"] <= 0.10
    out = score.score_no_locator(rows(3, 29))
    assert out["verdict"] == F and out["inferred_on"] is False
    out = score.score_no_locator(rows(0, 29), missing_gold=1)
    assert out["verdict"] == I and out["inferred_on"] is False


def test_bar6_reads_gold_on_no_locator_items():
    s = _session("f000", "fit", n_turns=40)
    b = [_it(i, "no_locator") for i in range(30)]
    runs = {"f000": _four("f000", s["turns"], [_it(i) for i in range(30)], b)}
    gold = {r["item_ref"]: {"label": "none", "turns": []}
            for r in score.run_items(runs["f000"]["B1"])}
    out = score.score_locator([s], runs, gold=gold, stage="fit")["bars"]["inferred_precision"]
    assert out["no_locator"] == 30 and out["supported"] == 0 and out["verdict"] == P
    ref0 = next(iter(gold))
    gold[ref0] = {"label": "turn", "turns": [0]}
    out = score.score_locator([s], runs, gold=gold, stage="fit")["bars"]["inferred_precision"]
    assert out["supported"] == 1


# =========================================================================================
# Bar 7: stability
# =========================================================================================

def test_bar7_one_moved_link_in_ten_is_0_9_and_passes():
    s = _session("f000", "fit")
    b1 = [_it(i) for i in range(10)]
    b2 = [_it(i) for i in range(9)] + [_it(9, turn=20)]
    runs = {"f000": _four("f000", s["turns"], b1, b1, b2_items=b2)}
    out = score.score_locator([s], runs, gold=_gold_for([s], runs), stage="fit")["bars"]["stability"]
    assert out["pairs"] == 10 and out["same_turn"] == 9
    assert out["share"] == pytest.approx(0.9) and out["verdict"] == P
    b2 = [_it(i) for i in range(8)] + [_it(8, turn=21), _it(9, turn=20)]
    runs = {"f000": _four("f000", s["turns"], b1, b1, b2_items=b2)}
    out = score.score_locator([s], runs, gold=_gold_for([s], runs), stage="fit")["bars"]["stability"]
    assert out["verdict"] == F


def test_bar7_only_counts_pairs_both_runs_linked():
    s = _session("f000", "fit")
    b1 = [_it(i) for i in range(10)]
    b2 = [_it(i) for i in range(9)] + [_it(9, "no_line")]
    runs = {"f000": _four("f000", s["turns"], b1, b1, b2_items=b2)}
    out = score.score_locator([s], runs, gold=_gold_for([s], runs), stage="fit")["bars"]["stability"]
    assert out["pairs"] == 9 and out["share"] == 1.0


# =========================================================================================
# Exclusions: fallback model, errors
# =========================================================================================

def test_fallback_model_session_is_excluded_from_paired_bars_and_counted():
    sessions, runs = _set(3, 10)
    s = sessions[1]
    runs[s["ref"]] = _four(s["ref"], s["turns"], [_it(i) for i in range(10)],
                           [_it(i) for i in range(3)], models={"B2": OTHER})
    out = score.score_locator(sessions, runs, gold=_gold_for(sessions, runs), stage="fit")
    assert out["sessions"]["primary_model"] == PRIMARY
    assert out["sessions"]["fallback_excluded"] == 1
    assert out["sessions"]["eligible"] == 2
    assert out["bars"]["admission"]["verdict"] == P
    assert out["bars"]["link_precision"]["accepted"] == 20
    assert out["bars"]["coverage"]["items"] == 20


def test_errored_or_missing_runs_are_excluded_and_counted():
    sessions, runs = _set(3, 10)
    runs["f000"]["A2"] = _rec("f000", "A", 2, [], sessions[0]["turns"], error="call_failed: x")
    del runs["f001"]["B2"]
    out = score.score_locator(sessions, runs, gold=_gold_for(sessions, runs), stage="fit")
    assert out["sessions"]["errored_excluded"] == 1
    assert out["sessions"]["missing_excluded"] == 1
    assert out["sessions"]["eligible"] == 1


def test_primary_model_is_the_mode_over_a1_runs():
    runs = {"a": {"A1": {"model": "m1"}}, "b": {"A1": {"model": "m1"}},
            "c": {"A1": {"model": "m2"}}}
    assert score.primary_model(runs) == "m1"


# =========================================================================================
# Bar 8 and the overall verdict
# =========================================================================================

def _passing_all():
    fit, fr = _set(3, 25, split="fit")
    held, hr = _set(3, 25, split="heldout")
    sessions, runs = fit + held, {**fr, **hr}
    return sessions, runs, _gold_for(sessions, runs)


def test_bar8_without_owner_labels_is_pending_owner_and_never_pass():
    sessions, runs, gold = _passing_all()
    todo = [{"item_ref": r} for r in list(gold)[:5]]
    out = score.score_locator(sessions, runs, gold=gold, owner_todo=todo, stage="all")
    assert out["bars"]["agent_owner_agreement"]["verdict"] == PO
    assert out["verdict"] == PO
    owner = {r["item_ref"]: gold[r["item_ref"]] for r in todo}
    out = score.score_locator(sessions, runs, gold=gold, owner_gold=owner, owner_todo=todo,
                              stage="all")
    assert out["bars"]["agent_owner_agreement"]["verdict"] == P
    assert out["verdict"] == P
    partial = dict(list(owner.items())[:3])
    out = score.score_locator(sessions, runs, gold=gold, owner_gold=partial, owner_todo=todo,
                              stage="all")
    assert out["verdict"] == PO


def test_bar8_agreement_compares_agent_with_owner_not_merged_gold():
    sessions, runs, gold = _passing_all()
    todo = [{"item_ref": r} for r in list(gold)[:10]]
    owner = {r["item_ref"]: gold[r["item_ref"]] for r in todo}
    for r in todo[:2]:
        owner[r["item_ref"]] = {"label": "none", "turns": []}
    out = score.score_locator(sessions, runs, gold=gold, owner_gold=owner, owner_todo=todo,
                              stage="all")
    assert out["bars"]["agent_owner_agreement"]["agreement"] == pytest.approx(0.8)
    assert out["bars"]["agent_owner_agreement"]["verdict"] == F
    assert out["verdict"] == F


def test_a_failing_bar_beats_pending_owner():
    sessions, runs, gold = _passing_all()
    wrong = {r for r in list(gold)[:10]}
    out = score.score_locator(sessions, runs, gold=_gold_for(sessions, runs, wrong=wrong),
                              owner_todo=[{"item_ref": next(iter(gold))}], stage="all")
    assert out["bars"]["link_precision"]["verdict"] == F
    assert out["verdict"] == F


def test_overall_insufficient_without_heldout_sessions():
    sessions, runs = _set(3, 25, split="fit")
    gold = _gold_for(sessions, runs)
    todo = [{"item_ref": next(iter(gold))}]
    out = score.score_locator(sessions, runs, gold=gold, owner_gold={todo[0]["item_ref"]:
                              gold[todo[0]["item_ref"]]}, owner_todo=todo, stage="all")
    assert out["verdict"] == I


# =========================================================================================
# Reported diagnostics
# =========================================================================================

def test_diagnostics_said_at_copied_from_evidence_and_rates():
    s = _session("f000", "fit")
    ev = [{"quote": "x", "at": s["turns"][0]["abs_start_str"]}, {"quote": "y", "at": "10:00"}]
    b = [_it(0), _it(1), _it(2, "ambiguous_stamp"), _it(3, "not_shown"), _it(4)]
    runs = {"f000": _four("f000", s["turns"], [_it(i) for i in range(5)], b, evidence=ev)}
    runs["f000"]["B1"]["topics"][0]["action_items"][4]["support"]["range_checked"] = False
    out = score.score_locator([s], runs, gold=_gold_for([s], runs), stage="fit")
    d = out["reported"]
    assert d["said_at_equals_evidence_at"]["links"] == 3
    assert d["said_at_equals_evidence_at"]["equal"] == 1
    assert d["said_at_equals_evidence_at"]["evidence_stamps_with_seconds"] == 1
    assert d["rates"]["ambiguous_stamp"]["count"] == 1
    assert d["rates"]["not_shown"]["count"] == 1
    assert d["rates"]["range_checked_false"]["count"] == 1
    assert d["rates"]["range_checked_false"]["of"] == 3


def test_diagnostics_coverage_by_kind_and_script_pair():
    s = _session("f000", "fit")
    s["turns"][1]["text"] = "脚手架需要修理"
    b = [_it(0), _it(1, text="修理脚手架"), _it(2, "no_line", kind="findings")]
    runs = {"f000": _four("f000", s["turns"], b, b)}
    out = score.score_locator([s], runs, gold=_gold_for([s], runs), stage="fit")
    cov = out["reported"]["coverage"]
    assert cov["by_kind"]["action_items"] == {"items": 2, "linked": 2, "coverage": 1.0}
    assert cov["by_kind"]["findings"]["linked"] == 0
    assert cov["by_script_pair"]["zh->zh"]["linked"] == 1
    assert cov["by_script_pair"]["en->en"]["linked"] == 1
    assert score.script_of("修理 the 脚手架") == "zh"
    assert score.script_of("fix the 架") == "en"
    assert score.script_of("") == "none"


def test_diagnostics_token_latency_deltas_are_null_safe_and_stray_count():
    sessions, runs = _set(2, 3)
    for ref in runs:
        for key in ("B1", "B2"):
            runs[ref][key]["usage"]["completion_tokens"] = 150
            runs[ref][key]["latency_s"] = 12.0
            runs[ref][key]["status_suggestions_stripped"] = 1
    runs["f001"]["A1"]["usage"] = None
    out = score.score_locator(sessions, runs, gold=_gold_for(sessions, runs), stage="fit")
    d = out["reported"]
    assert d["output_tokens"]["delta_b_minus_a"] == pytest.approx(50)
    assert d["output_tokens"]["sessions"] == 2
    assert d["latency_s"]["delta_b_minus_a"] == pytest.approx(2.0)
    assert d["stray_status_suggestions_on_b"] == {"count": 4, "runs_reporting": 4}
    for ref in runs:
        for key in runs[ref]:
            runs[ref][key]["usage"] = None
    out = score.score_locator(sessions, runs, gold=_gold_for(sessions, runs), stage="fit")
    assert out["reported"]["output_tokens"]["delta_b_minus_a"] is None


def test_side_set_table_and_kept_drop_flag():
    def side(arm, rep, kept, offered=4, weak=1):
        return {"arm": arm, "rep": rep, "error": None,
                "status_suggestions": {"offered": offered, "kept": kept,
                                       "dropped_by": {"weak": weak}}}
    side_runs = [side("A", 1, 3), side("A", 2, 3), side("B", 1, 2), side("B", 2, 2),
                 {"arm": "B", "rep": 2, "error": None, "status_suggestions": None}]
    t = score.side_table(side_runs)
    assert t["per_run"]["A1"] == {"runs": 1, "offered": 4, "kept": 3,
                                  "dropped_by": {"weak": 1}, "no_counts": 0}
    assert t["per_run"]["B2"]["no_counts"] == 1
    assert t["mean_kept"]["A"] == pytest.approx(3.0)
    assert t["mean_kept"]["B"] == pytest.approx(2.0)
    assert t["kept_delta_b_minus_a"] == pytest.approx(-1.0)
    assert t["kept_drops_under_b"] is True


# =========================================================================================
# Labels: blind export, owner sample
# =========================================================================================

FORBIDDEN = {"said_at", "support", "status", "arm", "_raw_said_at", "rep", "model"}


def _keys_deep(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys_deep(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys_deep(v)


def _write_files(tmp_path, sessions, runs):
    root = tmp_path / "item_support_eval"
    data, runs_dir, labels = root / "data", root / "runs", root / "labels"
    manifest = []
    for s in sessions:
        rec = dict(s, turns=[store.encode_turn(t) for t in s["turns"]])
        store.write_json(Path(data, "sessions", f"{s['ref']}.json"), rec)
        manifest.append({"ref": s["ref"], "split": s["split"]})
    store.write_json(Path(data, "manifest.json"), {"sessions": manifest})
    digest = store.manifest_digest(data)
    for ref, four in runs.items():
        for key, rec in four.items():
            rec = dict(rec, manifest_digest=digest)
            store.write_json(store.locator_run_path(key[0], int(key[1]), ref,
                                                    runs_dir=runs_dir), rec)
    return data, runs_dir, labels


def test_export_writes_blind_rows_without_said_at_support_or_status(tmp_path):
    sessions, runs = _set(2, 4)
    data, runs_dir, labels = _write_files(tmp_path, sessions, runs)
    out = label.export_locator(runs_dir, data_dir=data, labels_dir=labels)
    rows = store.read_jsonl(Path(labels, "todo.locator.agent.jsonl"))
    assert out["agent"] == 8 and len(rows) == 8
    for r in rows:
        assert not FORBIDDEN & set(_keys_deep(r))
        assert r["item_text"] and r["turns"]
        assert set(r) == {"item_ref", "split", "kind", "item_text", "turns",
                          "manifest_digest", "run_sha"}
    raw = Path(labels, "todo.locator.agent.jsonl").read_text(encoding="utf-8")
    assert "said_at" not in raw and "support" not in raw and "linked" not in raw
    # Task 10's files are never touched.
    assert not Path(labels, "todo.agent.jsonl").exists()


def test_export_and_owner_export_never_write_the_flag_file(tmp_path):
    sessions, runs = _twin_set()
    data, runs_dir, labels = _write_files(tmp_path, sessions, runs)
    label.export_locator(runs_dir, data_dir=data, labels_dir=labels)
    label.export_locator_owner(runs_dir, data_dir=data, labels_dir=labels)
    assert not Path(labels, "flags.owner.locator.jsonl").exists()


def test_locator_flags_waits_until_every_existing_todo_is_fully_answered(tmp_path):
    sessions, runs = _twin_set()
    data, runs_dir, labels = _write_files(tmp_path, sessions, runs)
    argv = ["locator-flags", "--data", str(data), "--runs", str(runs_dir),
            "--labels", str(labels)]
    flag_file = Path(labels, "flags.owner.locator.jsonl")

    def refused():
        with pytest.raises(SystemExit) as e:
            label.main(argv)
        assert e.value.code not in (0, None)
        assert not flag_file.exists()

    refused()                                           # no agent todo exported yet
    label.export_locator(runs_dir, data_dir=data, labels_dir=labels)
    todo = store.read_jsonl(Path(labels, "todo.locator.agent.jsonl"))
    assert len(todo) == 5
    answer = lambda r: {"item_ref": r["item_ref"], "label": "none", "turns": []}  # noqa: E731
    refused()                                           # nothing answered
    store.write_jsonl(Path(labels, "done.locator.agent.jsonl"), [answer(todo[0])])
    refused()                                           # 1 of 5: non-empty is not enough
    store.write_jsonl(Path(labels, "done.locator.agent.jsonl"), [answer(r) for r in todo])
    # Fit stage: agent todo complete, no owner todo yet -> allowed.
    out = label.main(argv)
    flags = store.read_jsonl(flag_file)
    assert out["flags"] == 1
    assert flags[0]["flag_id"] == "f000:0:action_items:0|0"
    assert flags[0]["classes"] == ["level"]
    flag_file.unlink()
    # Held-out stage: an owner todo of 40 exists; 1 of 40 answered -> refused; all -> allowed.
    owner = [{"item_ref": f"h{i:03d}:0:action_items:0"} for i in range(40)]
    store.write_jsonl(Path(labels, "todo.owner.locator.jsonl"), owner)
    store.write_jsonl(Path(labels, "done.owner.locator.jsonl"), [answer(owner[0])])
    refused()
    store.write_jsonl(Path(labels, "done.owner.locator.jsonl"), [answer(r) for r in owner])
    assert label.main(argv)["flags"] == 1 and flag_file.exists()


def test_owner_sample_puts_unknown_statuses_in_no_stratum():
    rows = _strata_rows(30, 12, 8) + [{"item_ref": f"s:0:decisions:{i}", "split": "heldout",
                                       "status": "no_support"} for i in range(10)]
    a = label.owner_sample_locator(rows, store.LOCATOR_OWNER_SEED)
    assert len(a) == 40 and all(r["status"] != "no_support" for r in a)


def test_iter_items_order_matches_the_raw_topics_so_said_at_keys_align():
    raw = [{"action_items": [{"action": "a0"}, {"action": "a1"}],
            "findings": [{"observation": "f0"}],
            "decisions": ["d0", {"decision": "d1"}], "questions": ["q0"]},
           {"decisions": [{"decision": "d2"}], "questions": [], "action_items": [],
            "findings": [{"observation": "f1"}, {"observation": "f2"}]}]
    from item_continuity import KINDS, normalise_children
    expected = []
    for ti, topic in enumerate(raw):
        for ln in KINDS:
            for ci, c in enumerate(topic.get(ln) or []):
                expected.append((f"{ti}:{ln}:{ci}", c if isinstance(c, str)
                                 else c[KINDS[ln].text_field]))
    topics = copy.deepcopy(raw)
    normalise_children(topics)
    got = [(f"{ti}:{ln}:{ci}", store.item_text(ln, c))
           for ti, ln, ci, c, _t in store.iter_items(topics)]
    assert got == expected


def _strata_rows(n_linked, n_nl, n_ref, split="heldout"):
    rows = []
    for i in range(n_linked):
        rows.append({"item_ref": f"s:0:action_items:{i}", "split": split, "status": "linked"})
    for i in range(n_nl):
        rows.append({"item_ref": f"s:0:findings:{i}", "split": split, "status": "no_locator"})
    for i in range(n_ref):
        st = ("no_line", "not_shown", "ambiguous_stamp", "unparseable", "outside_topic")[i % 5]
        rows.append({"item_ref": f"s:0:questions:{i}", "split": split, "status": st})
    return rows


def test_owner_sample_strata_sizes_and_determinism():
    rows = _strata_rows(60, 30, 20) + _strata_rows(50, 50, 50, split="fit")
    a = label.owner_sample_locator(rows, store.LOCATOR_OWNER_SEED)
    b = label.owner_sample_locator(list(reversed(rows)), store.LOCATOR_OWNER_SEED)
    assert [r["item_ref"] for r in a] == [r["item_ref"] for r in b]
    assert len(a) == 40
    st = [r["status"] for r in a]
    assert st.count("linked") == 20 and st.count("no_locator") == 12
    assert len(a) - st.count("linked") - st.count("no_locator") == 8
    assert all(r["split"] == "heldout" for r in a)
    c = label.owner_sample_locator(rows, "another-seed")
    assert [r["item_ref"] for r in c] != [r["item_ref"] for r in a]


def test_owner_sample_fills_short_strata_from_linked():
    rows = _strata_rows(60, 5, 3)
    a = label.owner_sample_locator(rows, store.LOCATOR_OWNER_SEED)
    st = [r["status"] for r in a]
    assert len(a) == 40
    assert st.count("no_locator") == 5 and st.count("linked") == 32


def test_locator_owner_export_writes_blind_sample(tmp_path):
    fit, fr = _set(1, 5, split="fit")
    held = []
    hr = {}
    for k in range(3):
        s = _session(f"h{k:03d}", "heldout")
        held.append(s)
        b = [_it(i) for i in range(10)] + [_it(i, "no_locator") for i in range(10, 15)] + \
            [_it(i, "no_line") for i in range(15, 18)]
        hr[s["ref"]] = _four(s["ref"], s["turns"], b, b)
    data, runs_dir, labels = _write_files(tmp_path, fit + held, {**fr, **hr})
    out = label.export_locator_owner(runs_dir, data_dir=data, labels_dir=labels)
    rows = store.read_jsonl(Path(labels, "todo.owner.locator.jsonl"))
    assert out["owner"] == 40 == len(rows)
    for r in rows:
        assert not FORBIDDEN & set(_keys_deep(r))
        assert r["split"] == "heldout"


# =========================================================================================
# Glue: files -> summary, provenance guards
# =========================================================================================

def _labels(labels, rows):
    store.write_jsonl(Path(labels, "done.locator.agent.jsonl"), rows)


def test_summary_from_files_writes_counts_only(tmp_path, capsys):
    sessions, runs = _set(2, 30)
    data, runs_dir, labels = _write_files(tmp_path, sessions, runs)
    label.export_locator(runs_dir, data_dir=data, labels_dir=labels)
    gold = _gold_for(sessions, runs)
    _labels(labels, [{"item_ref": k, **v} for k, v in gold.items()])
    out = score.main(["locator", "--stage", "fit", "--data", str(data), "--runs", str(runs_dir),
                      "--labels", str(labels)])
    assert out["fit_gate"]["verdict"] == P
    path = Path(runs_dir, "locator", "summary.fit.json")
    assert path.exists()
    text = path.read_text(encoding="utf-8") + capsys.readouterr().out
    assert "zeta" not in text and "f000" not in text


def test_summary_refuses_gold_made_on_another_b1_run(tmp_path):
    sessions, runs = _set(2, 30)
    data, runs_dir, labels = _write_files(tmp_path, sessions, runs)
    label.export_locator(runs_dir, data_dir=data, labels_dir=labels)
    gold = _gold_for(sessions, runs)
    _labels(labels, [{"item_ref": k, **v} for k, v in gold.items()])
    p = store.locator_run_path("B", 1, "f000", runs_dir=runs_dir)
    rec = store.read_json(p)
    rec["latency_s"] = 99.0
    store.write_json(p, rec)
    with pytest.raises(SystemExit, match="B1 run"):
        score.locator_summary("fit", data_dir=data, runs_dir=runs_dir, labels_dir=labels)


def test_summary_refuses_mixed_prompt_shas(tmp_path):
    sessions, runs = _set(2, 30)
    runs["f001"]["B2"]["prompt_sha"] = "sha-other"
    data, runs_dir, labels = _write_files(tmp_path, sessions, runs)
    with pytest.raises(SystemExit, match="prompt_sha"):
        score.locator_summary("fit", data_dir=data, runs_dir=runs_dir, labels_dir=labels)


def test_summary_refuses_runs_on_another_manifest(tmp_path):
    sessions, runs = _set(2, 30)
    data, runs_dir, labels = _write_files(tmp_path, sessions, runs)
    p = store.locator_run_path("A", 1, "f000", runs_dir=runs_dir)
    store.write_json(p, dict(store.read_json(p), manifest_digest="old"))
    with pytest.raises(SystemExit, match="manifest"):
        score.locator_summary("fit", data_dir=data, runs_dir=runs_dir, labels_dir=labels)


# =========================================================================================
# Fix round 1
# =========================================================================================

def _with_no_locator(n_nl, supported):
    """_passing_all plus one held-out session of n_nl no_locator items; gold says `supported`
    of them come from a turn."""
    sessions, runs, gold = _passing_all()
    s = _session("h900", "heldout")
    items = [_it(i, "no_locator") for i in range(n_nl)]
    sessions.append(s)
    runs["h900"] = _four("h900", s["turns"], items)
    for k, r in enumerate(score.run_items(runs["h900"]["B1"])):
        gold[r["item_ref"]] = {"label": "turn", "turns": [k]} if k < supported \
            else {"label": "none", "turns": []}
    return sessions, runs, gold


def _owner_ok(gold):
    todo = [{"item_ref": r} for r in list(gold)[:5]]
    return todo, {t["item_ref"]: gold[t["item_ref"]] for t in todo}


def test_bar6_fail_or_reported_only_never_changes_the_overall_verdict():
    # Coverage stays >= 60% with 30 no_locator items beside 150 links.
    for n_nl, sup, bar6, inferred in ((30, 5, F, False), (20, 0, RO, False), (30, 0, P, True)):
        sessions, runs, gold = _with_no_locator(n_nl, sup)
        todo, owner = _owner_ok(gold)
        out = score.score_locator(sessions, runs, gold=gold, owner_gold=owner, owner_todo=todo,
                                  stage="all")
        assert out["bars"]["inferred_precision"]["verdict"] == bar6
        assert out["inferred_on"] is inferred
        assert out["verdict"] == P


def test_bar8_owner_answer_without_agent_answer_is_missing_agent_not_pending():
    sessions, runs, gold = _passing_all()
    todo, owner = _owner_ok(gold)
    agent = dict(gold)
    agent.pop(todo[0]["item_ref"])
    out = score.score_locator(sessions, runs, gold=agent, owner_gold=owner, owner_todo=todo,
                              stage="all")
    b8 = out["bars"]["agent_owner_agreement"]
    assert b8["missing_agent"] == 1 and b8["missing_owner"] == 0
    assert b8["verdict"] == I and out["verdict"] == I


def test_output_tokens_fall_back_when_completion_tokens_is_null():
    assert score._out_tokens({"usage": {"completion_tokens": None, "output_tokens": 7}}) == 7
    assert score._out_tokens({"usage": {"output_tokens": 7}}) == 7
    assert score._out_tokens({"usage": {"completion_tokens": 5, "output_tokens": 7}}) == 5
    assert score._out_tokens({"usage": None}) is None


def test_primary_model_spans_all_stages_on_disk_and_is_recorded():
    fit, fr = _set(1, 30, split="fit", models={k: OTHER for k in ("A1", "A2", "B1", "B2")})
    held, hr = _set(3, 30, split="heldout")
    sessions, runs = fit + held, {**fr, **hr}
    out = score.score_locator(sessions, runs, gold=_gold_for(sessions, runs), stage="fit")
    assert out["sessions"]["primary_model"] == PRIMARY
    assert out["sessions"]["fallback_excluded"] == 1 and out["sessions"]["eligible"] == 0
    assert out["fit_gate"]["verdict"] == I


def test_locator_summary_fit_stage_reads_the_primary_over_every_stage(tmp_path):
    fit, fr = _set(1, 30, split="fit", models={k: OTHER for k in ("A1", "A2", "B1", "B2")})
    held, hr = _set(3, 30, split="heldout")
    data, runs_dir, labels = _write_files(tmp_path, fit + held, {**fr, **hr})
    out = score.locator_summary("fit", data_dir=data, runs_dir=runs_dir, labels_dir=labels)
    assert out["sessions"]["primary_model"] == PRIMARY
    assert out["sessions"]["scored"] == 1 and out["sessions"]["fallback_excluded"] == 1
