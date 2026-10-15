"""Ask routing eval scorer (plan 2026-10-16 phase 4, Task 2; spec §9).

Fixture results and routes for every group rule and every gate line. Pure: no
AWS, no model. `tools/ask-routing-eval/` is not a package, so the module is
loaded by path.
"""
import importlib.util
import json
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EVAL_DIR = os.path.join(REPO, "tools", "ask-routing-eval")


def _load(name):
    spec = importlib.util.spec_from_file_location("ask_routing_eval_" + name,
                                                  os.path.join(EVAL_DIR, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


score = _load("score")


def item(group, **expect):
    return {"id": group + "-1", "group": group, "question": "q", "channels": ["screen", "voice"],
            "expect": expect}


def screen(answer="x", citations=1, from_web=False, web_offer=False, status="done"):
    res = {"answer": answer,
           "citations": [{"report_date": "2026-09-03"} for _ in range(citations)],
           "from_web": from_web}
    if web_offer:
        res["web_offer"] = True
    return score.normalize("screen", {"status": status, "result": res})


def voice(answer="x", web=False, status="done"):
    return score.normalize("voice", {"status": status, "answerText": answer, "web": web,
                                     "basis": {}, "audioBase64": "AAAA"})


def route(**kw):
    r = {"orch": "v2", "policy": "never", "calls": [["day_overview", "search_records"]],
         "refs_r": 1, "refs_w": 0, "bad_refs": 0, "basis": "records", "guard": False,
         "fallback": None, "ms": {"judge": [1000], "total": 4000}, "turn": "a" * 32}
    r.update(kw)
    return r


def verdict(it, res, rt):
    return score.score_turn(it, res, rt)["verdict"]


# ----------------------------------------------------------------- normalize

def test_offer_sentences_match_voice_web():
    import voice_web
    assert score.OFFER_ALSO == voice_web.OFFER_ALSO
    assert score.OFFER_NO_RECORDS == voice_web.OFFER_NO_RECORDS


def test_normalize_screen_and_voice_shapes():
    s = screen(answer="A [1].", citations=2, from_web=True, web_offer=True)
    assert s == {"channel": "screen", "status": "done", "answer": "A [1].",
                 "citations": [{"report_date": "2026-09-03"}] * 2, "from_web": True,
                 "web_offer": True, "error": None}
    v = voice(answer="We poured. " + score.OFFER_ALSO, web=True)
    assert v["citations"] is None and v["from_web"] is True and v["web_offer"] is True
    assert "audioBase64" not in v
    assert voice(answer="Nothing.")["web_offer"] is False


# ------------------------------------------------------------ unscored / errors

def test_missing_route_is_unscored_not_pass():
    out = score.score_turn(item("project_day"), screen(), None)
    assert out["verdict"] == "unscored" and out["why"]


def test_error_turn_fails():
    assert verdict(item("project_day"), screen(status="error"), route()) == "fail"
    assert verdict(item("project_day"), None, route()) == "fail"


# ---------------------------------------------------------------- project_day

def test_project_day_rules():
    it = item("project_day", dates=["2026-09-03"])
    assert verdict(it, screen(), route()) == "pass"
    assert verdict(it, screen(citations=0), route()) == "fail"
    assert verdict(it, screen(from_web=True), route()) == "fail"
    assert verdict(it, screen(), route(refs_w=1)) == "fail"


def test_project_day_voice_reads_refs_r():
    it = item("project_day", dates=["2026-09-03"])
    assert verdict(it, voice(), route(refs_r=2)) == "pass"
    assert verdict(it, voice(), route(refs_r=0)) == "fail"


# ---------------------------------------------------------- project_empty_day

@pytest.mark.parametrize("answer", [
    "Nothing was recorded that day. The last records are from 2026-09-23.",
    "The most recent records are from 23 September.",
    "Your latest recordings are from September 23.",
    "The last site day on file was the 23rd.",
    "There are no records for that day.",
])
def test_empty_day_passes(answer):
    it = item("project_empty_day", dates=["2026-10-15"], latest_before="2026-09-23")
    assert verdict(it, screen(answer=answer, citations=0), route(refs_r=0)) == "pass"


def test_empty_day_fails():
    it = item("project_empty_day", dates=["2026-10-15"], latest_before="2026-09-23")
    good = "Nothing was recorded; latest is 23 September."
    assert verdict(it, screen(answer="We poured the slab.", citations=0), route()) == "fail"
    assert verdict(it, screen(answer=good, from_web=True), route()) == "fail"
    assert verdict(it, screen(answer=good), route(refs_w=1)) == "fail"
    assert verdict(it, screen(answer=good), route(calls=[["search_records"]])) == "fail"
    # Day overview in a later round counts: calls is one list per round.
    assert verdict(it, screen(answer=good),
                   route(calls=[["search_records"], ["day_overview"]])) == "pass"


def test_date_forms_are_word_bounded():
    assert score.mentions_date("on 23 September", "2026-09-03") is False
    assert score.mentions_date("on 3 September", "2026-09-03") is True
    assert score.mentions_date("on Sep 3", "2026-09-03") is True


# ---------------------------------------------------------- person_or_latest

def test_person_or_latest_rules():
    it = item("person_or_latest", dates=["2026-09-02", "2026-09-03"], latest="2026-09-03")
    # Date in a citation card only.
    assert verdict(it, screen(answer="It came up recently [1]."), route()) == "pass"
    # Date in the answer only.
    res = score.normalize("screen", {"status": "done", "result": {
        "answer": "Last on 3 September [1].", "citations": [{"report_date": "2026-09-02"}]}})
    assert verdict(it, res, route()) == "pass"
    res["answer"] = "Last on 2 September [1]."
    assert verdict(it, res, route()) == "fail"
    assert verdict(it, screen(citations=0, answer="On 3 September."), route()) == "fail"
    assert verdict(it, voice(answer="Last on the 3rd of September."), route(refs_r=1)) == "pass"


# ------------------------------------------------------------------- general

def test_general_screen_needs_web_under_auto_or_required():
    it = item("general")
    assert verdict(it, screen(from_web=True, citations=0), route(policy="auto",
                                                                 refs_r=0)) == "pass"
    assert verdict(it, screen(from_web=True), route(policy="required")) == "pass"
    assert verdict(it, screen(from_web=False), route(policy="auto")) == "fail"
    assert verdict(it, screen(from_web=True), route(policy="never")) == "fail"


def test_general_voice_passes_on_the_offer():
    it = item("general")
    assert verdict(it, voice(answer=score.OFFER_NO_RECORDS), route(policy="offer")) == "pass"
    assert verdict(it, voice(answer="Cover is 40 mm."), route(policy="offer")) == "fail"
    assert verdict(it, voice(answer="Cover is 40 mm.", web=True),
                   route(policy="required")) == "pass"


# --------------------------------------------------------------------- mixed

def test_mixed_rules():
    it = item("mixed")
    assert verdict(it, screen(from_web=True), route(policy="auto", refs_r=1,
                                                    refs_w=1)) == "pass"
    assert verdict(it, screen(), route(policy="auto", refs_r=1, refs_w=0)) == "fail"
    assert verdict(it, screen(), route(policy="auto", refs_r=0, refs_w=1)) == "fail"
    assert verdict(it, voice(answer="We did. " + score.OFFER_ALSO),
                   route(policy="offer", refs_r=1)) == "pass"
    assert verdict(it, voice(answer="We did."), route(policy="offer", refs_r=1)) == "fail"


# --------------------------------------------- explicit_web / scoped / web_cap

def test_explicit_web():
    it = item("explicit_web")
    assert verdict(it, screen(), route(policy="required", web_ran=True)) == "pass"
    assert verdict(it, screen(), route(policy="required")) == "fail"


def test_scoped_general():
    it = item("scoped_general")
    assert verdict(it, screen(), route(policy="never", refs_w=0)) == "pass"
    assert verdict(it, screen(), route(policy="auto", refs_w=0)) == "fail"
    assert verdict(it, screen(), route(policy="never", refs_w=1)) == "fail"


def test_web_cap_refused_second_request_still_passes():
    it = item("web_cap")
    capped = route(policy="required", web_ran=True,
                   calls=[["search_records"], ["web_search"], ["web_search"]])
    out = score.score_turn(it, screen(), capped)
    assert out["verdict"] == "pass"
    assert any("2 time(s)" in w for w in out["why"])
    assert verdict(it, screen(), route(policy="auto", web_ran=False)) == "fail"


# ---------------------------------------------------------------------- gate

def rec(it, res, rt, channel="screen"):
    return {"item": it, "channel": channel, "result": res, "route": rt,
            "verdict": score.score_turn(it, res, rt)}


def passing_run():
    pd = dict(item("project_day", dates=["2026-09-03"]), id="pd1")
    gen = dict(item("general"), id="g1")
    out = [rec(pd, screen(answer="Poured [1]."), route()) for _ in range(3)]
    gen_route = route(policy="auto", web_ran=True, refs_r=0, refs_w=1,
                      ms={"judge": [], "total": 12000})
    out += [rec(gen, screen(from_web=True, citations=0), gen_route) for _ in range(3)]
    return out


def _line(lines, text):
    (hit,) = [ln for ln in lines if text in ln]
    return hit


def test_gate_passes_a_clean_run():
    g = score.gate(passing_run())
    assert g["pass"] is True, g["lines"]
    assert _line(g["lines"], "pd1/screen").startswith("PASS")
    assert _line(g["lines"], "g1/screen").startswith("PASS")


def test_gate_project_needs_three_of_three():
    run = passing_run()
    run[0] = rec(run[0]["item"], screen(citations=0), route())
    g = score.gate(run)
    assert g["pass"] is False
    assert _line(g["lines"], "pd1/screen").startswith("FAIL")


def test_gate_general_two_of_three_is_enough_one_is_not():
    run = passing_run()
    bad = route(policy="auto", ms={"judge": [], "total": 12000})
    run[3] = rec(run[3]["item"], screen(from_web=False), bad)
    assert score.gate(run)["pass"] is True
    run[4] = rec(run[4]["item"], screen(from_web=False), bad)
    g = score.gate(run)
    assert g["pass"] is False and _line(g["lines"], "g1/screen").startswith("FAIL")


def test_gate_unscored_turn_fails_the_gate():
    run = passing_run()
    run[0] = rec(run[0]["item"], screen(), None)
    g = score.gate(run)
    assert g["pass"] is False
    assert _line(g["lines"], "unscored turns: 1").startswith("FAIL")


def test_gate_guard_fire_on_a_project_item():
    run = passing_run()
    run[0] = rec(run[0]["item"], screen(), route(guard=True, fallback="guard"))
    g = score.gate(run)
    assert g["pass"] is False
    assert _line(g["lines"], "guard fires: 1 in total").startswith("PASS")
    assert _line(g["lines"], "guard fires on project items: 1").startswith("FAIL")


def test_gate_two_guard_fires_in_total():
    run = passing_run()
    sg = dict(item("scoped_general"), id="sg1")
    run += [rec(sg, screen(), route(guard=True)) for _ in range(2)]
    g = score.gate(run)
    assert g["pass"] is False
    assert _line(g["lines"], "guard fires: 2 in total").startswith("FAIL")


def test_gate_bad_refs_reaching_the_client():
    run = passing_run()
    run[0] = rec(run[0]["item"], screen(answer="Poured [R3] and [2].", citations=1), route())
    g = score.gate(run)
    assert g["pass"] is False
    assert _line(g["lines"], "bad refs reaching the client: 2").startswith("FAIL")
    # The route's stripped count is reported, never gated.
    run = passing_run()
    run[0] = rec(run[0]["item"], screen(answer="Poured [1]."), route(bad_refs=4))
    g = score.gate(run)
    assert g["pass"] is True
    assert _line(g["lines"], "route.bad_refs stripped").startswith("INFO")


def test_gate_voice_any_numeric_marker_is_a_leak():
    assert score.leaked_refs(voice(answer="Poured [1].")) == 1
    assert score.leaked_refs(voice(answer="Poured.")) == 0


def test_gate_p50_latency_lines():
    run = passing_run()
    slow = route(ms={"judge": [], "total": 9000})
    run[:3] = [rec(run[0]["item"], screen(), slow) for _ in range(3)]
    g = score.gate(run)
    assert g["pass"] is False
    assert _line(g["lines"], "p50 latency project/screen: 9.0 s").startswith("FAIL")
    run = passing_run()
    slow = route(policy="auto", web_ran=True, refs_r=0, refs_w=1,
                 ms={"judge": [], "total": 30000})
    run[3:] = [rec(run[3]["item"], screen(from_web=True, citations=0), slow)
               for _ in range(3)]
    g = score.gate(run)
    assert g["pass"] is False
    assert _line(g["lines"], "p50 latency general: 30.0 s").startswith("FAIL")


def test_gate_reported_groups_are_info_only():
    run = passing_run()
    ew = dict(item("explicit_web"), id="ew1")
    run.append(rec(ew, screen(), route(policy="required")))
    g = score.gate(run)
    assert g["pass"] is True
    assert _line(g["lines"], "ew1/screen").startswith("INFO")


# ----------------------------------------------------------------- the set

def test_the_set_has_the_plan_groups_and_fields():
    with open(os.path.join(EVAL_DIR, "set.v1.jsonl"), encoding="utf-8") as f:
        items = [json.loads(ln) for ln in f if ln.strip()]
    assert len(items) == 24
    counts = {}
    for it in items:
        assert set(it) >= {"id", "group", "question", "channels", "expect"}
        assert set(it["channels"]) <= {"screen", "voice"} and it["channels"]
        counts[it["group"]] = counts.get(it["group"], 0) + 1
    assert counts == {"project_day": 4, "project_empty_day": 3, "person_or_latest": 3,
                      "general": 5, "mixed": 3, "explicit_web": 2, "scoped_general": 1,
                      "web_cap": 1, "spoken_date": 2}
    assert len({it["id"] for it in items}) == 24
    for it in items:
        if it["group"] in ("project_day", "project_empty_day", "person_or_latest",
                           "spoken_date"):
            assert it["expect"].get("dates"), it["id"]
        if it["group"] == "project_empty_day":
            assert it["expect"].get("latest_before"), it["id"]
        if it["group"] == "person_or_latest":
            assert it["expect"]["latest"] in it["expect"]["dates"]
        if it["group"] == "scoped_general":
            # The site id is filled in at run time (org-api or --site-id).
            assert it["channels"] == ["screen"] and it["body"] == {"site_id": "@visible_site"}
    assert any(it["expect"].get("relative") for it in items)


# ------------------------------------------------------------- spoken_date
# Spoken dates are the judge's job (query_slots #1133 anchors only ISO and
# "last <weekday>"): it must resolve "3 September" itself and call day_overview.

def test_spoken_date_passes_when_the_judge_looked_up_that_day():
    it = item("spoken_date", dates=["2026-09-03"])
    rt = route(calls=[["search_records"], ["day_overview"]])
    assert verdict(it, screen(citations=1), rt) == "pass"


def test_spoken_date_fails_without_a_day_overview_call():
    it = item("spoken_date", dates=["2026-09-03"])
    assert verdict(it, screen(citations=1), route(calls=[["search_records"]])) == "fail"


def test_spoken_date_fails_when_no_card_is_from_that_day():
    it = item("spoken_date", dates=["2026-09-23"])
    rt = route(calls=[["search_records"], ["day_overview"]])
    assert verdict(it, screen(answer="x", citations=1), rt) == "fail"


def test_spoken_date_is_gated_at_two_thirds():
    assert "spoken_date" in score.TWO_THIRDS_GROUPS
    assert "spoken_date" in score.PROJECT_GROUPS
