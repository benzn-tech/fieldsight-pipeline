"""Unit: every LLM caller's HTTP timeout must lose the race against its own Lambda timeout.

`llm_utils` states the rule and the reason in one line:

    # 150s so the HTTP client loses the race against the Lambda's own Timeout and
    # we get a catchable urllib3 error instead of a runtime hard-kill.

The guarantee that buys is a *fallback*. When the HTTP client gives up first, the caller
catches urllib3, logs, and degrades — `lambda_session_finalize` falls back to the rolling
summary and still sends the confirmation email. When the Lambda dies first there is no
exception to catch: no fallback, no email, and a `Status: timeout` line nobody is watching.

`SessionFinalizeFunction` had the race inverted — Timeout 120 against the 150 s default — and
it was found by running the session brief on a real 26-minute recording, which was
hard-killed at exactly 120000 ms three times over. Nothing in the suite could have said so,
because the ordering lived in two files that never mentioned each other.

This asserts the ordering rather than any number, so raising or lowering either side stays
safe and reversing them does not.
"""
import os
import re

import pytest

TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src", "template.yaml")

# The default in llm_utils, used by any function that does not set the variable.
DEFAULT_HTTP_TIMEOUT = 150


def _functions():
    """(name, Timeout, LLM_HTTP_TIMEOUT or None) for every function in the template.

    Parsed with a scanner rather than a YAML loader: the template is full of `!Ref`,
    `!Sub` and `!If` tags that a plain loader rejects, and adding a tag-tolerant loader
    here would be more machinery than the question needs.
    """
    text = open(TEMPLATE, encoding="utf-8").read()
    out = []
    for m in re.finditer(r"^  (\w+):\n    Type: AWS::Serverless::Function\n", text, re.M):
        name = m.group(1)
        nxt = re.search(r"^  \w+:\n    Type: AWS::", text[m.end():], re.M)
        block = text[m.end(): m.end() + (nxt.start() if nxt else len(text))]
        t = re.search(r"^      Timeout: (\d+)", block, re.M)
        h = re.search(r"^\s+LLM_HTTP_TIMEOUT: '(\d+)'", block, re.M)
        out.append((name, int(t.group(1)) if t else None,
                    int(h.group(1)) if h else None))
    return out


def _calls_llm(name):
    """Does this function's handler reach `llm_utils.call_llm`?

    Keyed on the source rather than a hand-kept list, so a function that starts calling an
    LLM is covered the day it does and not the day somebody remembers this file.
    """
    src_dir = os.path.join(os.path.dirname(TEMPLATE))
    text = open(TEMPLATE, encoding="utf-8").read()
    m = re.search(r"^  %s:\n(?:.*\n)*?      Handler: (\S+)\.lambda_handler" % name,
                  text, re.M)
    if not m:
        return False
    path = os.path.join(src_dir, m.group(1) + ".py")
    if not os.path.exists(path):
        return False
    body = open(path, encoding="utf-8").read()
    # A CALL, not a mention. The first version matched the string `session_brief` anywhere,
    # and org-api gained an endpoint that reads `session_brief/...` from S3 — no model, no
    # HTTP client, 30 s timeout, and instantly an offender. A test that reports a function
    # for containing a word is a test that will be silenced rather than fixed.
    return ("llm_utils.call_llm(" in body
            or "call_llm(" in body and "import llm_utils" in body
            or "session_brief.brief_from_turns(" in body)


def test_the_http_timeout_always_loses_the_race():
    """The invariant, asserted for every LLM caller at once.

    A function whose HTTP timeout exceeds its Lambda timeout cannot fall back — the runtime
    kills it before the client raises, so the `except` that exists for exactly this never
    runs.
    """
    offenders = []
    for name, lambda_timeout, http_timeout in _functions():
        if lambda_timeout is None or not _calls_llm(name):
            continue
        effective = http_timeout if http_timeout is not None else DEFAULT_HTTP_TIMEOUT
        if effective >= lambda_timeout:
            offenders.append(
                f"{name}: LLM_HTTP_TIMEOUT {effective}s >= Timeout {lambda_timeout}s"
                + ("" if http_timeout is not None else "  (unset, so the 150s default)"))
    assert not offenders, (
        "these functions die before their HTTP client gives up, so the fallback path "
        "cannot run: " + "; ".join(offenders))


def test_the_finalise_worker_is_sized_for_a_whole_transcript():
    """Sized against the other full-transcript LLM caller, not against a guess.

    `session_brief` sends the whole session; `ExtractSessionFunction` does the same class of
    work and carries 600 s. 120 was inherited from the rolling summariser, whose input is
    already compressed, and a real 26-minute session was hard-killed by it three times.
    """
    by_name = {n: t for n, t, _ in _functions()}
    assert by_name.get("SessionFinalizeFunction", 0) >= 300, by_name.get(
        "SessionFinalizeFunction")


@pytest.mark.parametrize("name", ["SessionFinalizeFunction", "ExtractSessionFunction"])
def test_a_full_transcript_caller_states_its_http_timeout(name):
    """Explicit, not defaulted. The 150 s default is fine for a caller with a 300 s timeout
    and fatal for one with 120, and a value that only matters by comparison should not be
    invisible at the place the comparison is made."""
    stated = {n: h for n, _, h in _functions()}
    assert stated.get(name) is not None, (
        f"{name} leaves LLM_HTTP_TIMEOUT at the default; state it beside the Timeout it "
        f"has to stay under")
