"""The LLM_USAGE line from `corroboration_client.call` must actually reach the
log in a real Lambda, not just be reachable by calling logger.info().

Mirrors `test_llm_usage_survives_the_lambda_runtime_default.py` exactly, for
the second production LLM client this repo carries. The two are NOT the same
proof: `corroboration_client.py` reaches the shared `llm_usage.log_usage`
helper through `import llm_usage`, never through `import llm_utils` (a
different test enforces that isolation --
test_corroboration_client.py::test_the_client_is_one_vendor_on_every_stack).
So the mechanism that raises the ROOT logger off the Lambda runtime's WARNING
default is `llm_usage.py`'s own module-level `logger.setLevel(logging.INFO)`,
reached by importing `corroboration_client`, not by any side effect of
`llm_utils` merely existing somewhere in the process. That chain is exactly
what this subprocess proves, rather than assumes: nothing else in the child
process configures logging beyond pinning root to WARNING before the import.
"""
import os
import subprocess
import sys

SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src")

SCRIPT = r"""
import json, logging, os, sys
logging.basicConfig(stream=sys.stdout, level=logging.WARNING, format="%(levelname)s %(message)s")
logging.getLogger().setLevel(logging.WARNING)   # what the Lambda runtime gives us
os.environ["CORROBORATION_API_KEY"] = "test-key"
import corroboration_client as cc                # only this import may raise the level

class _Resp:
    status = 200
    data = json.dumps({
        "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                  "completion_tokens_details": {"reasoning_tokens": 2},
                  "prompt_tokens_details": {"cached_tokens": 3}},
    }).encode("utf-8")

cc.urllib3.PoolManager.request = (
    lambda self, method, url, body=None, headers=None, timeout=None: _Resp())
cc.call("hi", timeout=13, caller="rewrite")
"""


def test_corroboration_usage_line_is_emitted_at_the_lambda_runtimes_default_level():
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    out = subprocess.run([sys.executable, "-c", SCRIPT], cwd=SRC, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    # The child process never configured logging beyond pinning root to
    # WARNING before the import -- only importing corroboration_client (via
    # its import of llm_usage) can have raised it, which is the exact
    # condition the recorded llm_utils defect lived in, and this client does
    # not import llm_utils to inherit the fix for free.
    assert "LLM_USAGE" in out.stdout, (
        "the usage line was not emitted at the Lambda's default level:\n"
        + out.stdout + out.stderr)
    assert "provider=openrouter" in out.stdout
    assert "caller=rewrite" in out.stdout
    assert "reasoning_tokens=2" in out.stdout
    assert "cache_read_tokens=3" in out.stdout
