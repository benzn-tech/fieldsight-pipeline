"""Unit: the report generator's timeout must exceed the work it is allowed to do.

Measured on prod 2026-08-13, one nightly run, one RequestId:

    Duration 300000.00 ms  Status: timeout
    Duration 300000.00 ms  Status: timeout     (Lambda's async retry)
    Duration  76249.39 ms                      (third attempt, succeeded)

Max memory used was 128 MB of 512, so nothing was memory- or compute-bound. The function
makes one LLM call per user per day and `llm_utils.HTTP_TIMEOUT` is 150s, so four users can
legitimately need 600s against a 300s ceiling. `fieldsight-prod-report-errors` was red for
that, and it is worth being precise: the obvious-looking `NoSuchKey ... summary_report.json`
[ERROR] in the same log stream is noise, not the trigger — it appears inside the attempt
that SUCCEEDED.

This test is arithmetic, not a magic number: whatever the per-call HTTP timeout becomes,
the function's ceiling has to stay above a plausible day's worth of calls.
"""
import os
import re

import pytest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src")

# A day's worth of users on one site. Not a limit anyone enforces -- the point is that the
# ceiling must clear a realistic day, and four is what prod ran on the night this was found.
USERS_PER_DAY = 4


def _report_generator_timeout():
    tpl = open(os.path.join(SRC, "template.yaml"), encoding="utf-8").read()
    i = tpl.find("\n  ReportGeneratorFunction:")
    assert i > 0, "ReportGeneratorFunction is gone from the template"
    m = re.search(r"\n  [A-Za-z0-9]+:\n", tpl[i + 5:])
    block = tpl[i:i + 5 + (m.start() if m else len(tpl))]
    t = re.search(r"^      Timeout: (\d+)$", block, re.MULTILINE)
    assert t, "no Timeout on ReportGeneratorFunction"
    return int(t.group(1))


def _llm_http_timeout():
    src = open(os.path.join(SRC, "llm_utils.py"), encoding="utf-8").read()
    m = re.search(r'HTTP_TIMEOUT = float\(os\.environ\.get\("LLM_HTTP_TIMEOUT", "(\d+)"\)\)',
                  src)
    assert m, "HTTP_TIMEOUT moved; this test can no longer do its arithmetic"
    return int(m.group(1))


def test_the_ceiling_clears_a_realistic_day():
    """A ceiling below the work does not fail fast — it fails, retries the whole job from
    scratch, and fails again, which is how one slow night produced two alarm-triggering
    errors and three full report generations."""
    ceiling = _report_generator_timeout()
    needed = _llm_http_timeout() * USERS_PER_DAY
    assert ceiling >= needed, (
        f"Timeout {ceiling}s cannot clear {USERS_PER_DAY} users x "
        f"{_llm_http_timeout()}s = {needed}s of LLM calls")


def test_the_ceiling_is_within_what_lambda_allows():
    assert _report_generator_timeout() <= 900, "Lambda's hard maximum is 900s"
