"""tools/anthropic-smoke.py (spec 2026-10-31 Part C merge gate): fakes only, no call."""
import importlib.util
import io
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PATH = os.path.join(REPO, "tools", "anthropic-smoke.py")


def _load():
    spec = importlib.util.spec_from_file_location("anthropic_smoke_under_test", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


A = _load()
KEY = "sk-ant-SENTINEL-key"


def _src(tmp_path, gate):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    body = ("def _call_anthropic(prompt, max_tokens, deadline=None, caller='unknown'%s):\n"
            "    pass\n" % (", enable_thinking=None" if gate else ""))
    if gate:
        body += "def _is_claude_5x(model):\n    return False\n"
    (src / "llm_utils.py").write_text(body, encoding="utf-8")
    return str(src)


def _key(tmp_path):
    kf = tmp_path / "ant.key"
    kf.write_text(KEY + "\n", encoding="utf-8")
    return str(kf)


def test_a_labelled_key_file_yields_the_bare_key(tmp_path):
    kf = tmp_path / "labelled.key"
    kf.write_text("field_sight_v2=" + KEY + "\n", encoding="utf-8")
    keys = []

    def spawn(cmd, env):
        keys.append(env["ANTHROPIC_API_KEY"])
        return 0, json.dumps({"call": cmd[-1], "model": env["CLAUDE_MODEL"], "ok": True,
                              "status": 200, "stop_reason": "end_turn", "text_len": 5})
    A.run(["--key-file", str(kf)], src_dir=_src(tmp_path, True), spawn=spawn,
          stdout=io.StringIO())
    assert keys == [KEY, KEY]


def test_gate_read_from_source(tmp_path):
    assert A.has_5x_gate(_src(tmp_path, False)) is False
    assert A.has_5x_gate(_src(tmp_path, True)) is True
    assert A.has_5x_gate(str(tmp_path / "nowhere")) is False


def test_refuses_without_the_5x_gate(tmp_path):
    spawned = []
    out = io.StringIO()
    code = A.run(["--key-file", _key(tmp_path)], src_dir=_src(tmp_path, False),
                 spawn=lambda *a: spawned.append(a), stdout=out)
    assert code == 2 and spawned == [] and "needs PR-2" in out.getvalue()


def test_child_env_per_call():
    base = {"PATH": "p", "ANTHROPIC_API_KEY": "sk-OTHER", "LLM_TEMPERATURE": "0.7",
            "QWEN_API_KEY": "sk-or"}
    h = A.child_env("haiku", KEY, base)
    assert (h["LLM_PROVIDER"], h["CLAUDE_MODEL"], h["LLM_TEMPERATURE"]) == \
        ("anthropic", "claude-haiku-5-5", "0")
    assert h["ANTHROPIC_API_KEY"] == KEY and "QWEN_API_KEY" not in h
    assert "LLM_REASONING_EFFORT" not in h and "enable_thinking" not in h
    s = A.child_env("sonnet", KEY, base)
    assert (s["CLAUDE_MODEL"], s["LLM_REASONING_EFFORT"]) == ("claude-sonnet-5-5", "high")
    assert "LLM_TEMPERATURE" not in s
    assert A.CALLS["haiku"]["enable_thinking"] is False
    assert A.CALLS["sonnet"]["enable_thinking"] is None


def test_runs_two_children_and_never_prints_the_key(tmp_path):
    seen = []

    def spawn(cmd, env):
        seen.append((cmd[-1], env["CLAUDE_MODEL"]))
        return 0, json.dumps({"call": cmd[-1], "model": env["CLAUDE_MODEL"], "ok": True,
                              "status": 200, "stop_reason": "end_turn", "text_len": 5})
    out = io.StringIO()
    code = A.run(["--key-file", _key(tmp_path)], src_dir=_src(tmp_path, True), spawn=spawn,
                 stdout=out)
    assert code == 0 and seen == [("haiku", "claude-haiku-5-5"),
                                  ("sonnet", "claude-sonnet-5-5")]
    text = out.getvalue()
    assert "PASS" in text and "SENTINEL" not in text and A.PROMPT not in text


def test_a_failed_or_silent_child_fails_the_gate(tmp_path):
    def spawn(cmd, env):
        if cmd[-1] == "sonnet":
            return 1, "Traceback ..."
        return 0, json.dumps({"call": "haiku", "ok": True})
    out = io.StringIO()
    assert A.run(["--key-file", _key(tmp_path)], src_dir=_src(tmp_path, True), spawn=spawn,
                 stdout=out) == 1
    assert "FAIL" in out.getvalue()


def test_dry_run_makes_no_call(tmp_path):
    spawned = []
    assert A.run(["--key-file", _key(tmp_path), "--dry-run"], src_dir=_src(tmp_path, True),
                 spawn=lambda *a: spawned.append(a), stdout=io.StringIO()) == 0
    assert spawned == []


class _Resp:
    def __init__(self, data):
        self.status, self.data = 200, json.dumps(data).encode("utf-8")


def test_child_reports_shape_only(monkeypatch, capsys):
    sys.path.insert(0, os.path.join(REPO, "src"))
    import llm_utils
    monkeypatch.setattr(llm_utils, "ANTHROPIC_API_KEY", KEY)
    monkeypatch.setattr(llm_utils, "_post_with_retry", lambda url, body, headers, **kw: (
        _Resp({"content": [{"type": "text", "text": "ANSWER-SENTINEL"}],
               "stop_reason": "end_turn", "usage": {}}), None))
    # Today's _call_anthropic (no enable_thinking): the sonnet shape calls it bare.
    assert A.one("sonnet") == 0
    res = json.loads(capsys.readouterr().out.strip())
    assert res["ok"] is True and res["text_len"] == len("ANSWER-SENTINEL")
    assert res["status"] == 200 and res["stop_reason"] == "end_turn"
    assert "messages" in res["sent"] and "SENTINEL" not in json.dumps(res)
