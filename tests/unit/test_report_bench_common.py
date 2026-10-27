"""Report / extraction bench, Task 1: import env, out-dir guard, JSONL helpers.

The bench dir has a hyphen, so modules are loaded by path. Every report-bench
module is named `rb_*` so it can never collide in `sys.modules` with the web
bench's `ledger` / `select_freeze` / `prompts` in the same pytest run.
"""
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


common = _load("rb_common")


def test_import_env_is_exact():
    env = common.IMPORT_ENV
    assert env["LLM_PROVIDER"] == "qwen"
    assert env["QWEN_BASE_URL"] == "https://openrouter.ai/api/v1"
    assert env["LLM_REASONING_EFFORT"] == "high"
    assert env["LLM_REASONING_HEADROOM"] == "8000"
    for k in ("EMIT_EVIDENCE", "DECLARE_CONTINUITY", "STATUS_SUGGESTIONS"):
        assert env[k] == "false"
    assert env["S3_BUCKET"] == "dummy"
    assert "LLM_TEMPERATURE" not in env


def test_subprocess_env_is_built_from_scratch(monkeypatch):
    monkeypatch.setenv("LLM_TEMPERATURE", "0")
    monkeypatch.setenv("SOME_SECRET_KEY", "x")
    env = common.subprocess_env()
    assert "LLM_TEMPERATURE" not in env
    assert "SOME_SECRET_KEY" not in env
    assert env["LLM_REASONING_HEADROOM"] == "8000"
    assert set(env) <= set(common.IMPORT_ENV) | {"SYSTEMROOT", "PATH", "TEMP", "TMP"}


def test_check_out_dir_refuses_repo_and_dropbox(tmp_path):
    with pytest.raises(ValueError):
        common.check_out_dir(os.path.join(REPO, "tmp"))
    with pytest.raises(ValueError):
        common.check_out_dir("C:/Users/camil/Dropbox/x")
    with pytest.raises(ValueError):
        common.check_out_dir("c:\\users\\camil\\dropbox\\x")
    assert common.check_out_dir(str(tmp_path)) == os.path.abspath(str(tmp_path))


def test_jsonl_roundtrip(tmp_path):
    p = str(tmp_path / "a.jsonl")
    common.append_jsonl(p, {"k": 1})
    common.append_jsonl(p, {"k": "二"})
    assert common.read_jsonl(p) == [{"k": 1}, {"k": "二"}]
    assert common.read_jsonl(str(tmp_path / "missing.jsonl")) == []


def test_sha256_text():
    assert common.sha256_text("abc") == \
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_bench_lock_is_exclusive(tmp_path):
    out = str(tmp_path)
    with common.bench_lock(out, "report"):
        assert os.path.exists(os.path.join(out, common.LOCK_NAME))
        with pytest.raises(RuntimeError):
            with common.bench_lock(out, "report"):
                pass
    assert not os.path.exists(os.path.join(out, common.LOCK_NAME))


def test_load_web_module_uses_a_private_name():
    led = common.load_web("ledger")
    assert led.__name__ == "web_ledger"
    assert sys.modules.get("ledger") is not led
