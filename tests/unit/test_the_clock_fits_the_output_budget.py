"""A batch function's HTTP timeout has to cover the answer it is allowed to ask for.

`test_template_llm_timeout_ordering` already pins `LLM_HTTP_TIMEOUT < Timeout`, so
urllib3 raises before the runtime hard-kills the function. That is necessary and
not sufficient: it says the two clocks are ordered, not that either is long enough
for the work.

`ANSWER_TOKEN_CEILING` went from three scattered 16000s to one 32000, and what
goes on the wire is that plus `REASONING_HEADROOM_TOKENS`. Meanwhile
`LLM_HTTP_TIMEOUT` stayed at the 180 that was sized for the old number. Nothing
fails until a report is long enough to spend its budget, and then it fails as a
*missing* report rather than a slow one -- urllib3 gives up, the outer Lambda
Timeout of 900 never gets a turn, and the S3 retry runs the same paid call into
the same wall (BUG-43).

The arithmetic uses throughput, because on this vendor latency tracks OUTPUT and
barely notices input. Measured on the deployed TEST functions at effort=high:

    prompt=44297  completion=16379  ->  119.1s   (137.5 tok/s)
    prompt=41599  completion= 3024  ->   37.3s   ( 81.0 tok/s)
    prompt= 7110  completion= 6440  ->   50.4s   (127.8 tok/s)

The 7K-token prompt took longer than the 41K one. Extrapolating a timeout from
input size -- the intuitive thing, and what was nearly done here -- gives an
answer that is wrong in both directions.

MEASURED_FLOOR_TOK_S is deliberately the slowest of the three, not the average: a
timeout sized on the average is a timeout that fails half the time it matters.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TEMPLATE = (ROOT / "src" / "template.yaml").read_text(encoding="utf-8")

MEASURED_FLOOR_TOK_S = 81.0

# The functions whose callers can reach ANSWER_TOKEN_CEILING. All batch: nobody is
# watching a screen while they run, so the right move when the budget grows is to
# grow the clock.
BATCH_FUNCTIONS = ["ReportGeneratorFunction", "MeetingMinutesFunction",
                   "ExtractSessionFunction"]

# AskAgentFunction is deliberately absent. It is the one path with a person
# waiting, its clock (45s) is a product decision rather than an engineering
# headroom, and the answer budget is what should yield there -- not the clock. Its
# measured answers are ~200 tokens against a budget of 8,000, so the ceiling is
# nowhere near binding; if that ever changes, the fix is a smaller budget for Ask,
# not a longer wait.


def _block(name):
    i = TEMPLATE.index(f"  {name}:")
    j = TEMPLATE.find("\n  ", TEMPLATE.index("Type:", i))
    # Walk to the next top-level resource rather than guessing a line count.
    m = re.search(r"\n  [A-Z][A-Za-z0-9]*:\n    Type:", TEMPLATE[i + 10:])
    end = i + 10 + m.start() if m else len(TEMPLATE)
    return TEMPLATE[i:end]


def _http_timeout(name):
    m = re.search(r"^\s+LLM_HTTP_TIMEOUT: '(\d+)'", _block(name), re.M)
    return int(m.group(1)) if m else None


def _lambda_timeout(name):
    m = re.search(r"^\s+Timeout: (\d+)", _block(name), re.M)
    return int(m.group(1)) if m else None


def _wire_budget_tokens():
    src = (ROOT / "src" / "llm_utils.py").read_text(encoding="utf-8")
    ceiling = int(re.search(
        r'ANSWER_TOKEN_CEILING = int\(os\.environ\.get\("LLM_ANSWER_TOKEN_CEILING", "(\d+)"\)\)',
        src).group(1))
    headroom = int(re.search(
        r'REASONING_HEADROOM_TOKENS = int\(os\.environ\.get\("LLM_REASONING_HEADROOM", "(\d+)"\)\)',
        src).group(1))
    return ceiling + headroom


@pytest.mark.parametrize("name", BATCH_FUNCTIONS)
def test_the_http_clock_covers_the_budget_it_hands_out(name):
    """Read the budget from the code and the clock from the template, so raising
    one without the other is red. Both numbers moved tonight, in opposite
    directions relative to each other, and nothing connected them."""
    needed = _wire_budget_tokens() / MEASURED_FLOOR_TOK_S
    http = _http_timeout(name)
    assert http is not None, f"{name} has no LLM_HTTP_TIMEOUT"
    assert http >= needed, (
        f"{name}: LLM_HTTP_TIMEOUT={http}s cannot deliver "
        f"{_wire_budget_tokens()} tokens at {MEASURED_FLOOR_TOK_S} tok/s "
        f"({needed:.0f}s needed)")


@pytest.mark.parametrize("name", BATCH_FUNCTIONS)
def test_the_lambda_clock_still_outlives_the_http_one(name):
    """Restated here rather than assumed: raising an HTTP timeout past its Lambda
    Timeout is the exact way to lose the `except` that exists for a slow LLM, and
    this file is where someone will be editing those numbers."""
    assert _lambda_timeout(name) > _http_timeout(name)
