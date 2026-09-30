"""Tests for scripts/continuity_eval (Task 10): the measurement harness's pure parts.

No AWS, no network: `run.run_chain` is driven with `lambda_extract_session` and
`llm_utils` functions monkeypatched at the module attribute the harness actually
calls through (`les.build_extraction_prompt`, `les.gather_session_segments`,
`les.assemble_session_turns`, `llm_utils.call_llm`), so a test can never pass by
accident from a real S3/LLM call succeeding. `sessions.list_candidate_sessions` is
exercised against a fake S3-client-and-paginator double plus a fake `gather`, never
a real boto3 client. `run.build_counts`/`load_deployed_llm_env` (real S3/aws-CLI I/O)
are out of scope here -- covered, if at all, by the integration seam another task
owns.
"""
from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")

import item_continuity
from scripts.continuity_eval import run as run_mod
from scripts.continuity_eval import sessions as sessions_mod


# ---------------------------------------------------------------------------
# prefix_segments
# ---------------------------------------------------------------------------

def test_prefix_segments_returns_first_ceil_frac_n_keys_in_time_order():
    keys = [f"k{i}" for i in range(5)]  # already in time order, like gather_session_segments
    assert sessions_mod.prefix_segments(keys, 0.4) == ["k0", "k1"]           # ceil(2.0) = 2
    assert sessions_mod.prefix_segments(keys, 0.41) == ["k0", "k1", "k2"]    # ceil(2.05) = 3
    assert sessions_mod.prefix_segments(keys, 1.0) == keys


def test_prefix_segments_never_returns_more_than_n_keys():
    keys = [f"k{i}" for i in range(3)]
    assert sessions_mod.prefix_segments(keys, 1.0) == keys


def test_prefix_segments_one_segment_at_a_small_fraction():
    keys = [f"k{i}" for i in range(100)]
    assert sessions_mod.prefix_segments(keys, 0.01) == ["k0"]  # ceil(1.0) = 1


# ---------------------------------------------------------------------------
# shapes
# ---------------------------------------------------------------------------

def test_shapes_a_is_live_at_95_then_final():
    assert sessions_mod.shapes()["a"] == [(0.95, 1.0)]


def test_shapes_b_is_the_four_way_chain():
    assert sessions_mod.shapes()["b"] == [(0.4, 0.6), (0.6, 0.8), (0.8, 1.0)]


def test_fraction_sequence_matches_the_spec_percentages():
    assert sessions_mod.fraction_sequence(sessions_mod.shapes()["a"]) == [0.95, 1.0]
    assert sessions_mod.fraction_sequence(sessions_mod.shapes()["b"]) == [0.4, 0.6, 0.8, 1.0]


# ---------------------------------------------------------------------------
# list_candidate_sessions: fake S3 paginator + fake gather, never real AWS.
# ---------------------------------------------------------------------------

class _FakePage:
    def __init__(self, keys):
        self._keys = keys

    def get(self, name, default=None):
        assert name == "Contents"
        return [{"Key": k} for k in self._keys]


class _FakePaginator:
    def __init__(self, keys):
        self._keys = keys

    def paginate(self, Bucket, Prefix):
        matched = [k for k in self._keys if k.startswith(Prefix)]
        yield _FakePage(matched)


class _FakeS3Client:
    def __init__(self, keys):
        self._keys = keys

    def get_paginator(self, op):
        assert op == "list_objects_v2"
        return _FakePaginator(self._keys)


def test_list_candidate_sessions_reads_the_extractions_prefix_and_filters_by_segment_count():
    keys = [
        "extractions/worker1/2026-09-01/sess1.json",
        "extractions/worker1/2026-09-02/sess2.json",
        "extractions/worker1/2026-09-03/sess3.json",
        "transcripts/worker1/2026-09-01/sess1_seg0.json",  # must be ignored (wrong prefix)
    ]
    client = _FakeS3Client(keys)
    segment_counts = {"sess1": 5, "sess2": 2, "sess3": 3}  # sess2 below the min_segments=3 floor

    def fake_gather(bucket, user_folder, date, session_base):
        return ["k"] * segment_counts[session_base]

    result = sessions_mod.list_candidate_sessions("test", client=client, gather=fake_gather)

    assert [s["session_base"] for s in result] == ["sess1", "sess3"]
    assert all(s["env"] == "test" for s in result)
    found = {s["session_base"]: s["n_segments"] for s in result}
    assert found == {"sess1": 5, "sess3": 3}


def test_list_candidate_sessions_deduplicates_a_key_seen_twice():
    keys = ["extractions/worker1/2026-09-01/sess1.json"] * 2
    client = _FakeS3Client(keys)
    calls = []

    def fake_gather(bucket, user_folder, date, session_base):
        calls.append(session_base)
        return ["k", "k", "k"]

    result = sessions_mod.list_candidate_sessions("test", client=client, gather=fake_gather)
    assert len(result) == 1
    assert calls == ["sess1"]  # gather is only ever called once per distinct session


# ---------------------------------------------------------------------------
# void
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("run,expected", [
    ({"arm": "with_block", "prior_count": 0, "prompt_has_block": True}, True),
    ({"arm": "with_block", "prior_count": 5, "prompt_has_block": False}, True),
    ({"arm": "with_block", "prior_count": 0, "prompt_has_block": False}, True),
    ({"arm": "with_block", "prior_count": 5, "prompt_has_block": True}, False),
    ({"arm": "baseline", "prior_count": 0, "prompt_has_block": False}, False),
    ({"arm": "baseline", "prior_count": 5, "prompt_has_block": True}, False),
])
def test_void(run, expected):
    assert run_mod.void(run) is expected


# ---------------------------------------------------------------------------
# run_chain: real prompt builder, real item_continuity, LLM/gather stubbed at the
# harness's own call sites.
# ---------------------------------------------------------------------------

SESSION = {"env": "test", "user_folder": "worker1", "date": "2026-09-01",
           "session_base": "sess1", "n_segments": 10}


def _fake_gather(bucket, user_folder, date, session_base):
    return [f"transcripts/{user_folder}/{date}/seg{i}.json" for i in range(10)]


def _fake_assemble(bucket, keys):
    # Shaped like real turns (`abs_start_str`/`speaker`/`source_filename`/`text`) because
    # `run.py` now calls the REAL `les.render_transcript` on these turns directly (to persist
    # the transcript text on the step record for Task 11's gold labelling), not just the
    # mocked `build_extraction_prompt`.
    turns = [{"abs_start": i, "abs_start_str": f"00:00:{i:02d}", "speaker": "spk_0",
              "source_filename": k.rsplit("/", 1)[-1], "text": f"turn {i}"}
             for i, k in enumerate(keys)]
    filenames = [k.rsplit("/", 1)[-1] for k in keys]
    return turns, filenames, {}


def _install_gather_and_assemble(monkeypatch):
    monkeypatch.setattr(run_mod.les, "gather_session_segments", _fake_gather)
    monkeypatch.setattr(run_mod.les, "assemble_session_turns", _fake_assemble)


def test_shape_a_first_step_gets_no_continuity_block_even_on_with_block_arm(monkeypatch, tmp_path):
    tmp_path = tmp_path / "continuity_eval_runs"  # run_chain refuses to write elsewhere
    _install_gather_and_assemble(monkeypatch)
    calls = []

    def fake_build(user_folder, date, session_base, turns, n_segments, speaker_names=None,
                    continuity_block=""):
        calls.append(continuity_block)
        return f"PROMPT<<<{continuity_block}>>>", {"truncated": False, "lines_omitted": 0}
    monkeypatch.setattr(run_mod.les, "build_extraction_prompt", fake_build)

    def fake_call_llm(prompt, **kw):
        return json.dumps({"topics": [{"action_items": [{"action": "Fix the pump"}]}]}), None
    monkeypatch.setattr(run_mod.llm_utils, "call_llm", fake_call_llm)

    records = run_mod.run_chain("bucket", SESSION, "a", sessions_mod.shapes()["a"],
                                 "with_block", 1, tmp_path)

    assert len(records) == 2  # shape a: fractions [0.95, 1.0]
    assert calls[0] == ""
    assert records[0]["prior_count"] == 0
    assert records[0]["prompt_has_block"] is False
    assert records[0]["void"] is True  # with_block + prior_count 0 -> void, per the shared rule


def test_the_prompt_used_is_produced_by_the_real_build_extraction_prompt(monkeypatch, tmp_path):
    """The harness never re-types the prompt: it must call
    lambda_extract_session.build_extraction_prompt, and the second step of a
    with-block chain must pass it exactly item_continuity.render_block's own output
    for the prior items carried from the first step's resolved extraction."""
    tmp_path = tmp_path / "continuity_eval_runs"  # run_chain refuses to write elsewhere
    _install_gather_and_assemble(monkeypatch)
    calls = []

    def spying_build(user_folder, date, session_base, turns, n_segments, speaker_names=None,
                      continuity_block=""):
        calls.append(continuity_block)
        return f"PROMPT<<<{continuity_block}>>>", {"truncated": False, "lines_omitted": 0}
    monkeypatch.setattr(run_mod.les, "build_extraction_prompt", spying_build)

    responses = iter([
        json.dumps({"topics": [{"action_items": [{"action": "Fix the leaking pump on level 2"}]}]}),
        json.dumps({"topics": [{"action_items": [
            {"action": "Fix the leaking pump on level 2, confirmed done",
             "continues": {"id": "A1", "starts": "Fix the leaking pump"}}]}]}),
    ])

    def fake_call_llm(prompt, **kw):
        return next(responses), None
    monkeypatch.setattr(run_mod.llm_utils, "call_llm", fake_call_llm)

    records = run_mod.run_chain("bucket", SESSION, "a", sessions_mod.shapes()["a"],
                                 "with_block", 1, tmp_path)

    assert len(records) == 2
    # Step 1 (index 0): first pass of the chain, no prior available -- empty block.
    assert calls[0] == ""

    # Step 2 (index 1): the harness must build the SAME prior list item_continuity
    # itself would build from step 1's resolved topics, and render it with the real
    # render_block -- not a hand-rolled string.
    first_topics = records[0]["extraction"]["topics"]
    expected_prior = item_continuity.prior_items({"topics": first_topics})
    expected_block = item_continuity.render_block(expected_prior)
    assert expected_block  # sanity: step 1 actually produced a carryable item
    assert calls[1] == expected_block

    assert records[1]["prior_count"] == len(expected_prior)
    assert records[1]["prompt_has_block"] is True
    assert records[1]["void"] is False

    # The claim was accepted (echo passes, kind matches, one-to-one): the second
    # item's item_id must equal the first item's, via the real item_continuity.resolve.
    second_topics = records[1]["extraction"]["topics"]
    first_id = first_topics[0]["action_items"][0]["item_id"]
    second_id = second_topics[0]["action_items"][0]["item_id"]
    assert second_id == first_id
    assert records[1]["claims"] and records[1]["claims"][0]["outcome"] == "accepted"


def test_baseline_arm_never_gets_a_continuity_block(monkeypatch, tmp_path):
    tmp_path = tmp_path / "continuity_eval_runs"  # run_chain refuses to write elsewhere
    _install_gather_and_assemble(monkeypatch)
    calls = []

    def spying_build(user_folder, date, session_base, turns, n_segments, speaker_names=None,
                      continuity_block=""):
        calls.append(continuity_block)
        return "PROMPT", {"truncated": False, "lines_omitted": 0}
    monkeypatch.setattr(run_mod.les, "build_extraction_prompt", spying_build)

    def fake_call_llm(prompt, **kw):
        return json.dumps({"topics": [{"action_items": [{"action": "Fix the pump"}]}]}), None
    monkeypatch.setattr(run_mod.llm_utils, "call_llm", fake_call_llm)

    records = run_mod.run_chain("bucket", SESSION, "b", sessions_mod.shapes()["b"],
                                 "baseline", 1, tmp_path)

    assert len(records) == 4  # shape b: fractions [0.4, 0.6, 0.8, 1.0]
    assert calls == [""] * 4
    assert all(r["prior_count"] == 0 for r in records)
    assert all(r["void"] is False for r in records)  # baseline is never void


def test_run_chain_writes_one_step_file_per_pass(monkeypatch, tmp_path):
    tmp_path = tmp_path / "continuity_eval_runs"  # run_chain refuses to write elsewhere
    _install_gather_and_assemble(monkeypatch)
    monkeypatch.setattr(
        run_mod.les, "build_extraction_prompt",
        lambda *a, **k: ("PROMPT", {"truncated": False, "lines_omitted": 0}))
    monkeypatch.setattr(
        run_mod.llm_utils, "call_llm",
        lambda prompt, **kw: (json.dumps({"topics": []}), None))

    run_mod.run_chain("bucket", SESSION, "b", sessions_mod.shapes()["b"], "baseline", 2, tmp_path)

    session_dir = (tmp_path / "runs" / "b" / "baseline" / "2" / run_mod._session_key(SESSION))
    written = sorted(p.name for p in session_dir.glob("*.json"))
    assert written == ["0.json", "1.json", "2.json", "3.json"]
    body = json.loads((session_dir / "0.json").read_text(encoding="utf-8"))
    assert set(body) >= {"prompt_has_block", "prior_count", "extraction", "claims",
                          "transcript_text"}
    # Persisted for Task 11's gold labelling (the "supported by the transcript" judgement
    # needs real evidence, not nothing) -- rendered by the real `les.render_transcript`, the
    # same function `build_extraction_prompt` calls internally, not re-derived here.
    assert "turn 0" in body["transcript_text"]


def test_run_chain_records_llm_call_error_without_raising(monkeypatch, tmp_path):
    tmp_path = tmp_path / "continuity_eval_runs"  # run_chain refuses to write elsewhere
    _install_gather_and_assemble(monkeypatch)
    monkeypatch.setattr(
        run_mod.les, "build_extraction_prompt",
        lambda *a, **k: ("PROMPT", {"truncated": False, "lines_omitted": 0}))
    monkeypatch.setattr(run_mod.llm_utils, "call_llm", lambda prompt, **kw: (None, "boom"))

    records = run_mod.run_chain("bucket", SESSION, "a", sessions_mod.shapes()["a"],
                                 "baseline", 1, tmp_path)

    assert len(records) == 2
    assert all(r["error"] for r in records)
    assert all(r["extraction"]["topics"] == [] for r in records)


# ---------------------------------------------------------------------------
# --dry-run: lists the plan, makes no LLM call, calls no deployed-config loader.
# ---------------------------------------------------------------------------

def test_dry_run_makes_no_llm_call_and_no_deployed_config_load(monkeypatch, tmp_path):
    llm_calls = []
    monkeypatch.setattr(run_mod.llm_utils, "call_llm",
                         lambda *a, **k: llm_calls.append(1) or (None, "should never run"))

    load_calls = []
    monkeypatch.setattr(run_mod, "load_deployed_llm_env",
                         lambda *a, **k: load_calls.append(1))

    fake_session = {"env": "test", "user_folder": "worker1", "date": "2026-09-01",
                     "session_base": "sess1", "n_segments": 5}
    monkeypatch.setattr(sessions_mod, "list_candidate_sessions",
                         lambda env, **kw: [fake_session])
    monkeypatch.setattr(sessions_mod, "s3_client", lambda **kw: object())

    # main() calls this in-process (never a subprocess), so any env var it sets on
    # a real run would otherwise outlive this test for the rest of the pytest
    # session. `main()` itself must not set anything on the --dry-run path (see the
    # guard test below), but monkeypatch's own teardown is the safety net here --
    # never rely on `main()`'s own restraint alone for a call inside a test process.
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

    out_dir = tmp_path / "continuity_eval_runs" / "run1"  # main() refuses to write elsewhere
    plan = run_mod.main(["--env", "test", "--sessions", "1", "--shapes", "a",
                          "--out", str(out_dir), "--dry-run"])

    assert llm_calls == []
    assert load_calls == []
    assert plan  # something was planned
    assert all(p["shape"] == "a" for p in plan)
    assert (out_dir / "sessions.json").exists()
    assert not (out_dir / "counts.json").exists()  # dry-run never computes counts
    written_sessions = json.loads((out_dir / "sessions.json").read_text(encoding="utf-8"))
    assert written_sessions == [{**fake_session, "data_env": "test", "config_env": "test"}]


def test_dry_run_leaves_aws_profile_and_region_env_unchanged(monkeypatch, tmp_path):
    """Guard against the exact CI-only leak this harness caused: main() must not set
    AWS_PROFILE/AWS_DEFAULT_REGION on the --dry-run path, because a caller that runs
    main() in-process (this test, or a script run's own --dry-run check) shares one
    os.environ with every AWS client built later in the same process -- including,
    in CI, unit tests that build a boto3 client and have no `fieldsight-deployer`
    profile to find."""
    monkeypatch.setattr(run_mod.llm_utils, "call_llm",
                         lambda *a, **k: (None, "should never run"))
    monkeypatch.setattr(run_mod, "load_deployed_llm_env", lambda *a, **k: None)
    fake_session = {"env": "test", "user_folder": "worker1", "date": "2026-09-01",
                     "session_base": "sess1", "n_segments": 5}
    monkeypatch.setattr(sessions_mod, "list_candidate_sessions",
                         lambda env, **kw: [fake_session])
    monkeypatch.setattr(sessions_mod, "s3_client", lambda **kw: object())

    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

    out_dir = tmp_path / "continuity_eval_runs" / "run2"
    run_mod.main(["--env", "test", "--sessions", "1", "--shapes", "a",
                  "--out", str(out_dir), "--dry-run"])

    assert "AWS_PROFILE" not in os.environ
    assert "AWS_DEFAULT_REGION" not in os.environ


# ---------------------------------------------------------------------------
# --config-env: separable from --env (data source) per spec S7 "Decision rule".
# ---------------------------------------------------------------------------

def test_config_env_defaults_to_env_on_dry_run(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(run_mod.llm_utils, "call_llm",
                         lambda *a, **k: (None, "should never run"))
    monkeypatch.setattr(run_mod, "load_deployed_llm_env", lambda *a, **k: None)
    fake_session = {"env": "test", "user_folder": "worker1", "date": "2026-09-01",
                     "session_base": "sess1", "n_segments": 5}
    monkeypatch.setattr(sessions_mod, "list_candidate_sessions",
                         lambda env, **kw: [fake_session])
    monkeypatch.setattr(sessions_mod, "s3_client", lambda **kw: object())
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

    out_dir = tmp_path / "continuity_eval_runs" / "run_default"
    run_mod.main(["--env", "test", "--sessions", "1", "--shapes", "a",
                  "--out", str(out_dir), "--dry-run"])

    # dry-run output shows both env values, and they're equal since --config-env
    # was never passed.
    first_line = json.loads(capsys.readouterr().out.splitlines()[0])
    assert first_line == {"data_env": "test", "config_env": "test"}
    written_sessions = json.loads((out_dir / "sessions.json").read_text(encoding="utf-8"))
    assert written_sessions == [{**fake_session, "data_env": "test", "config_env": "test"}]


def test_config_env_diverges_from_env_on_dry_run(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(run_mod.llm_utils, "call_llm",
                         lambda *a, **k: (None, "should never run"))
    monkeypatch.setattr(run_mod, "load_deployed_llm_env", lambda *a, **k: None)
    fake_session = {"env": "prod", "user_folder": "worker1", "date": "2026-09-01",
                     "session_base": "sess1", "n_segments": 5}
    list_calls = []

    def fake_list(env, **kw):
        list_calls.append(env)
        return [fake_session]
    monkeypatch.setattr(sessions_mod, "list_candidate_sessions", fake_list)
    monkeypatch.setattr(sessions_mod, "s3_client", lambda **kw: object())
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

    out_dir = tmp_path / "continuity_eval_runs" / "run_diverge"
    run_mod.main(["--env", "prod", "--config-env", "test", "--sessions", "1",
                  "--shapes", "a", "--out", str(out_dir), "--dry-run"])

    # Sessions are still listed from the prod bucket/env (--env), not the config env.
    assert list_calls == ["prod"]
    first_line = json.loads(capsys.readouterr().out.splitlines()[0])
    assert first_line == {"data_env": "prod", "config_env": "test"}
    written_sessions = json.loads((out_dir / "sessions.json").read_text(encoding="utf-8"))
    assert written_sessions == [{**fake_session, "data_env": "prod", "config_env": "test"}]


def test_config_env_test_loads_the_test_functions_env_for_a_prod_sessions_run(
        monkeypatch, tmp_path):
    """--env prod --config-env test: sessions come from the prod bucket, but the
    deployed model config loaded into this process is TEST's -- the harness must pass
    EXTRACT_SESSION_FUNCTION['test'], not EXTRACT_SESSION_FUNCTION['prod'], to
    load_deployed_llm_env."""
    load_calls = []
    monkeypatch.setattr(run_mod, "load_deployed_llm_env",
                         lambda function_name, **kw: load_calls.append(function_name))
    # run_chain and build_counts do real S3/LLM work on a non-dry-run path; stub both
    # out entirely so this test only exercises which config gets loaded and from
    # which bucket sessions are listed.
    monkeypatch.setattr(run_mod, "run_chain", lambda *a, **k: [])
    monkeypatch.setattr(run_mod, "build_counts", lambda sessions: [])

    fake_session = {"env": "prod", "user_folder": "worker1", "date": "2026-09-01",
                     "session_base": "sess1", "n_segments": 5}
    list_calls = []

    def fake_list(env, **kw):
        list_calls.append(env)
        return [fake_session]
    monkeypatch.setattr(sessions_mod, "list_candidate_sessions", fake_list)
    monkeypatch.setattr(sessions_mod, "s3_client", lambda **kw: object())
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

    out_dir = tmp_path / "continuity_eval_runs" / "run_real"
    run_mod.main(["--env", "prod", "--config-env", "test", "--sessions", "1",
                  "--shapes", "a", "--out", str(out_dir)])

    assert list_calls == ["prod"]  # data source is --env
    assert load_calls == [run_mod.EXTRACT_SESSION_FUNCTION["test"]]  # config is --config-env


def test_planned_runs_is_pure_and_covers_every_arm_and_rep():
    sessions = [{"env": "test", "user_folder": "u", "date": "2026-09-01", "session_base": "s"}]
    shapes_dict = {"a": sessions_mod.shapes()["a"]}
    plan = run_mod.planned_runs(sessions, shapes_dict)
    assert len(plan) == len(run_mod.ARMS) * len(run_mod.REPS)
    assert {p["arm"] for p in plan} == set(run_mod.ARMS)
    assert {p["rep"] for p in plan} == set(run_mod.REPS)
    assert all(p["n_steps"] == 2 for p in plan)  # shape a: [0.95, 1.0]
