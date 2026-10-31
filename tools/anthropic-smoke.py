"""PR-2 merge gate: two real calls through llm_utils._call_anthropic (Claude 5.5).

Spec: docs/superpowers/specs/2026-10-31-model-scope-and-config-update-design.md Part C.
`output_config.effort` over raw HTTP with anthropic-version 2023-06-01 is unproven in
this repo, so PR-2 merges only after both calls return text. Owner-run, < $0.05:

    C:/Users/camil/fspy/Scripts/python.exe tools/anthropic-smoke.py --key-file <file> [--dry-run]

  haiku   CLAUDE_MODEL=claude-haiku-5-5, LLM_TEMPERATURE=0, enable_thinking=False
          (5.x: temperature must be dropped, effort low sent)
  sonnet  CLAUDE_MODEL=claude-sonnet-5-5, LLM_REASONING_EFFORT=high

Each call runs in its own subprocess with its env set before llm_utils is imported (the
model, temperature and effort are import-time constants). Refuses (exit 2, no call) when
src has no 5.x gate yet (`_is_claude_5x` and `_call_anthropic(..., enable_thinking=)`,
i.e. PR-2 is not on this checkout). Prints model, ok, HTTP status, stop_reason, the
request's field names and the text length -- never the key, the prompt or the answer.
Exit 0 = both returned text.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SRC = os.path.join(REPO, "src")

PROMPT = "Reply with the single word: ready."
MAX_TOKENS = 64
CALLS = {
    "haiku": {"CLAUDE_MODEL": "claude-haiku-5-5", "LLM_TEMPERATURE": "0",
              "enable_thinking": False},
    "sonnet": {"CLAUDE_MODEL": "claude-sonnet-5-5", "LLM_REASONING_EFFORT": "high",
               "enable_thinking": None},
}
_PASSTHROUGH = ("SYSTEMROOT", "PATH", "TEMP", "TMP")
CHILD_TIMEOUT_S = 180


def has_5x_gate(src_dir=SRC):
    """From the source, nothing imported: `_is_claude_5x` exists and
    `_call_anthropic` takes `enable_thinking`."""
    try:
        with open(os.path.join(src_dir, "llm_utils.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError):
        return False
    gate = takes = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if node.name == "_is_claude_5x":
                gate = True
            elif node.name == "_call_anthropic":
                takes = "enable_thinking" in {a.arg for a in node.args.args + node.args.kwonlyargs}
    return gate and takes


def child_env(name, key, base=None):
    base = os.environ if base is None else base
    env = {k: base[k] for k in _PASSTHROUGH if base.get(k) is not None}
    env.update({"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": key,
                "PYTHONIOENCODING": "utf-8"})
    env.update({k: v for k, v in CALLS[name].items() if k != "enable_thinking"})
    return env


def one(name):
    """Child side: the env is already set. One JSON line on stdout."""
    sys.path.insert(0, SRC)
    import llm_utils
    seen = {}
    real_post = llm_utils._post_with_retry

    def watched(url, body, headers, **kw):
        sent = json.loads(body)
        seen["sent"] = sorted(sent) + sorted("output_config." + k
                                             for k in (sent.get("output_config") or {}))
        seen["max_tokens"] = sent.get("max_tokens")
        resp, err = real_post(url, body, headers, **kw)
        seen["status"] = getattr(resp, "status", None)
        if resp is not None:
            try:
                seen["stop_reason"] = json.loads(resp.data.decode("utf-8")).get("stop_reason")
            except Exception:                            # noqa: BLE001 - shape only
                pass
        return resp, err

    llm_utils._post_with_retry = watched
    kw = {}
    if CALLS[name]["enable_thinking"] is not None:
        kw["enable_thinking"] = CALLS[name]["enable_thinking"]
    text, err = llm_utils._call_anthropic(PROMPT, MAX_TOKENS, caller="anthropic_smoke", **kw)
    print(json.dumps({"call": name, "model": llm_utils.CLAUDE_MODEL,
                      "ok": bool(text and text.strip()) and err is None,
                      "status": seen.get("status"), "stop_reason": seen.get("stop_reason"),
                      "sent": seen.get("sent"), "max_tokens": seen.get("max_tokens"),
                      "text_len": len(text or ""),
                      # The vendor's error message (never the key / prompt / answer).
                      "error": (err or "")[:160] or None}), flush=True)
    return 0


def _spawn(cmd, env):
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8",
                       timeout=CHILD_TIMEOUT_S, check=False)
    return r.returncode, r.stdout


def run(argv, *, src_dir=SRC, spawn=_spawn, stdout=sys.stdout):
    ap = argparse.ArgumentParser(prog="anthropic-smoke")
    ap.add_argument("--key-file", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if not has_5x_gate(src_dir):
        print("refused: src/llm_utils.py has no Claude 5.x gate (_is_claude_5x, "
              "_call_anthropic(enable_thinking=)): needs PR-2", file=stdout)
        return 2
    try:
        with open(a.key_file, encoding="utf-8") as f:
            key = f.read().strip()
    except OSError:
        print("refused: --key-file unreadable", file=stdout)
        return 2
    if not key:
        print("refused: --key-file is empty", file=stdout)
        return 2
    print(f"anthropic smoke: {len(CALLS)} calls ({', '.join(CALLS)}), max_tokens "
          f"{MAX_TOKENS} + headroom", file=stdout)
    if a.dry_run:
        return 0
    ok = True
    for name in CALLS:
        code, out = spawn([sys.executable, os.path.abspath(__file__), "--one", name],
                          child_env(name, key))
        line = (out or "").strip().splitlines()[-1:] or [""]
        try:
            res = json.loads(line[0])
        except ValueError:
            res = {"call": name, "ok": False, "error": f"child exit {code}, no result"}
        ok = ok and bool(res.get("ok"))
        print(json.dumps(res), file=stdout)
    print("smoke: " + ("PASS (both returned text)" if ok else "FAIL"), file=stdout)
    return 0 if ok else 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--one"]:
        return one(argv[1])
    return run(argv)


if __name__ == "__main__":
    sys.exit(main())
