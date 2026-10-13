"""The LLM_USAGE line must actually reach the log in a real Lambda, not just
be reachable by calling logger.info().

The Lambda runtime leaves the root logger at WARNING (this is the exact,
recorded failure shape from test_finalize_decisions_are_visible.py: an INFO
decision log was silently dropped on TEST because nothing had raised the
level yet). `llm_utils.py` raises the ROOT logger to INFO as a side effect of
being imported (`logger = logging.getLogger(); logger.setLevel(logging.INFO)`,
module level) -- the same mechanism every other INFO line in this module
already relies on (the pre-existing "qwen call: ..." and "qwen done: ..."
lines). This test proves the usage line rides that same mechanism, in a
subprocess where nothing else has touched logging, rather than asserting
`logger.info` was merely called.
"""
import os
import subprocess
import sys

SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src")

SCRIPT = r"""
import json, logging, sys
logging.basicConfig(stream=sys.stdout, level=logging.WARNING, format="%(levelname)s %(message)s")
logging.getLogger().setLevel(logging.WARNING)   # what the Lambda runtime gives us
import llm_utils as lu                          # only this import may raise the level

class _Resp:
    status = 200
    data = json.dumps({
        "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                  "completion_tokens_details": {"reasoning_tokens": 2}},
    }).encode("utf-8")

lu.urllib3.PoolManager.request = lambda self, method, url, body=None, headers=None, timeout=None: _Resp()
lu.LLM_PROVIDER = "qwen"
lu.QWEN_API_KEY = "sk-test"
lu.call_llm("hi", max_tokens=50, caller="extraction")
"""


def test_llm_usage_line_is_emitted_at_the_lambda_runtimes_default_level():
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    out = subprocess.run([sys.executable, "-c", SCRIPT], cwd=SRC, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    # The child process never configured logging beyond pinning root to
    # WARNING before the import -- only importing llm_utils can have raised
    # it, which is the exact condition the recorded defect lived in.
    assert "LLM_USAGE" in out.stdout, (
        "the usage line was not emitted at the Lambda's default level:\n"
        + out.stdout + out.stderr)
    assert "caller=extraction" in out.stdout
    assert "reasoning_tokens=2" in out.stdout
