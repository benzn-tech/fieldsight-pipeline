"""Tests for scripts/nightly_turn_pick_eval (nightly re-match J2, phase N0 harness).

Synthetic fixtures only: tmp data/labels dirs with made-up sessions. No model is ever
called: the client factory is patched (to a fake, or to raise), and the package must
work with the `anthropic` SDK not importable.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import nightly_turn_pick as ntp

from scripts.item_support_eval import score as tc_score
from scripts.item_support_eval import store
from scripts.nightly_turn_pick_eval import run, score

P, F, I, PEND = score.PASS, score.FAIL, score.INSUFFICIENT, score.PENDING

REF_A, REF_B, REF_C = "aaaaaaaaaaa1", "bbbbbbbbbbb2", "ccccccccccc3"
ANSWER = '{"picks": [{"item": "I1", "turn": "T1"}, {"item": "I2", "turn": "T2"}]}'


# ---------------------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------------------

def _turn(mm, text, speaker):
    t = datetime(2026, 11, 10, 10, mm, 0)
    return {"abs_start": t, "abs_end": t, "text": text, "speaker": speaker,
            "source_filename": "rec_a.json", "start_sec": float(mm * 60),
            "end_sec": float(mm * 60 + 2)}


def _session(ref, split, time_range="10:00-10:05"):
    turns = [_turn(1, "we pour slab level 3 tomorrow", "S1"),
             _turn(2, "there is a crack in the north wall", "S2"),
             _turn(3, "ok thanks", "S1")]
    topics = [{"time_range": time_range,
               "action_items": [{"action": "pour slab level 3"}],
               "findings": [{"observation": "crack in the north wall"}],
               "decisions": [], "questions": []}]
    return {"base": "b", "date": "2026-11-10", "folder": "synthetic", "ref": ref,
            "split": split, "topics": topics,
            "turns": [store.encode_turn(t) for t in turns]}


def _gold_rows(ref):
    return [{"item_ref": f"{ref}:0:action_items:0", "label": "turn", "turns": [0]},
            {"item_ref": f"{ref}:0:findings:0", "label": "turn", "turns": [1]}]


@pytest.fixture
def env(tmp_path, monkeypatch):
    data, labels = tmp_path / "data", tmp_path / "labels"
    (data / "sessions").mkdir(parents=True)
    labels.mkdir()
    (data / "manifest.json").write_text('{"sessions": 3}\n', encoding="utf-8")
    for ref, split, tr in ((REF_A, "fit", "10:00-10:05"), (REF_B, "fit", None),
                           (REF_C, "heldout", "10:00-10:05")):
        (data / "sessions" / f"{ref}.json").write_text(
            json.dumps(_session(ref, split, tr)), encoding="utf-8")
    digest = hashlib.sha256((data / "manifest.json").read_bytes()).hexdigest()
    gold = _gold_rows(REF_A) + _gold_rows(REF_B) + _gold_rows(REF_C)
    (labels / "done.agent.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in gold), encoding="utf-8")
    (labels / "todo.agent.jsonl").write_text(
        "".join(json.dumps({"item_ref": r["item_ref"], "manifest_digest": digest}) + "\n"
                for r in gold), encoding="utf-8")
    (labels / "todo.owner.jsonl").write_text(
        "".join(json.dumps({"item_ref": r["item_ref"], "split": "heldout",
                            "manifest_digest": digest}) + "\n"
                for r in _gold_rows(REF_C)), encoding="utf-8")
    runs = tmp_path / "nightly_turn_pick_eval" / "runs"
    results = tmp_path / "nightly_turn_pick_eval" / "results"
    wording = tmp_path / "WORDING.md"
    wording.write_text(f"# log\n\n## v0\nprompt_hash: {ntp.PROMPT_HASH}\n", encoding="utf-8")
    monkeypatch.setattr(run, "EXPECTED_MANIFEST", digest)
    monkeypatch.setattr(run, "RUNS_DIR", runs)
    monkeypatch.setattr(run, "RESULTS_DIR", results)
    monkeypatch.setattr(run, "WORDING_PATH", wording)
    return SimpleNamespace(data=data, labels=labels, runs=runs, results=results,
                           wording=wording, tmp=tmp_path)


class FakeAPIError(Exception):
    pass


class FakeClient:
    """`responder(n, prompt)` -> (text, stop_reason), or raises."""

    def __init__(self, responder=None):
        self.calls = []
        self.responder = responder or (lambda n, prompt: (ANSWER, "end_turn"))
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text, stop = self.responder(len(self.calls), kwargs["messages"][0]["content"])
        return SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking="hidden"),
                     SimpleNamespace(type="text", text=text)],
            stop_reason=stop, usage=SimpleNamespace(input_tokens=100, output_tokens=7))


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(run, "make_client", lambda: (client, FakeAPIError, ()))
    return client


def _args(env, split, out, *extra, reps=3):
    return ["--split", split, "--reps", str(reps), "--out", str(env.runs / out),
            "--data", str(env.data), "--labels", str(env.labels), "--workers", "1", *extra]


def _rep(env, out, k):
    return [json.loads(x) for x in
            (env.runs / out / f"rep{k}.jsonl").read_text(encoding="utf-8").splitlines()]


def _exit_code(excinfo):
    return excinfo.value.code


# ---------------------------------------------------------------------------------------
# run.py
# ---------------------------------------------------------------------------------------

def test_dry_run_counts_calls_without_calling(env, monkeypatch, capsys):
    def boom():
        raise AssertionError("client factory must not be constructed in a dry run")
    monkeypatch.setattr(run, "make_client", boom)
    run.main(_args(env, "fit", "dry", "--dry-run"))
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    # REF_A has 2 eligible items -> one call per rep; REF_B is all no_window -> no call.
    assert out[0].startswith("sessions=1 calls=3 ")
    assert "skipped_no_window=2" in out[0]
    assert not (env.runs / "dry").exists()


def test_dry_run_refuses_wrong_manifest(env, monkeypatch):
    monkeypatch.setattr(run, "EXPECTED_MANIFEST", "0" * 64)
    with pytest.raises(SystemExit):
        run.main(_args(env, "fit", "dry", "--dry-run"))


def test_dry_run_refuses_when_refs_differ_from_gold(env):
    rows = _gold_rows(REF_A)[:1] + _gold_rows(REF_C)
    (env.labels / "done.agent.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    with pytest.raises(SystemExit):
        run.main(_args(env, "fit", "dry", "--dry-run"))


def test_gold_ref_maps_to_task1_alias(env):
    [sess] = [s for s in store.load_sessions(env.data, split="fit") if s["ref"] == REF_A]
    prep = run.prepare(sess)
    req = prep["request"]
    assert [(i["alias"], i["stable_id"], i["kind"], i["row_id"]) for i in req["items"]] == [
        ("I1", f"{REF_A}:0:action_items:0", "action_items", None),
        ("I2", f"{REF_A}:0:findings:0", "findings", None)]
    gold = tc_score.gold_by_ref(store.read_jsonl(env.labels / "done.agent.jsonl"))
    assert {i["stable_id"] for i in req["items"]} <= set(gold)
    assert req["topics"]["0"][0]["turn_index"] == 0
    [sess_b] = [s for s in store.load_sessions(env.data, split="fit") if s["ref"] == REF_B]
    prep_b = run.prepare(sess_b)
    assert prep_b["request"]["items"] == []
    assert prep_b["reasons"] == {f"{REF_B}:0:action_items:0": "no_window",
                                 f"{REF_B}:0:findings:0": "no_window"}


def test_real_run_writes_rows_for_every_item(env, monkeypatch):
    client = _patch_client(monkeypatch, FakeClient())
    run.main(_args(env, "fit", "r1"))
    assert len(client.calls) == 3
    for k in (1, 2, 3):
        rows = _rep(env, "r1", k)
        assert {r["item_ref"] for r in rows} == {f"{x}:0:{n}:0" for x in (REF_A, REF_B)
                                                 for n in ("action_items", "findings")}
        by = {r["item_ref"]: r for r in rows}
        a = by[f"{REF_A}:0:action_items:0"]
        assert a["raw_pick"] == "T1" and a["turn_index"] == 0
        assert by[f"{REF_B}:0:findings:0"]["outcome"] == "skipped_no_window"
        for r in rows:
            assert set(r) == {"item_ref", "session", "kind", "lang", "raw_pick", "turn_index",
                              "outcome", "guard", "cross_language", "script_unknown"}
    meta = json.loads((env.runs / "r1" / "meta.json").read_text(encoding="utf-8"))
    assert meta["model"] == "claude-sonnet-5-5" and meta["prompt_hash"] == ntp.PROMPT_HASH
    assert meta["calls"] == 3 and meta["failed_calls"] == 0
    assert meta["usage"] == {"input_tokens": 300, "output_tokens": 21}
    assert meta["max_tokens"] == 16000 and meta["max_retries"] == 2 and meta["effort"] == "high"
    assert set(meta["hashes"]) == {"src/nightly_turn_pick.py", "src/item_support.py",
                                   "src/item_text_match.py", "labels/done.agent.jsonl",
                                   "data/manifest.json", "nightly_turn_pick_sans_instruction"}
    text = "".join(p.read_text(encoding="utf-8") for p in (env.runs / "r1").iterdir())
    assert "pour slab" not in text and "crack" not in text


def test_refuses_existing_run_dir(env, monkeypatch):
    client = _patch_client(monkeypatch, FakeClient())
    (env.runs / "r1").mkdir(parents=True)
    with pytest.raises(SystemExit) as e:
        run.main(_args(env, "fit", "r1"))
    assert _exit_code(e)
    assert client.calls == []


def test_resume_skips_recorded_pairs(env, monkeypatch):
    def first(n, prompt):
        if n == 1:
            return ("", "refusal")          # a failed call: recorded, never re-called
        raise RuntimeError("crash mid-run")
    _patch_client(monkeypatch, FakeClient(first))
    with pytest.raises(RuntimeError):
        run.main(_args(env, "fit", "r1", reps=3))
    client = _patch_client(monkeypatch, FakeClient())
    run.main(_args(env, "fit", "r1", "--resume", reps=3))
    assert len(client.calls) == 2           # reps 2 and 3 of REF_A only
    outcomes = [{r["outcome"] for r in _rep(env, "r1", k) if r["session"] == REF_A}
                for k in (1, 2, 3)]
    assert outcomes[0] == {"call_failed"}
    meta = json.loads((env.runs / "r1" / "meta.json").read_text(encoding="utf-8"))
    assert meta["calls"] == 3 and meta["failed_calls"] == 1 and meta["complete"] is True
    client2 = _patch_client(monkeypatch, FakeClient())
    run.main(_args(env, "fit", "r1", "--resume", reps=3))
    assert client2.calls == []


def test_refusal_and_max_tokens_are_failed_calls(env, monkeypatch):
    def responder(n, prompt):
        if n == 1:
            return ("", "refusal")
        if n == 2:
            return ('{"picks": [', "max_tokens")
        if n == 3:
            return ("not json at all", "end_turn")
        raise FakeAPIError("server said no")
    _patch_client(monkeypatch, FakeClient(responder))
    run.main(_args(env, "fit", "r1", reps=4))
    for k in (1, 2, 3, 4):
        rows = [r for r in _rep(env, "r1", k) if r["session"] == REF_A]
        assert {r["outcome"] for r in rows} == {"call_failed"}
        assert {r["raw_pick"] for r in rows} == {"__failed__"}
    meta = json.loads((env.runs / "r1" / "meta.json").read_text(encoding="utf-8"))
    assert meta["calls"] == 4 and meta["failed_calls"] == 4
    assert meta["usage"]["input_tokens"] == 300   # the API error carries no usage


def test_heldout_refuses_without_fit_run(env, monkeypatch):
    _patch_client(monkeypatch, FakeClient())
    with pytest.raises(SystemExit):
        run.main(_args(env, "heldout", "h1"))


def test_heldout_refuses_hash_mismatch(env, monkeypatch):
    client = _patch_client(monkeypatch, FakeClient())
    run.main(_args(env, "fit", "fit0"))
    meta_p = env.runs / "fit0" / "meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    good = json.dumps(meta)
    meta["hashes"]["src/item_support.py"] = "0" * 64
    meta_p.write_text(json.dumps(meta), encoding="utf-8")
    n = len(client.calls)
    with pytest.raises(SystemExit):
        run.main(_args(env, "heldout", "h1", "--fit-run", str(env.runs / "fit0")))
    # A different nightly_turn_pick.py is allowed (wording only, checked via WORDING.md).
    meta = json.loads(good)
    meta["hashes"]["src/nightly_turn_pick.py"] = "0" * 64
    meta_p.write_text(json.dumps(meta), encoding="utf-8")
    env.wording.write_text("## v0\nprompt_hash: 000000000000\n", encoding="utf-8")
    with pytest.raises(SystemExit):           # PROMPT_HASH not the last WORDING.md entry
        run.main(_args(env, "heldout", "h2", "--fit-run", str(env.runs / "fit0")))
    assert len(client.calls) == n
    env.wording.write_text(f"## v0\nprompt_hash: 000000000000\n## v1\n"
                           f"prompt_hash: {ntp.PROMPT_HASH}\n", encoding="utf-8")
    run.main(_args(env, "heldout", "h3", "--fit-run", str(env.runs / "fit0")))
    assert len(client.calls) == n + 3


def test_writes_refused_outside_runs(env):
    with pytest.raises(SystemExit):
        run.require_runs_dir(env.tmp / "elsewhere" / "x")
    with pytest.raises(SystemExit):
        run.require_runs_dir(env.runs)               # the runs dir itself, no run name
    with pytest.raises(SystemExit):
        run.write_json(env.tmp / "elsewhere" / "meta.json", {"a": 1})
    with pytest.raises(SystemExit):
        run.require_runs_dir(env.runs / ".." / ".." / "x")
    run.write_json(env.runs / "ok" / "meta.json", {"a": 1})
    assert (env.runs / "ok" / "meta.json").exists()


def test_call_sends_no_sampling_params(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)    # the SDK is not importable
    importlib.reload(run)
    importlib.reload(score)
    client = FakeClient(lambda n, p: ("hello", "end_turn"))
    out = run.call_model(client, FakeAPIError, "PROMPT")   # no setup errors given
    [kw] = client.calls
    assert kw == {"model": "claude-sonnet-5-5", "max_tokens": 16000,
                  "output_config": {"effort": "high"},
                  "messages": [{"role": "user", "content": "PROMPT"}]}
    assert out["text"] == "hello" and out["stop_reason"] == "end_turn"
    assert out["input_tokens"] == 100 and out["output_tokens"] == 7 and out["error"] is None

    def raises(n, p):
        raise FakeAPIError("x")
    err = run.call_model(FakeClient(raises), FakeAPIError, "PROMPT")
    assert err["error"] == "FakeAPIError" and err["text"] is None


# ---------------------------------------------------------------------------------------
# score.py: bars
# ---------------------------------------------------------------------------------------

def _ok(n):
    return [{"correct": True}] * n


def test_bar1_uses_cp_upper():
    assert score.score_bar1(_ok(58))["verdict"] == I
    out = score.score_bar1(_ok(59))
    assert out["verdict"] == P and out["cp_upper"] == tc_score.cp_upper(0, 59)
    out = score.score_bar1(_ok(99) + [{"correct": False}])
    assert out["cp_upper"] == tc_score.cp_upper(1, 100)
    assert out["verdict"] == (P if tc_score.cp_upper(1, 100) <= 0.05 else F)
    assert score.score_bar1(_ok(80), missing_gold=1)["verdict"] == I


def test_bar2_insufficient_below_30():
    assert score.score_bar2(_ok(29))["verdict"] == I
    assert score.score_bar2([])["verdict"] == I


def test_bar2_rule_does_not_use_59():
    # 30..58 links with 0 wrong: judged (not insufficient), and the bound is above 5%.
    out = score.score_bar2(_ok(30))
    assert out["verdict"] == F and out["cp_upper"] == tc_score.cp_upper(0, 30)
    assert score.score_bar2(_ok(58))["verdict"] == F
    assert score.score_bar2(_ok(59))["verdict"] == P


def test_bar3a_unanswered_flag_is_pending():
    assert score.score_bar3([{"confirmed": None}], by_gold=0, pairs=0)["verdict"] == PEND
    assert score.score_bar3([{"confirmed": False}], by_gold=0, pairs=0)["verdict"] == P
    assert score.score_bar3([{"confirmed": True}], by_gold=0, pairs=0)["verdict"] == F
    assert score.score_bar3([{"confirmed": None}], by_gold=1, pairs=0)["verdict"] == F
    assert score.score_bar3([], by_gold=0, pairs=1)["verdict"] == F
    assert score.score_bar3([], by_gold=0, pairs=0)["verdict"] == P


def test_flag_answers_only_json_bools():
    rows = [{"flag_id": "a", "confirmed": True}, {"flag_id": "b", "confirmed": "yes"},
            {"flag_id": "c", "confirmed": 0}, {"flag_id": "d", "confirmed": False}]
    assert score.flag_answers(rows) == {"a": True, "d": False}


def test_bar4_rep_agreement_counts_null_as_answer():
    raw = {"a": [None, None, None], "b": ["T1", "T1", "T1"], "c": ["T1", None, "T1"],
           "d": ["__failed__"] * 3, "e": ["__failed__", None, None]}
    out = score.score_bar4(raw, reps=3)
    assert out["items"] == 5 and out["agree"] == 3
    assert out["verdict"] == F
    nine = {str(i): ["T2"] * 3 for i in range(9)}
    nine["x"] = [None, "T1", None]
    assert score.score_bar4(nine, reps=3)["verdict"] == P
    assert score.score_bar4({"a": ["T1"]}, reps=1)["verdict"] == score.NOT_JUDGED


def test_bar5_pending_without_owner_labels():
    g = {"r1": {"label": "turn", "turns": [1]}, "r2": {"label": "turn", "turns": [2]}}
    todo = ["r1", "r2"]
    assert score.score_bar5(todo, g, {})["verdict"] == PEND
    assert score.score_bar5(todo, g, {"r1": g["r1"]})["verdict"] == PEND
    assert score.score_bar5(todo, g, dict(g))["verdict"] == P
    assert score.score_bar5([], g, {})["verdict"] == PEND


def test_worst_rep_ordering():
    assert score.worst(P, I, PEND) == PEND
    assert score.worst(PEND, F, I) == F
    assert score.worst(P, I) == I
    assert score.worst(P, P, score.NOT_JUDGED, score.NOT_APPLICABLE) == P
    assert score.per_rep_verdict([{"verdict": P}, {"verdict": I}, {"verdict": P}]) == I


def test_cross_off_rejudge_when_bar2_not_pass():
    on = {"bar1": F, "bar3": P, "bar4": P}
    off = {"bar1": P, "bar3": P, "bar4": P}
    v, allow = score.overall_verdict(I, on, off, bar5=P)
    assert (v, allow) == (P, False)
    v, allow = score.overall_verdict(F, on, off, bar5=P)
    assert (v, allow) == (P, False)
    v, allow = score.overall_verdict(P, on, off, bar5=P)
    assert (v, allow) == (F, True)
    v, allow = score.overall_verdict(I, off, off, bar5=PEND)
    assert (v, allow) == (PEND, False)


# ---------------------------------------------------------------------------------------
# score.py: files
# ---------------------------------------------------------------------------------------

def _score(env, name, *extra):
    return score.main(["--run", str(env.runs / name), *extra])


def test_heldout_refuses_second_scoring(env, monkeypatch):
    _patch_client(monkeypatch, FakeClient())
    run.main(_args(env, "fit", "fit0"))
    run.main(_args(env, "heldout", "h1", "--fit-run", str(env.runs / "fit0")))
    first = _score(env, "h1")
    assert first["provisional"] is True and first["split"] == "heldout"
    assert first["prompt_hash"] == ntp.PROMPT_HASH
    assert (env.runs / "h1" / "summary.json").exists()
    with pytest.raises(SystemExit) as e:
        _score(env, "h1")
    assert _exit_code(e)
    with pytest.raises(SystemExit):       # owner files incomplete: the one addendum is final
        _score(env, "h1", "--addendum", "2026-11-11")
    (env.labels / "done.owner.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in _gold_rows(REF_C)), encoding="utf-8")
    final = _score(env, "h1", "--addendum", "2026-11-11")
    assert final["provisional"] is False
    assert (env.runs / "h1" / "summary.addendum-2026-11-11.json").exists()
    with pytest.raises(SystemExit):
        _score(env, "h1", "--addendum", "2026-11-12")


def test_fit_run_scores_repeatedly_and_publish_needs_fit_ok(env, monkeypatch):
    _patch_client(monkeypatch, FakeClient())
    run.main(_args(env, "fit", "fit0"))
    s1 = _score(env, "fit0")
    s2 = _score(env, "fit0")
    assert s1 == s2
    assert s1["bars"]["bar5"]["verdict"] == score.NOT_APPLICABLE
    assert s1["reported"]["coverage"]["overall"]["min"] == pytest.approx(0.5)
    with pytest.raises(SystemExit):
        _score(env, "fit0", "--publish")
    _score(env, "fit0", "--publish", "--fit-ok")
    published = (env.results / "summary.json").read_text(encoding="utf-8")
    assert REF_A not in published and "pour slab" not in published


def test_bar3a_flags_exported_and_answers_read(env, monkeypatch):
    # A Chinese item (level 3) picks an English turn (level 4): cross-language, so the guard
    # does not check it and it is accepted, but bar 3(a) counts it and asks the owner.
    sess = _session(REF_A, "fit")
    sess["topics"][0]["action_items"] = [{"action": "3楼浇筑楼板"}]
    sess["turns"][0] = store.encode_turn(_turn(1, "pour slab level 4 tomorrow", "S1"))
    (env.data / "sessions" / f"{REF_A}.json").write_text(json.dumps(sess), encoding="utf-8")
    _patch_client(monkeypatch, FakeClient())
    run.main(_args(env, "fit", "fit0"))
    a = [r for r in _rep(env, "fit0", 1) if r["item_ref"] == f"{REF_A}:0:action_items:0"][0]
    assert a["outcome"] == "accepted" and a["cross_language"] is True and a["lang"] == "zh"
    s = _score(env, "fit0")
    assert s["bars"]["bar3"]["verdict"] == PEND and s["provisional"] is True
    # 1 cross-language link per rep: bar 2 insufficient -> cross-language off, re-judged.
    assert s["bars"]["bar2"]["verdict"] == I and s["allow_cross_language"] is False
    assert s["bars_cross_off"]["bar3"]["verdict"] == P
    cov, off = s["reported"]["coverage"], s["reported"]["coverage_cross_off"]
    assert (cov["overall"]["min"], off["overall"]["min"]) == (0.5, 0.25)
    assert cov["mixed"]["min"] == 0.25 and off["mixed"]["min"] == 0
    flags = store.read_jsonl(env.runs / "fit0" / "flags.owner.jsonl")
    fid = f"{REF_A}:0:action_items:0|0"
    assert [f["flag_id"] for f in flags] == [fid]
    assert "3楼" not in json.dumps(flags, ensure_ascii=False)
    done = env.runs / "fit0" / "done.flags.owner.jsonl"
    done.write_text(json.dumps({"flag_id": fid, "confirmed": "no"}) + "\n", encoding="utf-8")
    assert _score(env, "fit0")["bars"]["bar3"]["verdict"] == PEND
    done.write_text(json.dumps({"flag_id": fid, "confirmed": False}) + "\n", encoding="utf-8")
    s = _score(env, "fit0")
    assert s["bars"]["bar3"]["verdict"] == P
    done.write_text(json.dumps({"flag_id": fid, "confirmed": True}) + "\n", encoding="utf-8")
    assert _score(env, "fit0")["bars"]["bar3"]["verdict"] == F


def test_bar3_by_gold_and_pairs_from_a_real_twin_pair():
    turns = [_turn(1, "pour slab level 3 now", "S1"), _turn(2, "pour slab level 4 now", "S2"),
             _turn(3, "ok thanks", "S1")]
    sess = {"base": "b", "date": "2026-11-10", "folder": "synthetic", "ref": REF_A,
            "split": "fit", "turns": turns,
            "topics": [{"time_range": "10:00-10:05",
                        "action_items": [{"action": "pour slab level 3"},
                                         {"action": "pour slab level 4"}],
                        "findings": [], "decisions": [], "questions": []}]}
    r3, r4 = f"{REF_A}:0:action_items:0", f"{REF_A}:0:action_items:1"
    gold = {r3: {"label": "turn", "turns": [0]}, r4: {"label": "turn", "turns": [1]}}
    ctx = score.Ctx([sess], gold, {})
    assert ctx.twins == {r3: {r4}, r4: {r3}}

    def row(ref, outcome, turn_index, cross=False):
        return {"item_ref": ref, "session": REF_A, "outcome": outcome,
                "turn_index": turn_index, "cross_language": cross}

    # Both twins accepted on one turn: one pair; r4 sits on r3's gold turn and is wrong.
    _b1, _b2, b3 = score.judge_rep([row(r3, "accepted", 0), row(r4, "accepted", 0)], ctx, True)
    assert (b3["pairs"], b3["by_gold"], b3["verdict"]) == (1, 1, F)
    # One twin accepted on its twin's gold turn.
    _b1, _b2, b3 = score.judge_rep([row(r3, "accepted", 1), row(r4, "abstained", None)],
                                   ctx, True)
    assert (b3["pairs"], b3["by_gold"]) == (0, 1)
    # Cross-language links counted as not linked when cross-language is off.
    rows = [row(r3, "accepted", 0, True), row(r4, "accepted", 0, True)]
    _b1, _b2, b3 = score.judge_rep(rows, ctx, False)
    assert (b3["pairs"], b3["by_gold"], b3["verdict"]) == (0, 0, P)


class FakeAuthError(FakeAPIError):
    status_code = 401


def test_auth_error_aborts_without_recording(env, monkeypatch):
    def raises(n, p):
        raise FakeAuthError("bad key")
    client = FakeClient(raises)
    monkeypatch.setattr(run, "make_client", lambda: (client, FakeAPIError, (FakeAuthError,)))
    with pytest.raises(SystemExit) as e:
        run.main(_args(env, "fit", "r1"))
    assert "FakeAuthError" in str(e.value.code)
    assert [x for x in store.read_jsonl(env.runs / "r1" / "log.jsonl")
            if x["session"] == REF_A] == []
    meta = json.loads((env.runs / "r1" / "meta.json").read_text(encoding="utf-8"))
    assert meta["complete"] is False and meta["calls"] == 0
    good = _patch_client(monkeypatch, FakeClient())
    run.main(_args(env, "fit", "r1", "--resume"))
    assert len(good.calls) == 3


def test_sans_instruction_hash_ignores_only_the_wording():
    src = 'import x\n\nINSTRUCTION = (\n    "a b 楼\\n"\n    "c"\n)\n\nY = 1\n'
    reworded = src.replace('"c"', '"d e f"')
    other = src.replace("Y = 1", "Y = 2")
    h = run.sans_instruction_hash
    assert h(src) == h(reworded) and h(src) != h(other)
    assert h(src) == h(src.replace("\n", "\r\n"))


def test_heldout_refuses_non_wording_change(env, monkeypatch):
    client = _patch_client(monkeypatch, FakeClient())
    run.main(_args(env, "fit", "fit0"))
    meta_p = env.runs / "fit0" / "meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    assert "nightly_turn_pick_sans_instruction" in meta["hashes"]
    meta["hashes"]["nightly_turn_pick_sans_instruction"] = "0" * 64
    meta_p.write_text(json.dumps(meta), encoding="utf-8")
    n = len(client.calls)
    with pytest.raises(SystemExit) as e:
        run.main(_args(env, "heldout", "h1", "--fit-run", str(env.runs / "fit0")))
    assert "sans_instruction" in str(e.value.code)
    assert len(client.calls) == n
