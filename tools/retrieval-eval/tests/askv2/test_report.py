import pytest

from askv2 import report

ALL_TRUE = {"sql": True, "rows": True, "item": True, "seen": True, "final_view": True,
            "cited": True}


def turn(qid, *, stages=None, lost_at=None, status="scored", expect="answerable",
         control=None, fallback=None, judge=1, prompt=1000, completion=50, reasoning=None,
         statements=10, ms_total=120.0, ms_max=40.0, largest=5000):
    return {"id": qid, "expect": expect,
            "funnel": {"stages": stages if expect == "answerable" else None,
                       "lost_at": lost_at, "status": status, "fallback": fallback,
                       "control": control, "flags": []},
            "judge": [{"usage": {"prompt": prompt, "completion": completion,
                                 "reasoning": reasoning}}] * judge,
            "sql": {"statements": statements, "ms_total": ms_total, "ms_max": ms_max,
                    "largest_response": largest, "templates": [["abc", "SELECT 1"]]}}


def miss(qid, lost, **kw):
    st = dict(ALL_TRUE)
    order = list(ALL_TRUE)
    first = {"never_retrieved": "sql", "cut_render_budget": "seen",
             "seen_not_cited": "cited"}[lost]
    for s in order[order.index(first):]:
        st[s] = False
    return turn(qid, stages=st, lost_at=lost, **kw)


def test_summary_counts_over_scored_turns_only():
    turns = [turn("q1", stages=ALL_TRUE), miss("q2", "seen_not_cited"),
             miss("q3", "never_retrieved"),
             turn("q4", stages=ALL_TRUE, status="fallback", fallback="judge_error"),
             turn("q5", stages=ALL_TRUE, status="invalid"),
             turn("c1", expect="unanswerable", control={"answered_with_citations": True}),
             turn("c2", expect="unanswerable", control={"answered_with_citations": False})]
    s = report.summarise(turns)
    assert s["answerable_scored"] == 3
    assert s["stages"]["sql"] == "2/3" and s["stages"]["seen"] == "2/3"
    assert s["stages"]["cited"] == "1/3"
    assert s["lost_at"] == {"seen_not_cited": 1, "never_retrieved": 1}
    assert s["status"] == {"scored": 5, "fallback": 1, "invalid": 1}
    assert s["fallback_reasons"] == {"judge_error": 1}
    assert s["controls"] == {"n": 2, "answered_with_citations": 1}
    assert s["judge_calls"] == 7
    assert s["tokens"] == {"prompt": 7000, "completion": 350, "reasoning": 0}
    assert s["sql"] == {"statements": 70, "turn_ms_p50": 120.0, "stmt_ms_max": 40.0,
                        "largest_response": 5000}


def test_flips_between_repeats():
    r1 = [turn("q1", stages=ALL_TRUE), miss("q2", "seen_not_cited"), turn("q3", stages=ALL_TRUE)]
    r2 = [miss("q1", "seen_not_cited"), miss("q2", "seen_not_cited"),
          turn("q3", stages=ALL_TRUE, status="invalid")]
    assert report.flips([r1, r2]) == ["q1"]          # q3 is not scored in both repeats
    assert report.flips([r1]) == []


def test_majority_over_repeats():
    r1 = [turn("q1", stages=ALL_TRUE)]
    r2 = [miss("q1", "seen_not_cited")]
    r3 = [turn("q1", stages=ALL_TRUE)]
    assert report.majority([r1, r2, r3], "cited") == {"q1": True}
    assert report.majority([r1, r2], "cited") == {"q1": None}      # a tie decides nothing


def test_caps_abort():
    caps = report.Caps(max_judge_calls=3, max_tokens=None)
    caps.check([turn("q1", judge=2)])
    with pytest.raises(report.CapExceeded, match="judge calls 4 > 3"):
        caps.check([turn("q1", judge=2), turn("q2", judge=2)])
    caps = report.Caps(max_judge_calls=None, max_tokens=2000)
    with pytest.raises(report.CapExceeded, match="tokens"):
        caps.check([turn("q1", prompt=1500, completion=600)])


def test_extrapolation_is_per_turn_mean_times_turns():
    e = report.extrapolate([turn("q1", judge=1), turn("q2", judge=3, prompt=3000)], 270)
    assert e["turns"] == 270 and e["judge_calls"] == 540
    assert e["prompt_tokens"] == 270 * (1000 + 9000) // 2


def test_diff_refuses_different_model_chains():
    a = {"fingerprint": {"model_chain": ["m1", "f1"]}, "runs": []}
    b = {"fingerprint": {"model_chain": ["m2", "f1"]}, "runs": []}
    with pytest.raises(SystemExit, match="model chains differ"):
        report.diff(a, b)


def test_diff_reports_majority_changes():
    fp = {"model_chain": ["m1"]}
    a = {"fingerprint": fp, "runs": [{"turns": [turn("q1", stages=ALL_TRUE),
                                                miss("q2", "seen_not_cited")]}]}
    b = {"fingerprint": fp, "runs": [{"turns": [miss("q1", "never_retrieved"),
                                                miss("q2", "seen_not_cited")]}]}
    d = report.diff(a, b)
    assert d["cited"] == {"q1": [True, False]}
    assert d["seen"] == {"q1": [True, False]}
    assert "label_note" not in d


def test_diff_notes_the_cut_text_cap_rename():
    fp = {"model_chain": ["m1"]}
    old = turn("q1", stages=ALL_TRUE, lost_at="cut_600_cap")
    a = {"fingerprint": fp, "runs": [{"turns": [old]}]}
    new = turn("q1", stages=ALL_TRUE, lost_at="cut_text_cap")
    b = {"fingerprint": fp, "runs": [{"turns": [new]}]}
    assert "cut_text_cap" in report.diff(a, b)["label_note"]
