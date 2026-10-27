"""Report bench, Task 11: scoring with noise floor and failure gates."""
import importlib.util
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BENCH_DIR = os.path.join(REPO, "tools", "report-bench")


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(BENCH_DIR, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


score = _load("rb_score")
gold_mod = _load("rb_gold")


def test_fourteen_patterns():
    assert score.PLACEHOLDER_COUNT == 14


@pytest.mark.parametrize("text", ["Bullet 1: Meeting purpose", "due 2026-YYYY-MM-DD",
                                  "[insert name]", "{{date}}", "<responsible>", "at HH:MM",
                                  "[person] to do it", "lorem ipsum", "mail x@example.com",
                                  "Room XX", "<placeholder>"])
def test_placeholder_hits_text(text):
    assert score.placeholder_hits(None, text) >= 1


def test_real_date_no_hit():
    assert score.placeholder_hits(None, "Pour on 2026-10-28 at 09:30") == 0


def test_value_patterns_on_parsed():
    prompt = '{"action": "What needs to be done", "deadline": "When, or null"}'
    ex = score.schema_example_values(prompt)
    parsed = {"topics": [{"action_items": [{"action": "What needs to be done",
                                            "responsible": "TBD", "deadline": "..."}]}]}
    assert score.placeholder_hits(parsed, "", ex) == 3
    assert score.placeholder_hits({"a": "Clear the drain"}, "", ex) == 0


def test_noise_floor():
    assert score.noise_floor({"s1": [0.5, 0.75, 0.5], "s2": [0.2, 0.2, 0.2]},
                             {"s1": 4, "s2": 10}, "action_recall") == 0.25
    assert score.noise_floor({"s1": [0.6, 0.6, 0.6]}, {"s1": 5}, "action_recall") == 0.2
    assert score.noise_floor({"s1": [3, 5, 4]}, {"s1": 5}, "latency_ms") == 2


def _rep(**kw):
    base = {"parsed": True, "truncated": False, "placeholder_hits": 0, "fabricated": 0}
    base.update(kw)
    return base


def test_session_failed():
    assert score.session_failed([_rep(), _rep(truncated=True), _rep(truncated=True)])
    assert not score.session_failed([_rep(), _rep(), _rep(fabricated=1)])
    assert score.session_failed([_rep(parsed=False), _rep(placeholder_hits=2), _rep()])


GOLD = {"schema": "report-bench-gold/v1", "session": "s",
        "items": [{"id": "g1", "kind": "action", "what": "Clear the drainage channel",
                   "responsible": "Sam", "due": "Friday", "label": "true"},
                  {"id": "g2", "kind": "action", "what": "Order the steel mesh",
                   "label": "unsure"},
                  {"id": "g3", "kind": "decision", "what": "Pour concrete on Monday",
                   "label": "true"}]}


def test_unsure_out_of_recall_and_precision():
    items = [{"kind": "action", "text": "Clear the drainage channel", "responsible": "Sam",
              "due": "Fri"},
             {"kind": "action", "text": "Order the steel mesh"},
             {"kind": "action", "text": "Paint the fence"}]
    verdicts = {"X|c|m|1|2": {"verdict": "fabricated", "gold_id": None}}
    r = score.against_gold(items, GOLD, verdicts, "X", "X|c|m|1")
    assert r["action_recall"] == 1.0                     # 1 true gold, found
    assert r["action_precision"] == 0.5                  # unsure match excluded
    assert r["fabricated"] == 1
    assert r["responsible_correct"] == 1.0 and r["due_kept"] == 1.0
    assert r["decision_recall"] == 0.0


def test_unjudged_loose_item_is_unadjudicated():
    items = [{"kind": "action", "text": "Drainage channel works, big job"}]
    r = score.against_gold(items, GOLD, {}, "X", "p")
    assert r["unadjudicated"] == 1 and r["action_precision"] is None


def test_r_has_no_precision():
    r = score.against_gold([{"kind": "action", "text": "Clear the drainage channel"}],
                           GOLD, {}, "R", "p")
    assert "action_precision" not in r and r["action_recall"] == 1.0


def test_t_coverage_and_input_coverage():
    text = "## Actions\n- Clear the drainage channel\n## Decisions\n- nothing decided\n"
    items = gold_mod.split_t_items(text)
    inp = [{"kind": "action", "text": "Clear the drainage channel"},
           {"kind": "decision", "text": "Pour concrete on Monday"}]
    r = score.against_gold(items, GOLD, {}, "T", "p", input_items=inp,
                           sections=["Actions", "Decisions"], text=text)
    assert r["action_coverage"] == 1.0 and r["input_coverage"] == 0.5
    assert r["sections_present"] is True


def test_claude_schema_keys_none():
    rec = {"outcome": "ok", "attempts": [{"latency_ms": 5}]}
    m = score.mechanical(rec, "X", "anthropic", '{"topics": []}', '"topics"')
    assert m["schema_keys"] is None and m["parsed"]
    m = score.mechanical(rec, "X", "openrouter", '{"topics": []}', '"topics" "declared_site"')
    assert m["schema_keys"] is False


def test_score_phase_end_to_end(tmp_path):
    common = _load("rb_common")
    out = str(tmp_path)
    common.write_json(os.path.join(out, "inputs", "manifest.json"),
                      {"s": {"folder": "F", "date": "D", "role": "set"}, "R": {}})
    common.write_json(os.path.join(out, "gold", "gold_s.json"), GOLD)
    common.write_text(os.path.join(out, "prompts", "X-cur", "s.txt"), '"topics"')
    ans = ('{"topics": [{"action_items": [{"action": "Clear the drainage channel", '
           '"responsible": "Sam", "deadline": "Fri"}], "decisions": []}]}')
    for rep in (1, 2, 3):
        tp = os.path.join(out, "answers", f"{rep}.txt")
        common.write_text(tp, ans if rep < 3 else "not json")
        common.append_jsonl(os.path.join(out, "runs", "round0.jsonl"), {
            "key": f"round0|X|X-cur|muse|s|{rep}", "task": "X", "candidate": "X-cur",
            "model": "muse", "session": "s", "rep": rep, "outcome": "ok",
            "attempts": [{"latency_ms": 10 * rep}], "usd": 0.0, "text_path": tp,
            "prompt_path": os.path.join("prompts", "X-cur", "s.txt")})
    res = score.score_phase(out, "round0")
    c = res["X|X-cur|muse"]
    assert c["failed_sessions"] == 0
    assert c["mean"]["action_recall"] == pytest.approx(2 / 3)
    assert c["noise_floor"]["action_recall"] == 1.0


def test_verdict_tables_are_per_phase(tmp_path):
    common = _load("rb_common")
    out = str(tmp_path)
    common.write_json(os.path.join(out, "gold", "verdicts_pilot_s.json"), {"a": 1})
    common.write_json(os.path.join(out, "gold", "verdicts_round0_s.json"), {"b": 2})
    assert score._verdict_tables(out, "round0") == {"b": 2}


def test_verdicts_do_not_leak_across_sessions(tmp_path):
    common = _load("rb_common")
    out = str(tmp_path)
    common.write_json(os.path.join(out, "inputs", "manifest.json"),
                      {"s1": {"folder": "F", "date": "D", "role": "set"},
                       "s2": {"folder": "F", "date": "E", "role": "set"}, "R": {}})
    g = {"schema": "report-bench-gold/v1", "items": [
        {"id": "g1", "kind": "action", "what": "Clear the drainage channel", "label": "true"}]}
    ans = '{"topics": [{"action_items": [{"action": "Paint the fence"}]}]}'
    for sess in ("s1", "s2"):
        common.write_json(os.path.join(out, "gold", f"gold_{sess}.json"), dict(g, session=sess))
        tp = os.path.join(out, "answers", sess + ".txt")
        common.write_text(tp, ans)
        common.append_jsonl(os.path.join(out, "runs", "round0.jsonl"), {
            "key": f"round0|X|X-cur|muse|{sess}|1", "task": "X", "candidate": "X-cur",
            "model": "muse", "session": sess, "rep": 1, "outcome": "ok",
            "attempts": [{"latency_ms": 1}], "usd": 0.0, "text_path": tp,
            "prompt_path": "none.txt"})
    # The owner judges "Paint the fence" on s1 only.
    gm = gold_mod
    gold1 = gm.load_gold(os.path.join(out, "gold", "gold_s1.json"))
    pool = gm.adjudication_pool([{"cell": "X|X-cur|muse", "rep": 1, "items": [
        {"kind": "action", "text": "Paint the fence"}]}], gold1)
    new, table = gm.apply_adjudication(gold1, pool, {pool[0]["pid"]: "fabricated"})
    assert all(k.startswith("s1|") for k in table)
    assert all(i["id"].startswith(("g", "adj-s1-")) for i in new["items"])
    common.write_json(os.path.join(out, "gold", "verdicts_round0_s1.json"), table)
    res = score.score_phase(out, "round0")["X|X-cur|muse"]["sessions"]
    assert res["s1"]["reps"][0]["fabricated"] == 1
    assert res["s2"]["reps"][0]["fabricated"] == 0
    assert res["s2"]["reps"][0]["unadjudicated"] == 1
