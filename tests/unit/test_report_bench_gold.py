"""Report bench, Task 9: blind listing, matcher, bounded adjudication."""
import html.parser
import importlib.util
import inspect
import json
import os
import re
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


gold = _load("rb_gold")
common = _load("rb_common")
SENTINEL = "RUNSENTINEL model said this"
TURNS = [{"abs_start_str": "09:00:00", "speaker": "spk_0", "text": "Clear the drain </script> today"}]


def _inlined(path):
    text = open(path, encoding="utf-8").read()
    m = re.search(r'<script type="application/json" id="data">(.*?)</script>', text, re.S)
    return text, json.loads(m.group(1))


class _P(html.parser.HTMLParser):
    pass


def test_blind_page_has_no_run_input(tmp_path):
    params = inspect.signature(gold.blind_page).parameters
    assert set(params) == {"session", "turns", "out", "prefill"}
    p = gold.blind_page("sid1", TURNS, str(tmp_path))
    text, data = _inlined(p)
    assert SENTINEL not in text
    assert data["turns"][0]["text"] == TURNS[0]["text"]       # </script> survives
    _P().feed(text)


def test_prefill_counts():
    pre = gold.prefill_task_admission()
    counts = {}
    for i in pre:
        counts[i["label"]] = counts.get(i["label"], 0) + 1
    assert counts == {"true": 5, "false": 3, "unsure": 1}


def test_similarity():
    assert gold.similarity("Six brackets are missing", "Shortage of six brackets") >= 0.4
    zh = json.load(open(os.path.join(REPO, "tests", "fixtures", "lang_pair_zh.json"),
                        encoding="utf-8"))
    a = zh[1]["text"]
    assert gold.similarity(a, "排水沟堵了，昨天下雨") >= 0.4
    assert gold.similarity("", "x") == 0.0


def test_match_is_one_to_one():
    g = [{"id": "g1", "kind": "action", "what": "Clear the blocked drainage channel"}]
    items = [{"kind": "action", "text": "Clear the blocked drainage channel today"},
             {"kind": "action", "text": "Clear blocked drainage channel"},
             {"kind": "decision", "text": "Clear the blocked drainage channel"}]
    m = gold.match(items, g)
    assert sum(1 for x in m if x["gold_id"] == "g1") == 1
    assert m[2]["gold_id"] is None                       # other kind never matches


def _gold(n=1):
    return {"schema": gold.GOLD_SCHEMA, "session": "s",
            "items": [{"id": "g1", "kind": "action", "what": "Clear the drainage channel",
                       "label": "true"},
                      {"id": "g2", "kind": "action", "what": "Order steel", "label": "unsure"}]}


def test_pool_orders_two_rep_items_first_over_cap():
    runs = []
    for i in range(60):
        runs.append({"cell": "X|X-cur|muse", "rep": 1,
                     "items": [{"kind": "action", "text": f"unique item number {i} zebra{i}"}]})
    for i in range(10):
        for rep in (1, 2):
            runs.append({"cell": "X|X-cur|deepseek", "rep": rep,
                         "items": [{"kind": "action", "text": f"repeated thing alpha{i} beta{i}"}]})
    pool = gold.adjudication_pool(runs, _gold())
    assert len(pool) == 70
    assert all(r["n_reps"] >= 2 for r in pool[:10])
    assert all(r["hidden"] for r in pool[10:]) and not any(r["hidden"] for r in pool[:10])


def test_pool_has_loose_matches_and_page_is_blind_to_source(tmp_path):
    runs = [{"cell": "X|X-cur|muse", "rep": 1,
             "items": [{"kind": "action", "text": "Drainage channel, check it and also the wall"},
                       {"kind": "action", "text": "Clear the drainage channel"},
                       {"kind": "action", "text": "Paint the fence"}]}]
    pool = gold.adjudication_pool(runs, _gold())
    texts = {r["text"]: r for r in pool}
    assert "Clear the drainage channel" not in texts           # confident match
    assert texts["Paint the fence"]["gold_id"] is None
    p = gold.adjudicate_page("s", TURNS, pool, str(tmp_path))
    text, data = _inlined(p)
    assert "muse" not in text and "X-cur" not in text
    assert {r["pid"] for r in data["rows"]} == {r["pid"] for r in pool}


def test_apply_adjudication():
    runs = [{"cell": "c", "rep": 1, "items": [{"kind": "action", "text": "Paint the fence"},
                                              {"kind": "action", "text": "Fly to Mars"}]}]
    pool = gold.adjudication_pool(runs, _gold())
    by = {r["text"]: r["pid"] for r in pool}
    new, table = gold.apply_adjudication(_gold(), pool, {by["Paint the fence"]: "true",
                                                         by["Fly to Mars"]: "fabricated"})
    added = [i for i in new["items"] if i.get("source") == "adjudicated"]
    assert len(added) == 1 and added[0]["what"] == "Paint the fence"
    assert table["s|c|1|0"] == {"verdict": "match", "gold_id": added[0]["id"]}
    assert table["s|c|1|1"]["verdict"] == "fabricated"


def test_load_gold_rejects_duplicate_ids(tmp_path):
    g = _gold()
    g["items"].append(dict(g["items"][0]))
    p = tmp_path / "g.json"
    p.write_text(json.dumps(g), encoding="utf-8")
    with pytest.raises(ValueError):
        gold.load_gold(str(p))
    p.write_text(json.dumps(_gold()), encoding="utf-8")
    assert gold.load_gold(str(p))["session"] == "s"


def test_split_t_items():
    text = ("## Summary\nWe met. [t0]\n\n## Actions\n- Clear the drain [t0]\n"
            "| Item | Assigned | Due |\n|---|---|---|\n| Order steel | Sam | Fri |\n\n"
            "## Decisions\n1. Pour on Monday [t1, t2]\n\n## Other\n- not an item\n")
    assert gold.split_t_items(text) == [
        {"kind": "action", "text": "Clear the drain"},
        {"kind": "action", "text": "Order steel"},
        {"kind": "decision", "text": "Pour on Monday"}]


def test_items_from_parsed():
    x = {"topics": [{"action_items": [{"action": "A1", "responsible": "Sam", "deadline": None}],
                     "decisions": [{"decision": "D1", "decided_by": "Ann"}]}]}
    assert [i["kind"] for i in gold.items_from_parsed("X", x)] == ["action", "decision"]
    r = {"topics": [{"action_items": [{"action": "A1"}], "key_decisions": ["D1"]}]}
    assert [i["text"] for i in gold.items_from_parsed("R", r)] == ["A1", "D1"]


def test_gold_for_day(tmp_path):
    out = str(tmp_path)
    common.write_json(os.path.join(out, "gold", "gold_s1.json"), dict(_gold(), session="s1"))
    m = {"s1": {"folder": "F", "date": "D"}, "s2": {"folder": "F", "date": "E"},
         "R": {"F_D": {"folder": "F", "date": "D"}}}
    g = gold.gold_for_day(out, "F_D", m)
    assert [i["id"] for i in g["items"]] == ["s1:g1", "s1:g2"]


def test_loose_match_goes_to_the_owner():
    runs = [{"cell": "c", "rep": 1,
             "items": [{"kind": "action", "text": "Drainage channel works, big job"}]}]
    pool = gold.adjudication_pool(runs, _gold())
    assert len(pool) == 1
    assert pool[0]["gold_id"] == "g1" and 0.4 <= pool[0]["sim"] < 0.6
    new, table = gold.apply_adjudication(_gold(), pool, {pool[0]["pid"]: "confirm"})
    assert table["s|c|1|0"] == {"verdict": "match", "gold_id": "g1"}
