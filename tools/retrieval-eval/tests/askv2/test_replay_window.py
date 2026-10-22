"""replay_window.py: rebuild a --keep-rows dump's envelopes with the imported ask_tools at
a given window/budget, offline (no model, no Data API), and score them (item-window plan
Task 7). The src under test is whatever EVAL_SRC_REF exported (conftest), so assertions
that depend on the item window check `CENTRED` first."""
import json

import pytest

import ask_orchestrator
import ask_tools
import replay_window

CENTRED = hasattr(ask_tools, "WINDOW_CHARS")
DAY = "2026-09-10"
FILL = "we went over the general plan for the week and the rest of it"
GOLD = {
    "q-win": {"id": "q-win", "lang": "en", "date": DAY, "expect": "answerable",
              "question": "Is the crane booked?",
              "evidence": [{"match_any": ["crane hire is booked"]}]},
    "q-none": {"id": "q-none", "lang": "zh", "date": DAY, "expect": "answerable",
               "question": "屋面分包商为什么退出了",
               "evidence": [{"match_any": ["xtreme"]}]},
    "q-big": {"id": "q-big", "lang": "en", "date": DAY, "expect": "answerable",
              "question": "What did the engineer say about the pour?",
              "evidence": [{"match_any": ["engineer signed off"]}]},
}


def _transcript(n, at):
    return "\n".join(f"[08:{10 + i // 60:02d}:{i % 60:02d}] spk_1: {at.get(i, FILL)}"
                     for i in range(n))


def _row(cid, text, date=DAY):
    return {"id": cid, "chunk_text": text, "chunk_type": "transcript_window",
            "report_date": date, "site_name": "Riverside", "topic_title": "Roof",
            "source_s3_key": f"extractions/Ben/{date}/s.json",
            "metadata": {"window_span": "08:10:00–08:10:39"}}


def _turn(qid, batches):
    """A --keep-rows record. `batches`: [(judge_calls_before, [(name, args, rows,
    post_ids)])]; post_ids None means no envelope was built (an error envelope)."""
    out = {"id": qid, "expect": "answerable", "date": DAY, "turn": "t-" + qid,
           "batches": [], "invokes": [], "post_search": []}
    for bi, (before, calls) in enumerate(batches):
        envs, results = [], []
        for ci, (name, args, rows, post) in enumerate(calls):
            cid = str(ci)
            results.append({"id": cid, "name": name, "rows": rows, "meta": {}})
            meta = {"truncated": False} if post is not None else {"error": "upstream"}
            envs.append({"tool": name, "id": cid, "meta": meta, "items": []})
            if post is not None:
                out["post_search"].append({"batch": bi, "call_id": cid, "name": name,
                                           "args": args, "ids": post})
        out["batches"].append({"calls": [{"name": n, "args": a} for n, a, _, _ in calls],
                               "judge_calls_before": before, "envelopes": envs})
        out["invokes"].append({"batch": bi, "calls": [], "results": results})
    return out


def _dump():
    win = _row("c-win", _transcript(40, {30: "the crane hire is booked for Thursday"}))
    none = _row("c-none", _transcript(40, {}))
    big = [_row(f"c-big{i}", _transcript(40, {}) + f" {i}") for i in range(30)]
    ev = _row("c-ev", "the engineer signed off the pour")
    return [
        _turn("q-win", [(0, [("search_records", {"query": "Is the crane booked?"}, [win],
                              ["c-win"])])]),
        _turn("q-none", [(0, [("search_records", {"query": "屋面分包商为什么退出了"}, [none],
                               ["c-none"])])]),
        # Round 1 holds the evidence; round 2 is 30 big rows, truncated by the budget,
        # plus a call that failed upstream (header only).
        _turn("q-big", [(0, [("search_records", {"query": "engineer pour"}, [ev], ["c-ev"])]),
                        (1, [("search_records", {"query": "pour"}, big,
                              [r["id"] for r in big]),
                             ("day_overview", {"date_from": DAY}, [], None)])]),
    ]


def _replay(window=1000, result_chars=12_500, dump=None):
    return replay_window.replay(dump or _dump(), GOLD, ask_tools, ask_orchestrator,
                                window=window, result_chars=result_chars)


def test_the_window_recovers_a_head_cut_and_constants_are_restored():
    before = (ask_tools.MAX_RESULT_CHARS, ask_tools.MAX_TEXT)
    out = _replay()
    assert (ask_tools.MAX_RESULT_CHARS, ask_tools.MAX_TEXT) == before
    t = out["turns"]["q-win#1"]
    assert t["evidence_in_item"] is CENTRED          # past char 600: only a window has it
    assert out["window_mode"] == ("centred" if CENTRED else "head")
    head = _replay(window=600, result_chars=10_000)
    assert head["turns"]["q-win#1"]["evidence_in_item"] is CENTRED


def test_a_turn_without_evidence_stays_without_and_zh_is_reported_apart():
    out = _replay()
    assert out["turns"]["q-none#1"]["evidence_in_item"] is False
    assert set(out["summary"]["zh_mixed"]) == {"q-none#1"}


def test_a_truncated_envelope_is_rebuilt_from_the_post_search_rows():
    small, large = _replay(result_chars=10_000), _replay(result_chars=12_500)
    for out in (small, large):
        assert out["turns"]["q-big#1"]["truncated_envelopes"] == 1
        assert out["summary"]["truncated_envelopes"] == 1
    assert large["turns"]["q-big#1"]["items"] > small["turns"]["q-big#1"]["items"]
    assert small["turns"]["q-big#1"]["unreplayed"] == 0


def test_render_drops_and_evidence_bearing_drops(monkeypatch):
    out = _replay()
    assert out["turns"]["q-big#1"]["evidence_in_render"] is True
    assert out["summary"]["evidence_bearing_drops"] == 0
    monkeypatch.setattr(ask_orchestrator, "RENDER_MAX_CHARS", 3000)
    out = _replay()
    t = out["turns"]["q-big#1"]
    assert t["evidence_in_item"] is True and t["evidence_in_render"] is False
    assert t["evidence_dropped"] is True and t["dropped_lines"] > 0
    s = out["summary"]
    assert s["evidence_bearing_drops"] == 1 and s["turns_with_drop"] >= 1
    assert s["dropped_lines"] >= t["dropped_lines"]
    assert s["rendered_chars"]["p90"] >= s["rendered_chars"]["median"] > 0


def test_overhead_is_measured_on_the_rebuilt_items():
    o = _replay()["summary"]["overhead"]
    assert o["n"] > 0 and o["max"] >= o["p95"] > 0


def test_repeats_are_keyed_apart_and_baseline_lost_at_is_attached():
    dump = _dump() + [dict(_dump()[0], turn="t-q-win-2")]
    results = {"runs": [{"turns": [{"turn": "t-q-win", "funnel": {"lost_at": "cut_600_cap"}},
                                   {"turn": "t-q-win-2", "funnel": {"lost_at": "cut_text_cap"}},
                                   {"turn": "t-q-big", "funnel": {"lost_at": None}}]}]}
    out = replay_window.replay(dump, GOLD, ask_tools, ask_orchestrator, window=1000,
                               result_chars=12_500, results=results)
    assert {"q-win#1", "q-win#2"} <= set(out["turns"])
    assert out["turns"]["q-win#1"]["lost_at"] == "cut_text_cap"     # old label read as new
    cap = out["summary"]["cut_text_cap"]
    assert cap["turns"] == 2 and cap["evidence_in_item"] == (2 if CENTRED else 0)


def test_deterministic():
    a, b = _replay(), _replay()
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_compare_lists_flips_both_ways():
    def arm(**turns):
        return {"src_ref": "x", "summary": {"evidence_in_item": 0},
                "turns": {k: {"evidence_in_item": v[0], "evidence_in_render": v[1]}
                          for k, v in turns.items()}}
    a = arm(**{"q1#1": (False, False), "q2#1": (True, True), "q3#1": (True, True)})
    b = arm(**{"q1#1": (True, True), "q2#1": (False, True), "q3#1": (True, True)})
    d = replay_window.compare(a, b)
    assert d["evidence_in_item"] == {"gained": ["q1#1"], "lost": ["q2#1"]}
    assert d["evidence_in_render"] == {"gained": ["q1#1"], "lost": []}


def test_out_must_not_point_into_results(tmp_path):
    with pytest.raises(SystemExit, match="results/"):
        replay_window.parse_args(["--rows", "r.json", "--gold", "g.jsonl", "--src-ref", "x",
                                  "--family", "session",
                                  "--out", str(replay_window.HERE / "results" / "r.json")])


# --- --dump-envelopes ---------------------------------------------------------

def _dup_dump():
    """q-dup: round 1 search_records [ev, shared]; round 2 latest_activity [shared, other]
    (`shared` comes back byte-identical: same row, same ref) plus a 30-row search. q-win
    is a single-call turn."""
    ev = _row("c-ev", "the engineer signed off the pour")
    shared = _row("c-shared", "the scaffold inspection is on Friday")
    other = _row("c-other", "the rest of the plan")
    big = [_row(f"c-big{i}", _transcript(40, {}) + f" {i}") for i in range(30)]
    dup = _turn("q-big", [(0, [("search_records", {"query": "engineer pour"}, [ev, shared],
                                ["c-ev", "c-shared"])]),
                          (1, [("latest_activity", {}, [shared, other],
                                ["c-shared", "c-other"]),
                               ("search_records", {"query": "pour"}, big,
                                [r["id"] for r in big])])])
    return [_dump()[0], dup]


def _dumped(**kw):
    sink = []
    replay_window.replay(_dup_dump(), GOLD, ask_tools, ask_orchestrator, window=1000,
                         result_chars=12_500, envelope_dump=sink, **kw)
    return {d["qid"]: d for d in sink}


def test_dump_one_line_per_turn_with_rounds_and_flattened_items():
    d = _dumped()
    assert set(d) == {"q-win", "q-big"}
    t = d["q-big"]
    assert (t["repeat"], t["turn"]) == (1, "t-q-big")
    assert t["arm"]["window"] == 1000 and t["arm"]["result_chars"] == 12_500
    assert [r["round"] for r in t["rounds"]] == [1, 2]
    assert [[(e["name"], e["call_id"]) for e in r["envelopes"]] for r in t["rounds"]] == \
        [[("search_records", "0")], [("latest_activity", "0"), ("search_records", "1")]]
    r1 = t["rounds"][0]["envelopes"][0]["items"]
    assert [it["chunk_id"] for it in r1] == ["c-ev", "c-shared"]
    assert all("ref" in it and "text" in it for it in r1)
    assert d["q-win"]["metrics"] is None               # single-call turn
    json.dumps(list(d.values()))                       # serialisable as is


def test_dump_metrics_dup_lines_render_len_and_budget_drops(monkeypatch):
    m = _dumped()["q-big"]["metrics"]
    assert m["dup_item_lines_by_tool_pair"] == {"search_records->latest_activity": 1}
    assert m["budget_dropped_items"] == 0
    assert m["render_len"] > 0
    monkeypatch.setattr(ask_orchestrator, "RENDER_MAX_CHARS", 3000)
    m2 = _dumped()["q-big"]["metrics"]
    assert ask_orchestrator.RENDER_MAX_CHARS == 3000   # restored to the patched value
    assert m2["render_len"] == m["render_len"] > 3000  # measured before the budget
    assert m2["budget_dropped_items"] > 0
    assert m2["dup_item_lines_by_tool_pair"] == m["dup_item_lines_by_tool_pair"]


def test_dump_envelopes_must_not_point_into_results():
    with pytest.raises(SystemExit, match="results/"):
        replay_window.parse_args(["--rows", "r.json", "--gold", "g.jsonl", "--src-ref", "x",
                                  "--family", "session", "--out", "o.json",
                                  "--dump-envelopes",
                                  str(replay_window.HERE / "results" / "e.jsonl")])


def test_main_writes_the_envelope_dump(tmp_path):
    import os
    rows, gold = tmp_path / "rows.json", tmp_path / "gold.jsonl"
    rows.write_text(json.dumps(_dup_dump()), encoding="utf-8")
    gold.write_text("\n".join(json.dumps(g) for g in GOLD.values()), encoding="utf-8")
    dump = tmp_path / "env.jsonl"
    ref = os.environ.get("EVAL_SRC_REF", "origin/main")
    replay_window.main(["--rows", str(rows), "--gold", str(gold), "--src-ref", ref,
                        "--family", "session", "--window", "1000", "--result-chars", "12500",
                        "--out", str(tmp_path / "o.json"), "--dump-envelopes", str(dump)])
    lines = [json.loads(ln) for ln in dump.read_text(encoding="utf-8").splitlines()]
    assert [ln["qid"] for ln in lines] == ["q-win", "q-big"]
    assert lines[1]["family"] == "session"
    assert lines[1]["arm"] == {"src_ref": ref, "window": 1000, "result_chars": 12_500}
    assert set(lines[1]) == {"family", "qid", "repeat", "turn", "arm", "rounds", "metrics"}
