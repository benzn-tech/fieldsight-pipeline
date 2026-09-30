"""Tests for scripts/continuity_eval/label.py (Task 11): blind assignment export, the agent
annotator's instruction text, merging labelled files, and owner-adjudication routing.

Pure filesystem + JSON: no AWS, no LLM, no Postgres. Run directories are hand-built here in
the exact shape `scripts/continuity_eval/run.py` writes (spec/Task 10), so a schema drift
between the two would show up as a KeyError here, not a silent gap.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from scripts.continuity_eval import label


# ---------------------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------------------

_FIELD = {"action_items": "action", "findings": "observation",
          "decisions": "decision", "questions": "question"}


def _child(list_name, text, item_id=None):
    d = {_FIELD[list_name]: text}
    if item_id is not None:
        d["item_id"] = item_id
    return d


def _write_step(run_dir, shape, arm, rep, session, step_idx, *, topics, claims=None,
                 void=False, transcript_text="[00:00] spk_0: placeholder transcript"):
    d = Path(run_dir) / "runs" / shape / arm / str(rep) / session
    d.mkdir(parents=True, exist_ok=True)
    record = {"prompt_has_block": arm == "with_block" and step_idx > 0, "prior_count": 0,
              "extraction": {"topics": topics}, "claims": claims or [], "void": void,
              "error": None, "latency_ms": 1000, "n_segments": 3, "frac": 1.0,
              "transcript_text": transcript_text}
    (d / f"{step_idx}.json").write_text(json.dumps(record), encoding="utf-8")


def _write_sessions_json(run_dir, sessions):
    (Path(run_dir)).mkdir(parents=True, exist_ok=True)
    (Path(run_dir) / "sessions.json").write_text(json.dumps(sessions), encoding="utf-8")


def _two_step_chain(run_dir, shape, arm, rep, session, prior_id, new_id, claims=None):
    _write_step(run_dir, shape, arm, rep, session, 0,
                topics=[{"action_items": [_child("action_items", "Fix the pump", prior_id)]}])
    _write_step(run_dir, shape, arm, rep, session, 1,
                topics=[{"action_items": [_child("action_items", "Pump fixed today", new_id)]}],
                claims=claims or [])


# ---------------------------------------------------------------------------------------
# BLIND_LABEL_INSTRUCTION / prompt_for_agent
# ---------------------------------------------------------------------------------------

def test_blind_instruction_never_mentions_claims_the_model_or_which_arm():
    text = label.BLIND_LABEL_INSTRUCTION.lower()
    for banned in ("claim", "the model", "continues", "with_block", "baseline", "extraction"):
        assert banned not in text, banned


def test_prompt_for_agent_shows_only_prior_list_and_new_item_text():
    line = {"prior_list": [{"alias": "A1", "kind": "action_item", "text": "fix the pump"}],
            "new_item_text": "the pump was fixed", "arm": "with_block",
            "model_claim_alias": "F9", "model_claim_outcome": "accepted"}
    prompt = label.prompt_for_agent(line)
    assert "fix the pump" in prompt
    assert "the pump was fixed" in prompt
    assert label.BLIND_LABEL_INSTRUCTION in prompt
    # Fields a todo line should never carry into the rendered text, even if present on the
    # dict (e.g. because the caller passed a full assignment record by mistake).
    assert "with_block" not in prompt
    assert "F9" not in prompt
    assert "accepted" not in prompt


def test_prompt_for_agent_with_no_prior_items_says_so_instead_of_a_blank_section():
    line = {"prior_list": [], "new_item_text": "brand new item"}
    prompt = label.prompt_for_agent(line)
    assert "no earlier items" in prompt.lower()


# ---------------------------------------------------------------------------------------
# iter_assignments / export
# ---------------------------------------------------------------------------------------

def test_iter_assignments_skips_the_first_step_of_a_chain(tmp_path):
    prior_id = str(uuid.uuid4())
    _two_step_chain(tmp_path, "a", "with_block", 1, "sess__2026-01-01__base",
                     prior_id, prior_id)
    assignments = list(label.iter_assignments(tmp_path))
    assert len(assignments) == 1
    assert assignments[0]["step"] == 1


def test_iter_assignments_skips_void_steps(tmp_path):
    prior_id = str(uuid.uuid4())
    new_id = str(uuid.uuid4())
    _write_step(tmp_path, "a", "with_block", 1, "sess", 0,
                topics=[{"action_items": [_child("action_items", "Fix the pump", prior_id)]}])
    _write_step(tmp_path, "a", "with_block", 1, "sess", 1,
                topics=[{"action_items": [_child("action_items", "Pump fixed", new_id)]}],
                void=True)
    assert list(label.iter_assignments(tmp_path)) == []


def test_iter_assignments_never_exposes_a_claim_field_to_the_public_shape(tmp_path):
    prior_id = str(uuid.uuid4())
    claim = {"alias": "A1", "prior_item_id": prior_id, "new_item_id": prior_id,
              "outcome": "accepted", "guard": None, "list_name": "action_items"}
    _two_step_chain(tmp_path, "a", "with_block", 1, "sess", prior_id, prior_id, claims=[claim])
    [a] = list(label.iter_assignments(tmp_path))
    assert a["model_claim_alias"] == "A1"          # private field, present for adjudication
    for field in label._PUBLIC_FIELDS:
        assert field in a
    # export() must copy ONLY the public fields -- checked directly below, this just pins
    # that the private fields exist under names outside that whitelist.
    assert "model_claim_alias" not in label._PUBLIC_FIELDS
    assert "model_claim_outcome" not in label._PUBLIC_FIELDS


def test_export_routes_test_sessions_to_the_agent_file_regardless_of_prod_labeller(tmp_path):
    prior_id = str(uuid.uuid4())
    session = "user__2026-01-01__base"
    _two_step_chain(tmp_path, "a", "with_block", 1, session, prior_id, prior_id)
    _write_sessions_json(tmp_path, [{"env": "test", "user_folder": "user",
                                      "date": "2026-01-01", "session_base": "base"}])
    result = label.export(tmp_path, tmp_path / "continuity_eval_runs" / "out", prod_labeller="agent")
    assert result["agent_count"] == 1
    assert result["owner_count"] == 0


def test_export_routes_prod_sessions_to_owner_by_default(tmp_path):
    prior_id = str(uuid.uuid4())
    session = "user__2026-01-01__base"
    _two_step_chain(tmp_path, "a", "with_block", 1, session, prior_id, prior_id)
    _write_sessions_json(tmp_path, [{"env": "prod", "user_folder": "user",
                                      "date": "2026-01-01", "session_base": "base"}])
    result = label.export(tmp_path, tmp_path / "continuity_eval_runs" / "out")   # default prod_labeller="owner"
    assert result["agent_count"] == 0
    assert result["owner_count"] == 1


def test_export_routes_prod_sessions_to_agent_when_prod_labeller_is_agent(tmp_path):
    prior_id = str(uuid.uuid4())
    session = "user__2026-01-01__base"
    _two_step_chain(tmp_path, "a", "with_block", 1, session, prior_id, prior_id)
    _write_sessions_json(tmp_path, [{"env": "prod", "user_folder": "user",
                                      "date": "2026-01-01", "session_base": "base"}])
    result = label.export(tmp_path, tmp_path / "continuity_eval_runs" / "out", prod_labeller="agent")
    assert result["agent_count"] == 1
    assert result["owner_count"] == 0


def test_export_rejects_an_unknown_prod_labeller(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        label.export(tmp_path, tmp_path / "continuity_eval_runs" / "out", prod_labeller="nobody")


def test_export_written_lines_carry_no_claim_field(tmp_path):
    prior_id = str(uuid.uuid4())
    claim = {"alias": "A1", "prior_item_id": prior_id, "new_item_id": prior_id,
              "outcome": "accepted", "guard": None, "list_name": "action_items"}
    session = "user__2026-01-01__base"
    _two_step_chain(tmp_path, "a", "with_block", 1, session, prior_id, prior_id, claims=[claim])
    _write_sessions_json(tmp_path, [{"env": "test", "user_folder": "user",
                                      "date": "2026-01-01", "session_base": "base"}])
    label.export(tmp_path, tmp_path / "continuity_eval_runs" / "out")
    lines = list(label._read_jsonl(tmp_path / "continuity_eval_runs" / "out" / "assignments_todo.agent.jsonl"))
    assert len(lines) == 1
    assert "model_claim_alias" not in lines[0]
    assert "model_claim_outcome" not in lines[0]
    assert set(lines[0]) == set(label._PUBLIC_FIELDS)


# ---------------------------------------------------------------------------------------
# import_labels
# ---------------------------------------------------------------------------------------

def test_import_labels_owner_wins_over_agent_for_the_same_assignment(tmp_path):
    agent_path = tmp_path / "agent.jsonl"
    owner_path = tmp_path / "owner.jsonl"
    label._write_jsonl(agent_path, [
        {"assignment_id": "x1", "counterpart": "A1", "supported": True, "labeller": "agent"}])
    label._write_jsonl(owner_path, [
        {"assignment_id": "x1", "counterpart": "none", "supported": False, "labeller": "owner"}])
    merged = label.import_labels([agent_path, owner_path], tmp_path / "continuity_eval_runs" / "done.jsonl")
    assert len(merged) == 1
    assert merged[0]["labeller"] == "owner"
    assert merged[0]["counterpart"] == "none"


def test_import_labels_owner_wins_regardless_of_file_order(tmp_path):
    agent_path = tmp_path / "agent.jsonl"
    owner_path = tmp_path / "owner.jsonl"
    label._write_jsonl(agent_path, [
        {"assignment_id": "x1", "counterpart": "A1", "supported": True, "labeller": "agent"}])
    label._write_jsonl(owner_path, [
        {"assignment_id": "x1", "counterpart": "none", "supported": False, "labeller": "owner"}])
    merged = label.import_labels([owner_path, agent_path], tmp_path / "continuity_eval_runs" / "done.jsonl")
    assert merged[0]["labeller"] == "owner"


def test_import_labels_keeps_unrelated_assignments_from_both_files(tmp_path):
    agent_path = tmp_path / "agent.jsonl"
    owner_path = tmp_path / "owner.jsonl"
    label._write_jsonl(agent_path, [
        {"assignment_id": "x1", "counterpart": "A1", "supported": True, "labeller": "agent"}])
    label._write_jsonl(owner_path, [
        {"assignment_id": "x2", "counterpart": "none", "supported": True, "labeller": "owner"}])
    merged = label.import_labels([agent_path, owner_path], tmp_path / "continuity_eval_runs" / "done.jsonl")
    assert {r["assignment_id"] for r in merged} == {"x1", "x2"}


# ---------------------------------------------------------------------------------------
# build_adjudication_todo
# ---------------------------------------------------------------------------------------

def test_adjudication_todo_includes_accepted_claims_the_agent_labelled_different(tmp_path):
    prior_id = str(uuid.uuid4())
    session = "user__2026-01-01__base"
    claim = {"alias": "A1", "prior_item_id": prior_id, "new_item_id": prior_id,
              "outcome": "accepted", "guard": None, "list_name": "action_items"}
    _two_step_chain(tmp_path, "a", "with_block", 1, session, prior_id, prior_id, claims=[claim])
    agent_done = tmp_path / "agent_done.jsonl"
    [assignment] = list(label.iter_assignments(tmp_path))
    label._write_jsonl(agent_done, [
        {"assignment_id": assignment["assignment_id"], "counterpart": "none",
         "supported": True, "labeller": "agent"}])
    rows = label.build_adjudication_todo(tmp_path, agent_done, tmp_path / "continuity_eval_runs" / "adj.jsonl",
                                           sample_rate=0.0)
    assert len(rows) == 1
    assert rows[0]["reason"] == "claim_disagreement"
    assert rows[0]["model_claimed_alias"] == "A1"
    assert rows[0]["agent_counterpart"] == "none"


def test_adjudication_todo_does_not_flag_agreement_as_a_disagreement(tmp_path):
    prior_id = str(uuid.uuid4())
    session = "user__2026-01-01__base"
    claim = {"alias": "A1", "prior_item_id": prior_id, "new_item_id": prior_id,
              "outcome": "accepted", "guard": None, "list_name": "action_items"}
    _two_step_chain(tmp_path, "a", "with_block", 1, session, prior_id, prior_id, claims=[claim])
    agent_done = tmp_path / "agent_done.jsonl"
    [assignment] = list(label.iter_assignments(tmp_path))
    label._write_jsonl(agent_done, [
        {"assignment_id": assignment["assignment_id"], "counterpart": "A1",
         "supported": True, "labeller": "agent"}])
    rows = label.build_adjudication_todo(tmp_path, agent_done, tmp_path / "continuity_eval_runs" / "adj.jsonl",
                                           sample_rate=0.0)
    assert rows == []


def test_adjudication_todo_sample_is_deterministic_and_does_not_double_count(tmp_path):
    # Ten assignments, none claimed (so no disagreement path fires); a 30% sample should
    # take 3, and running it twice with the same seed gives the same 3.
    session = "user__2026-01-01__base"
    for i in range(10):
        item_id = str(uuid.uuid4())
        _write_step(tmp_path, "a", "with_block", 1, f"{session}{i}", 0,
                    topics=[{"action_items": [_child("action_items", f"item {i}", item_id)]}])
        _write_step(tmp_path, "a", "with_block", 1, f"{session}{i}", 1,
                    topics=[{"action_items": [_child("action_items", f"item {i} v2",
                                                        str(uuid.uuid4()))]}])
    agent_done = tmp_path / "agent_done.jsonl"
    assignments = list(label.iter_assignments(tmp_path))
    assert len(assignments) == 10
    label._write_jsonl(agent_done, [
        {"assignment_id": a["assignment_id"], "counterpart": "none", "supported": True,
         "labeller": "agent"} for a in assignments])
    rows1 = label.build_adjudication_todo(tmp_path, agent_done, tmp_path / "continuity_eval_runs" / "adj1.jsonl",
                                            sample_rate=0.3, seed=7)
    rows2 = label.build_adjudication_todo(tmp_path, agent_done, tmp_path / "continuity_eval_runs" / "adj2.jsonl",
                                            sample_rate=0.3, seed=7)
    assert len(rows1) == 3
    assert [r["assignment_id"] for r in rows1] == [r["assignment_id"] for r in rows2]
    assert len({r["assignment_id"] for r in rows1}) == 3   # no double counting


# =========================================================================================
# Fix round 1: guard capture, transcript evidence, hard-negative elicitation (controller
# review of Task 11)
# =========================================================================================

def test_blind_instruction_asks_for_hard_negatives_with_an_example():
    text = label.BLIND_LABEL_INSTRUCTION.lower()
    assert "hard negative" in text
    assert "for example" in text


def test_iter_assignments_captures_the_real_claim_guard(tmp_path):
    prior_id = str(uuid.uuid4())
    new_id = str(uuid.uuid4())
    claim = {"alias": "A1", "prior_item_id": prior_id, "new_item_id": new_id,
              "outcome": "rejected", "guard": "echo", "list_name": "action_items"}
    _write_step(tmp_path, "a", "with_block", 1, "sess", 0,
                topics=[{"action_items": [_child("action_items", "Fix the pump", prior_id)]}])
    _write_step(tmp_path, "a", "with_block", 1, "sess", 1,
                topics=[{"action_items": [_child("action_items", "Pump task rework", new_id)]}],
                claims=[claim])
    [a] = list(label.iter_assignments(tmp_path))
    assert a["model_claim_guard"] == "echo"
    assert "model_claim_guard" not in label._PUBLIC_FIELDS


def test_prompt_for_agent_includes_transcript_text_and_still_no_claim_or_arm_info():
    line = {"prior_list": [{"alias": "A1", "kind": "action_item", "text": "fix the pump"}],
            "new_item_text": "the pump was fixed", "arm": "with_block",
            "transcript_text": "[00:00] spk_0: someone said fix the pump please",
            "model_claim_alias": "F9", "model_claim_outcome": "accepted",
            "model_claim_guard": "echo"}
    prompt = label.prompt_for_agent(line)
    assert "someone said fix the pump please" in prompt
    assert "with_block" not in prompt
    assert "F9" not in prompt
    assert "accepted" not in prompt
    assert "echo" not in prompt


def test_prompt_for_agent_reads_the_transcript_from_a_side_file(tmp_path):
    transcripts_dir = tmp_path / "transcripts"
    step_dir = transcripts_dir / "a" / "with_block" / "1" / "sess"
    step_dir.mkdir(parents=True)
    (step_dir / "1.txt").write_text("[00:00] spk_0: the real transcript line",
                                     encoding="utf-8")
    line = {"prior_list": [], "new_item_text": "new item",
            "transcript_ref": "a/with_block/1/sess/1.txt"}
    prompt = label.prompt_for_agent(line, transcripts_dir=transcripts_dir)
    assert "the real transcript line" in prompt


def test_prompt_for_agent_says_unavailable_with_no_transcript_source():
    prompt = label.prompt_for_agent({"prior_list": [], "new_item_text": "x"})
    assert "unavailable" in prompt.lower()


def test_export_writes_one_transcript_file_shared_by_every_new_item_in_a_step(tmp_path):
    prior_id = str(uuid.uuid4())
    session = "user__2026-01-01__base"
    _write_step(tmp_path, "a", "with_block", 1, session, 0,
                topics=[{"action_items": [_child("action_items", "Fix the pump", prior_id)]}],
                transcript_text="[00:00] spk_0: step 0 transcript")
    _write_step(tmp_path, "a", "with_block", 1, session, 1,
                topics=[{"action_items": [_child("action_items", "Pump fixed", str(uuid.uuid4())),
                                            _child("action_items", "Second item",
                                                    str(uuid.uuid4()))]}],
                transcript_text="[00:00] spk_0: step 1 transcript, real evidence")
    _write_sessions_json(tmp_path, [{"env": "test", "user_folder": "user",
                                      "date": "2026-01-01", "session_base": "base"}])
    result = label.export(tmp_path, tmp_path / "continuity_eval_runs" / "out")
    lines = list(label._read_jsonl(tmp_path / "continuity_eval_runs" / "out" / "assignments_todo.agent.jsonl"))
    assert len(lines) == 2
    refs = {line["transcript_ref"] for line in lines}
    assert len(refs) == 1                                    # both new items share one step
    [ref] = refs
    transcript_path = Path(result["transcripts_dir"]) / ref
    assert transcript_path.exists()
    assert transcript_path.read_text(encoding="utf-8") == \
        "[00:00] spk_0: step 1 transcript, real evidence"


def test_adjudication_row_carries_the_same_transcript_ref(tmp_path):
    prior_id = str(uuid.uuid4())
    session = "user__2026-01-01__base"
    claim = {"alias": "A1", "prior_item_id": prior_id, "new_item_id": prior_id,
              "outcome": "accepted", "guard": None, "list_name": "action_items"}
    _two_step_chain(tmp_path, "a", "with_block", 1, session, prior_id, prior_id, claims=[claim])
    agent_done = tmp_path / "agent_done.jsonl"
    [assignment] = list(label.iter_assignments(tmp_path))
    label._write_jsonl(agent_done, [
        {"assignment_id": assignment["assignment_id"], "counterpart": "none",
         "supported": True, "labeller": "agent"}])
    [row] = label.build_adjudication_todo(tmp_path, agent_done, tmp_path / "continuity_eval_runs" / "adj.jsonl",
                                            sample_rate=0.0)
    assert row["transcript_ref"] == assignment["transcript_ref"]
