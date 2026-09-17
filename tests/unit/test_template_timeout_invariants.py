"""Unit: a Lambda timeout is a promise about everything that runs inside it.

This template has shipped the same failure three times -- a model HTTP timeout at or
above its function's Timeout, so the runtime SIGKILLs the function before urllib3 can
raise and no result is written (ExtractSession: 664 invocations in one afternoon;
SessionFinalize: three hard-kills and no confirmation email; MeetingMinutes). The
numbers below are not pinned; the relationships are:

1. No Timeout exceeds the Lambda maximum (900 s).
2. LLM_HTTP_TIMEOUT and GENERATION_BUDGET_SECONDS are strictly below Timeout.
3. A handler behind API Gateway is at or under 30 s: the gateway cuts at 29 s.
4. A function on a rate() schedule is shorter than its interval -- otherwise runs
   stack -- unless listed in OVERLAP_ACCEPTED with its exact value and a reason.
5. A function invoked synchronously by another is no longer than its caller.

Text parsing, not a YAML loader: CloudFormation tags (!Ref, !Sub) break safe_load,
and tests/unit/test_template_workflow_parameter_wiring.py already reads this file
this way.
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATE = os.path.join(REPO, "src", "template.yaml")

LAMBDA_MAX_SECONDS = 900
API_GATEWAY_HANDLER_MAX_SECONDS = 30

# rate()-scheduled functions allowed to outlive their interval. Pinned to the exact
# value so a raise still fails.
OVERLAP_ACCEPTED = {
    # rate(1 minute), no reserved concurrency. Sessions are CAS-claimed so an overlap
    # cannot double-finalize, but every extra copy holds an Aurora connection and eats
    # account concurrency; raising it breaks the auto-pause cadence invariant
    # (tests/unit/test_sweep_cadence_vs_autopause.py).
    "FinalizeSweepFunction": 120,
}

# (caller, callee) pairs invoked with InvocationType=RequestResponse -- the caller
# waits, so the callee can never usefully outlive it. Evidence per pair is cited in
# the plan research (lambda-timeout-tiers.md, "Sync callee chains").
SYNC_CALLEES = [
    ("AskAgentFunction", "RagSearchFunction"),
    ("WsSendVoiceFunction", "VoiceResolveFunction"),
    ("SpeakerEmbedFunction", "VoiceprintWriterFunction"),
    ("MatcherFunction", "SuggestionWriterFunction"),
    ("DeviceReportFunction", "DeviceLedgerFunction"),
]

_RATE_UNIT_SECONDS = {"minute": 60, "minutes": 60, "hour": 3600, "hours": 3600,
                      "day": 86400, "days": 86400}


def _global_timeout(text):
    g = re.search(r"(?ms)^Globals:\n(.*?)(?=^\S)", text)
    m = re.search(r"(?m)^    Timeout:\s*(\d+)", g.group(1)) if g else None
    return int(m.group(1)) if m else 3


def _functions():
    text = open(TEMPLATE, encoding="utf-8").read()
    default_timeout = _global_timeout(text)
    resources = re.search(r"(?ms)^Resources:\n(.*?)(?=^\S|\Z)", text).group(1)
    blocks = re.split(r"(?m)^  (?=[A-Za-z0-9]+:\s*$)", resources)
    out = {}
    for block in blocks:
        head = re.match(r"([A-Za-z0-9]+):\s*\n(?:\s*#.*\n)*    Type:\s*AWS::Serverless::Function\b",
                        block)
        if not head:
            continue
        timeout = re.search(r"(?m)^      Timeout:\s*(\d+)", block)
        llm = re.search(r"(?m)^\s+LLM_HTTP_TIMEOUT:\s*'?(\d+)'?\s*$", block)
        budget = re.search(r"(?m)^\s+GENERATION_BUDGET_SECONDS:\s*'?(\d+)'?\s*$", block)
        rates = [int(n) * _RATE_UNIT_SECONDS[u] for n, u in re.findall(
            r"(?m)^\s+Schedule:\s*'?rate\((\d+)\s+(minutes?|hours?|days?)\)'?\s*$", block)]
        api = bool(re.search(r"(?m)^\s+Type:\s*(Api|HttpApi)\s*$", block))
        out[head.group(1)] = {
            "timeout": int(timeout.group(1)) if timeout else default_timeout,
            "llm_http_timeout": int(llm.group(1)) if llm else None,
            "budget": int(budget.group(1)) if budget else None,
            "rates_sec": rates,
            "api": api,
        }
    return out


def test_the_parser_sees_the_whole_template():
    # A parser that silently matches nothing passes every invariant below.
    fns = _functions()
    assert len(fns) >= 40, sorted(fns)
    for name in ("SessionReportFunction", "FinalizeSweepFunction", "OrgApiFunction",
                 "AskAgentFunction", "ExtractSessionFunction"):
        assert name in fns
    assert fns["OrgApiFunction"]["api"] is True
    assert fns["FinalizeSweepFunction"]["rates_sec"] == [60]
    assert fns["ExtractSessionFunction"]["llm_http_timeout"] is not None


def test_no_timeout_exceeds_the_lambda_maximum():
    over = {n: f["timeout"] for n, f in _functions().items()
            if f["timeout"] > LAMBDA_MAX_SECONDS}
    assert over == {}


def test_a_model_http_timeout_is_below_its_function_timeout():
    bad = {n: (f["llm_http_timeout"], f["timeout"]) for n, f in _functions().items()
           if f["llm_http_timeout"] is not None and f["llm_http_timeout"] >= f["timeout"]}
    assert bad == {}


def test_a_generation_budget_is_below_its_function_timeout():
    bad = {n: (f["budget"], f["timeout"]) for n, f in _functions().items()
           if f["budget"] is not None and f["budget"] >= f["timeout"]}
    assert bad == {}


def test_an_api_gateway_handler_is_at_or_under_thirty_seconds():
    bad = {n: f["timeout"] for n, f in _functions().items()
           if f["api"] and f["timeout"] > API_GATEWAY_HANDLER_MAX_SECONDS}
    assert bad == {}


def test_a_scheduled_function_is_shorter_than_its_interval():
    fns = _functions()
    bad = {}
    for name, f in fns.items():
        for interval in f["rates_sec"]:
            if name in OVERLAP_ACCEPTED:
                if f["timeout"] != OVERLAP_ACCEPTED[name]:
                    bad[name] = ("accepted value changed", f["timeout"],
                                 OVERLAP_ACCEPTED[name])
            elif f["timeout"] >= interval:
                bad[name] = (f["timeout"], interval)
    assert bad == {}


def test_a_sync_callee_is_no_longer_than_its_caller():
    fns = _functions()
    bad = {}
    for caller, callee in SYNC_CALLEES:
        assert caller in fns and callee in fns, (caller, callee)
        if fns[callee]["timeout"] > fns[caller]["timeout"]:
            bad[(caller, callee)] = (fns[caller]["timeout"], fns[callee]["timeout"])
    assert bad == {}
