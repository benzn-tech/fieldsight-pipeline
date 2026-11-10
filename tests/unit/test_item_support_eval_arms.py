"""Tests for scripts/item_support_eval/arms.py (item quote locator, Task 6): the A/B extraction
arms, their staged call counts and the confirm gate. No AWS and no model call: a fake call_llm,
a fake env loader and a fake S3 stand in. All text is synthetic.
"""
from __future__ import annotations

import io
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import item_continuity
import status_suggestions
from tests.unit.test_lambda_extract_session import les  # noqa: F401  (fake AWS env + import)

from scripts.item_support_eval import arms, build, store

DATE = "2026-07-06"
FOLDER = "Synthetic_User"
LINES = ("we need to fix the scaffold on level two before friday",
         "the crane inspection is booked for monday morning",
         "please send the revised drawings to the engineer")


# =========================================================================================
# Fixtures
# =========================================================================================

def _turns(base_dt=datetime(2026, 7, 6, 10, 0, 0), lines=LINES, files=("seg_a.json",)):
    out = []
    for i, text in enumerate(lines):
        at = base_dt + timedelta(seconds=20 * i)
        out.append({"speaker": "spk_0", "text": text, "start_sec": float(20 * i),
                    "end_sec": float(20 * i + 15), "abs_start": at,
                    "abs_end": at + timedelta(seconds=15),
                    "abs_start_str": at.strftime("%H:%M:%S"),
                    "source_filename": files[i % len(files)]})
    return out


def _record(ref, split, folder=FOLDER, date=DATE, base=None, files=("seg_a.json",)):
    return {"ref": ref, "folder": folder, "date": date, "base": base or f"base_{ref}",
            "split": split, "topics": [],
            "turns": [store.encode_turn(t) for t in _turns(files=files)]}


def _dirs(tmp_path):
    root = tmp_path / "item_support_eval"
    return root / "data", root / "runs"


def _write_set(data_dir, n_fit, n_heldout, n_side=0):
    manifest = []
    for i in range(n_fit + n_heldout):
        split = "fit" if i < n_fit else "heldout"
        ref = f"r{i:03d}"
        store.write_json(Path(data_dir, "sessions", f"{ref}.json"), _record(ref, split))
        manifest.append({"ref": ref, "split": split})
    store.write_json(Path(data_dir, "manifest.json"), {"sessions": manifest})
    for i in range(n_side):
        ref = f"s{i:03d}"
        rec = _record(ref, "side")
        rec.update({"prior": [], "n_segments": 1, "prior_source": "published_current"})
        store.write_json(Path(data_dir, "side", "sessions", f"{ref}.json"), rec)
    if n_side:
        store.write_json(Path(data_dir, "side", "manifest.json"), {"sessions": n_side})


def _payload(said_at="10:00:00", extra=None):
    action = {"action": "Fix the scaffold on level two", "responsible": "Sam",
              "deadline": "Friday", "priority": "high"}
    if said_at is not None:
        action["said_at"] = said_at
    if extra:
        action.update(extra)
    return {"topics": [{"topic_title": "Scaffold", "time_range": "10:00 - 10:01",
                        "action_items": [action], "findings": [], "decisions": [],
                        "questions": []}], "declared_site": None}


class FakeCall:
    """call_llm double: records (prompt, kwargs); answers per call from `answer(n, prompt)`."""

    def __init__(self, answer=None):
        self.calls = []
        self.answer = answer or (lambda n, prompt: (json.dumps(_payload()), None))

    def __call__(self, prompt, **kw):
        self.calls.append((prompt, kw))
        info = kw.get("info")
        if isinstance(info, dict):
            info["usage"] = {"prompt_tokens": 100, "completion_tokens": 10}
            info["model"] = "fake-model"
        return self.answer(len(self.calls), prompt)


class FakeEnv:
    def __init__(self):
        self.calls = 0

    def __call__(self, function_name, **kw):
        self.calls += 1
        return ["LLM_PROVIDER"]


def _main(argv, data_dir, runs_dir, call=None, load_env=None):
    return arms.main(argv + ["--data", str(data_dir), "--runs", str(runs_dir)],
                     call=call or FakeCall(), load_env=load_env or FakeEnv())


def _run_files(runs_dir, side=False):
    base = Path(runs_dir, "locator", "side") if side else Path(runs_dir, "locator")
    out = {}
    for arm in arms.ARMS:
        for rep in arms.REPS:
            for p in sorted(Path(base, arm, str(rep)).glob("*.json")):
                out[(arm, rep, p.stem)] = store.read_json(p)
    return out


def _children(topics):
    for t in topics:
        for ln in ("action_items", "findings", "decisions", "questions"):
            for c in t.get(ln) or []:
                yield c


# =========================================================================================
# count / confirm gate
# =========================================================================================

def test_count_makes_no_call_and_matches_the_staged_spend(tmp_path, capsys):
    data, runs = _dirs(tmp_path)
    _write_set(data, 30, 29, 6)
    call, env = FakeCall(), FakeEnv()
    got = {}
    for stage in ("fit", "heldout", "side"):
        out = _main(["count", "--stage", stage], data, runs, call=call, load_env=env)
        got[stage] = out["calls"]
        assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["calls"] == out["calls"]
    assert got == {"fit": 120, "heldout": 116, "side": 24}
    assert call.calls == [] and env.calls == 0
    assert arms.plan_calls("fit", data_dir=data, runs_dir=runs) == 120


def test_run_without_matching_confirm_count_exits_before_any_call(tmp_path):
    data, runs = _dirs(tmp_path)
    _write_set(data, 2, 1)
    call, env = FakeCall(), FakeEnv()
    for argv in (["run", "--stage", "fit", "--emit-evidence", "false"],
                 ["run", "--stage", "fit", "--emit-evidence", "false", "--confirm-count", "7"]):
        with pytest.raises(SystemExit) as e:
            _main(argv, data, runs, call=call, load_env=env)
        assert e.value.code not in (0, None)
    assert call.calls == [] and env.calls == 0
    assert not Path(runs, "locator").exists()


def test_run_requires_an_explicit_emit_evidence(tmp_path):
    data, runs = _dirs(tmp_path)
    _write_set(data, 1, 0)
    call = FakeCall()
    with pytest.raises(SystemExit) as e:
        _main(["run", "--stage", "fit", "--confirm-count", "4"], data, runs, call=call)
    assert e.value.code not in (0, None)
    assert call.calls == []


# =========================================================================================
# Run files
# =========================================================================================

def test_run_writes_four_files_per_session_with_the_frozen_prompt_sha(tmp_path):
    data, runs = _dirs(tmp_path)
    _write_set(data, 2, 1)
    call, env = FakeCall(), FakeEnv()
    _main(["run", "--stage", "fit", "--emit-evidence", "false", "--confirm-count", "8"],
          data, runs, call=call, load_env=env)
    files = _run_files(runs)
    assert len(files) == 8 and len(call.calls) == 8 and env.calls == 1
    digest = store.manifest_digest(data)
    for (arm, rep, ref), f in files.items():
        assert f["arm"] == arm and f["rep"] == rep and f["ref"] == ref
        assert f["prompt_sha"] == arms.prompt_sha(arm == "B")
        assert f["manifest_digest"] == digest
        assert f["emit_evidence"] is False and f["error"] is None
        assert f["n_segments"] == 1
        assert f["status_evidence_floor_tokens"] == les.STATUS_EVIDENCE_FLOOR_TOKENS
        assert f["evidence_window_sec"] == les.EVIDENCE_WINDOW_SEC
        assert f["usage"] == {"prompt_tokens": 100, "completion_tokens": 10}
        assert isinstance(f["latency_s"], float) and isinstance(f["shown"], list)
    assert arms.prompt_sha(True) != arms.prompt_sha(False)
    # Final tier, the extractor's own token budget, JSON forced.
    for _prompt, kw in call.calls:
        assert kw["enable_thinking"] is True and kw["force_json"] is True
        assert kw["max_tokens"] == les.max_tokens_for(1)


def test_b_topics_carry_support_and_no_said_at_and_a_carry_neither(tmp_path):
    data, runs = _dirs(tmp_path)
    _write_set(data, 1, 0)
    _main(["run", "--stage", "fit", "--emit-evidence", "false", "--confirm-count", "4"],
          data, runs)
    files = _run_files(runs)
    for (arm, _rep, _ref), f in files.items():
        kids = list(_children(f["topics"]))
        assert kids
        for c in kids:
            assert "said_at" not in c
            if arm == "B":
                assert c["support"]["status"] == "linked"
                assert c["support"]["start_sec"] == 0.0
            else:
                assert "support" not in c
        if arm == "B":
            assert f["_raw_said_at"] == [{"item": "0:action_items:0", "said_at": "10:00:00"}]
        else:
            assert "_raw_said_at" not in f


def test_locator_prompt_only_in_b_and_emit_evidence_is_set_then_restored(tmp_path, monkeypatch):
    data, runs = _dirs(tmp_path)
    _write_set(data, 1, 0)
    monkeypatch.setattr(les, "EMIT_EVIDENCE", False)
    seen = []

    def answer(n, prompt):
        seen.append(les.EMIT_EVIDENCE)
        return json.dumps(_payload()), None
    call = FakeCall(answer)
    _main(["run", "--stage", "fit", "--emit-evidence", "true", "--confirm-count", "4"],
          data, runs, call=call)
    assert seen == [True] * 4 and les.EMIT_EVIDENCE is False
    with_locator = [p for p, _kw in call.calls if '"said_at"' in p]
    assert len(with_locator) == 2
    for f in _run_files(runs).values():
        assert f["emit_evidence"] is True and "evidence_error" not in f
        assert f["prompt_sha"] != arms.prompt_sha(f["arm"] == "B")  # sha follows the flag
    monkeypatch.setattr(les, "EMIT_EVIDENCE", True)
    for f in _run_files(runs).values():
        assert f["prompt_sha"] == arms.prompt_sha(f["arm"] == "B")


def test_main_stage_records_how_many_stray_status_suggestions_were_stripped(tmp_path):
    data, runs = _dirs(tmp_path)
    _write_set(data, 1, 0)
    stray = {"status_suggestion": {"status": "done", "quote": "it is fixed", "at": "10:00"}}
    call = FakeCall(lambda n, prompt: (json.dumps(_payload(extra=stray)), None))
    _main(["run", "--stage", "fit", "--emit-evidence", "false", "--confirm-count", "4"],
          data, runs, call=call)
    for f in _run_files(runs).values():
        assert f["status_suggestions_stripped"] == 1
        assert all("status_suggestion" not in c for c in _children(f["topics"]))


def test_an_llm_error_is_recorded_and_the_run_continues(tmp_path):
    data, runs = _dirs(tmp_path)
    _write_set(data, 2, 0)

    def answer(n, prompt):
        return (None, "HTTP 500") if n == 1 else (json.dumps(_payload()), None)
    call = FakeCall(answer)
    _main(["run", "--stage", "fit", "--emit-evidence", "false", "--confirm-count", "8"],
          data, runs, call=call)
    files = _run_files(runs)
    assert len(call.calls) == 8 and len(files) == 8
    errors = [f for f in files.values() if f["error"]]
    assert len(errors) == 1 and errors[0]["topics"] is None
    assert "HTTP 500" in errors[0]["error"]


def test_unparseable_and_malformed_answers_are_errors_not_crashes(tmp_path):
    data, runs = _dirs(tmp_path)
    _write_set(data, 1, 0)
    answers = {1: ("not json at all", None), 2: (json.dumps({"topics": "nope"}), None)}
    call = FakeCall(lambda n, p: answers.get(n, (json.dumps(_payload()), None)))
    _main(["run", "--stage", "fit", "--emit-evidence", "false", "--confirm-count", "4"],
          data, runs, call=call)
    errs = sorted(f["error"] for f in _run_files(runs).values() if f["error"])
    assert len(errs) == 2


def test_an_existing_run_file_is_skipped_on_resume(tmp_path):
    data, runs = _dirs(tmp_path)
    _write_set(data, 2, 0)
    first = FakeCall()
    _main(["run", "--stage", "fit", "--emit-evidence", "false", "--confirm-count", "8"],
          data, runs, call=first)
    victim = Path(runs, "locator", "B", "2", "r001.json")
    victim.unlink()
    assert arms.plan_calls("fit", data_dir=data, runs_dir=runs) == 1
    again = FakeCall()
    _main(["run", "--stage", "fit", "--emit-evidence", "false", "--confirm-count", "1"],
          data, runs, call=again)
    assert len(again.calls) == 1 and victim.exists()
    assert arms.plan_calls("fit", data_dir=data, runs_dir=runs) == 0


def test_dry_run_builds_prompts_and_makes_no_call(tmp_path):
    data, runs = _dirs(tmp_path)
    _write_set(data, 1, 0)
    sessions = store.load_sessions(data, split="fit")
    call = FakeCall()
    out = arms.run_arm(sessions, "B", 1, dry_run=True, call=call, runs_dir=runs,
                       manifest_digest="x", emit_evidence=False)
    assert call.calls == [] and not Path(runs, "locator").exists()
    assert out[0]["prompt_sha"] == arms.prompt_sha(True) and out[0]["prompt_chars"] > 0


def test_n_segments_counts_distinct_source_files_unless_recorded(tmp_path):
    s = store.decode_turn(_record("x", "fit", files=("a.json", "b.json")))
    assert arms.n_segments(s) == 2
    s["n_segments"] = 5
    assert arms.n_segments(s) == 5


# =========================================================================================
# Side set
# =========================================================================================

def _side_prior():
    pid = str(uuid.uuid4())
    published = {"topics": [{"topic_title": "Scaffold", "action_items": [
        {"action": "Fix the scaffold on level two", "item_id": pid}]}]}
    return pid, item_continuity.prior_items(published)


def test_side_run_uses_the_status_block_and_records_suggestion_counts(tmp_path):
    data, runs = _dirs(tmp_path)
    _write_set(data, 0, 0, 1)
    pid, prior = _side_prior()
    p = Path(data, "side", "sessions", "s000.json")
    rec = store.read_json(p)
    rec["prior"] = [x._asdict() for x in prior]
    store.write_json(p, rec)
    suggestion = {"status_suggestion": {"status": "done", "at": "10:00:00",
                                        "quote": LINES[0]}}
    call = FakeCall(lambda n, prompt: (json.dumps(_payload(extra=suggestion)), None))
    _main(["run", "--stage", "side", "--emit-evidence", "false", "--confirm-count", "4"],
          data, runs, call=call)
    block = item_continuity.render_block(prior, status_suggestions.INSTRUCTION)
    assert all(block in prompt for prompt, _kw in call.calls)
    files = _run_files(runs, side=True)
    assert len(files) == 4 and not _run_files(runs)
    for f in files.values():
        assert f["status_suggestions"] == {"offered": 1, "kept": 1, "dropped_by": {}}
        kid = next(_children(f["topics"]))
        assert kid["item_id"] == pid and kid["status_suggestion"]["status"] == "done"
        assert "said_at" not in kid
        assert f["manifest_digest"] == arms.side_digest(data)


def test_side_suggestion_on_an_uncarried_item_is_dropped(tmp_path):
    data, runs = _dirs(tmp_path)
    _write_set(data, 0, 0, 1)
    _pid, prior = _side_prior()
    p = Path(data, "side", "sessions", "s000.json")
    rec = store.read_json(p)
    rec["prior"] = [x._asdict() for x in prior]
    store.write_json(p, rec)
    payload = _payload(extra={"status_suggestion": {"status": "done", "at": "10:00:00",
                                                    "quote": LINES[0]}})
    payload["topics"][0]["action_items"][0]["action"] = "Something entirely different here"
    call = FakeCall(lambda n, prompt: (json.dumps(payload), None))
    _main(["run", "--stage", "side", "--emit-evidence", "false", "--confirm-count", "4"],
          data, runs, call=call)
    for f in _run_files(runs, side=True).values():
        assert f["status_suggestions"]["kept"] == 0
        assert f["status_suggestions"]["dropped_by"] == {"not_carried": 1}


# =========================================================================================
# Side build (fake S3)
# =========================================================================================

class _FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(json.dumps(self.objects[Key]).encode("utf-8"))}

    def get_paginator(self, op):
        objects = self.objects

        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in objects if k.startswith(Prefix)]}
        return P()


def _side_objects(extra_keys=()):
    pid = str(uuid.uuid4())
    final = {"tier": "final", "topics": [{"topic_title": "T", "action_items": [
        {"action": "Fix the scaffold on level two", "item_id": pid}]}],
        "status_suggestions": {"offered": 2, "kept": 1, "dropped_by": {"weak": 1}},
        "continuity": {"question_set": "item_continuity:abc"}}
    objs = {}
    for date, prefix in build.SIDE_SESSIONS:
        objs[f"extractions/{build.SIDE_FOLDER}/{date}/{prefix}00aa.json"] = final
    for k in extra_keys:
        objs[k] = final
    return objs, pid


def test_build_side_resolves_prefixes_and_stores_prior_and_turns(tmp_path, monkeypatch):
    data, _runs = _dirs(tmp_path)
    objs, pid = _side_objects()
    seen = []

    def gather(bucket, folder, date, base):
        seen.append((folder, date, base))
        return [f"transcripts/{folder}/{date}/{base}_seg.json"]

    def assemble(bucket, keys):
        return _turns(), ["seg_a.json", "seg_b.json"], {}
    monkeypatch.setattr(les, "gather_session_segments", gather)
    monkeypatch.setattr(les, "assemble_session_turns", assemble)
    out = build.build_side(out_dir=data, s3c=_FakeS3(objs))
    assert out["sessions"] == 6 and len(seen) == 6
    recs = [store.load_session(p) for p in sorted(Path(data, "side", "sessions").glob("*.json"))]
    assert len(recs) == 6
    for r in recs:
        assert r["split"] == "side" and r["n_segments"] == 2
        assert r["prior"][0]["item_id"] == pid
        assert r["published_status_suggestions"]["kept"] == 1
        assert r["published_question_set"] == "item_continuity:abc"
        assert isinstance(r["turns"][0]["abs_start"], datetime)
    assert Path(data, "side", "manifest.json").exists()


def test_build_side_refuses_an_ambiguous_prefix(tmp_path, monkeypatch):
    data, _runs = _dirs(tmp_path)
    date, prefix = build.SIDE_SESSIONS[0]
    objs, _pid = _side_objects(
        extra_keys=[f"extractions/{build.SIDE_FOLDER}/{date}/{prefix}11bb.json"])
    monkeypatch.setattr(les, "gather_session_segments", lambda *a: ["k"])
    monkeypatch.setattr(les, "assemble_session_turns", lambda b, k: (_turns(), ["a"], {}))
    with pytest.raises(SystemExit):
        build.build_side(out_dir=data, s3c=_FakeS3(objs))
    assert not Path(data, "side").exists()
