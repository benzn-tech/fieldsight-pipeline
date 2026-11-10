"""Nightly re-match (plan Task 6): the pure seams and the wiring.

The tick, the run-rule and the handler's SQL are in tests/integration/test_nightly_tick.py.
Here: site_local_now, the rule registration, and the template / workflow / wire-script
wiring -- the ways this deploys green and does nothing, or does too much:
- the flag not passed by a workflow (SAM keeps a stack's previous value);
- the tick enabled while the flag is off, or the worker created while it is off;
- the worker retrying twice (a retry is a second paid call);
- the worker wired to S3 in shadow (the wire script runs --apply on every TEST deploy);
- the handler's head_object answered 403 for a missing key (no ListBucket), which would
  look like "exists" or crash every run.
"""
import datetime as dt
import io
import json
import os
import re

import pytest

_yaml = pytest.importorskip("yaml")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATE = os.path.join(REPO, "src", "template.yaml")
WORKFLOWS = {"TEST": os.path.join(REPO, ".github", "workflows", "deploy.yml"),
             "PROD": os.path.join(REPO, ".github", "workflows", "deploy-prod.yml")}
WIRE = os.path.join(REPO, "scripts", "wire-s3-events.sh")
LAKE = "arn:aws:s3:::${IngestBucketName}"
COND = "ShouldRunNightlyRematch"
WORKER, WRITER, SCAN = ("NightlyTurnPickFunction", "NightlySupportWriterFunction",
                        "DecisionScanFunction")
OFF = {"Not": [{"Equals": [{"Ref": "NightlyRematch"}, "off"]}]}


class _Loader(_yaml.SafeLoader):
    pass


def _tagged(loader, suffix, node):
    if isinstance(node, _yaml.ScalarNode):
        return {suffix: loader.construct_scalar(node)}
    if isinstance(node, _yaml.MappingNode):
        return {suffix: loader.construct_mapping(node, deep=True)}
    return {suffix: loader.construct_sequence(node, deep=True)}


_Loader.add_multi_constructor("!", _tagged)


@pytest.fixture(scope="module")
def doc():
    with io.open(TEMPLATE, encoding="utf-8") as f:
        return _yaml.load(f.read(), Loader=_Loader)


def _props(doc, name):
    return doc["Resources"][name]["Properties"]


def _env(doc, name):
    return _props(doc, name)["Environment"]["Variables"]


def _as_list(v):
    return v if isinstance(v, list) else [v]


def _sub(r):
    return r.get("Sub") if isinstance(r, dict) else r


def _statements(doc, name):
    for pol in _props(doc, name)["Policies"]:
        if isinstance(pol, dict) and "Statement" in pol:
            yield from pol["Statement"]


def _grants(doc, name):
    """{action: set(resource sub)} and the ListBucket prefixes."""
    got, prefixes = {}, set()
    for st in _statements(doc, name):
        assert st["Effect"] == "Allow"
        for a in _as_list(st["Action"]):
            got.setdefault(a, set()).update(_sub(r) for r in _as_list(st["Resource"]))
            if a == "s3:ListBucket":
                prefixes |= set(_as_list(st["Condition"]["StringLike"]["s3:prefix"]))
    return got, prefixes


# ---------------------------------------------------------------- pure seams

def test_site_local_now_is_nz_in_v1():
    import nightly_rematch as nr
    utc = dt.timezone.utc
    assert nr.site_local_now({"id": "s"}, dt.datetime(2026, 11, 10, 12, 0, tzinfo=utc)) == \
        dt.datetime(2026, 11, 11, 1, 0)                      # NZDT +13
    assert nr.site_local_now({"id": "s"}, dt.datetime(2026, 7, 1, 13, 0, tzinfo=utc)) == \
        dt.datetime(2026, 7, 2, 1, 0)                        # NZST +12


def test_request_key_is_deterministic_per_session_and_night():
    import nightly_rematch as nr
    k = nr.request_key("site-1", dt.date(2026, 11, 10), "extractions/f/2026-11-10/sidx.json")
    assert k == nr.request_key("site-1", "2026-11-10", "extractions/f/2026-11-10/sidx.json")
    assert re.fullmatch(r"nightly_requests/site-1/2026-11-10/[0-9a-f]{20}\.json", k)
    assert k != nr.request_key("site-1", dt.date(2026, 11, 11),
                               "extractions/f/2026-11-10/sidx.json")


def test_rule_registered_and_decision_rules_stays_importable_without_db():
    import decision_rules
    rule = decision_rules.RULES["nightly.due"]
    assert rule.run is not None and rule.late_after_days == 1 and rule.purpose
    src = io.open(os.path.join(REPO, "src", "decision_rules.py"), encoding="utf-8").read()
    assert "\nimport nightly_rematch" not in src and "\nfrom nightly_rematch" not in src


# ---------------------------------------------------------------- the flag, three places

def test_nightly_flag_wired_three_places(doc):
    p = doc["Parameters"]["NightlyRematch"]
    assert p["Default"] == "off" and p["AllowedValues"] == ["off", "shadow", "on"]
    assert _env(doc, SCAN)["NIGHTLY_REMATCH"] == {"Ref": "NightlyRematch"}
    for env, path in WORKFLOWS.items():
        text = io.open(path, encoding="utf-8").read()
        line = "\"NightlyRematch=${{ vars.%s_NIGHTLY_REMATCH || 'off' }}\"" % env
        assert line in text, env
        other = "PROD" if env == "TEST" else "TEST"
        assert f"{other}_NIGHTLY_REMATCH" not in text, env


def test_condition_needs_the_decision_loop_and_the_flag(doc):
    cond = doc["Conditions"][COND]["And"]
    assert {"Condition": "ShouldRunDecisionLoop"} in cond and OFF in cond
    tick = doc["Conditions"]["ShouldScheduleNightlyTick"]["And"]
    assert {"Condition": "ShouldEnableSchedules"} in tick and OFF in tick
    for name in (WORKER, WRITER, "AnthropicLayer"):
        assert doc["Resources"][name]["Condition"] == COND, name


def test_hourly_tick_schedule(doc):
    events = _props(doc, SCAN)["Events"]
    [tick] = [e for e in events.values()
              if json.loads(e["Properties"]["Input"])["scan"] == "nightly_tick"]
    pr = tick["Properties"]
    assert tick["Type"] == "Schedule" and pr["Schedule"] == "rate(1 hour)"
    assert pr["State"] == {"If": ["ShouldScheduleNightlyTick", "ENABLED", "DISABLED"]}


def test_scan_writes_only_nightly_requests_and_can_tell_missing_from_denied(doc):
    got, prefixes = _grants(doc, SCAN)
    assert got["s3:PutObject"] == {LAKE + "/nightly_requests/*", LAKE + "/lake_manifest/*",
                                   "arn:aws:s3:::${DataBucketName}/lake_manifest/*"}
    assert LAKE + "/nightly_requests/*" in got["s3:GetObject"]
    assert "nightly_requests/*" in prefixes


# ---------------------------------------------------------------- worker and writer

def test_worker_shape(doc):
    res = doc["Resources"][WORKER]
    p = res["Properties"]
    assert p["Handler"] == "lambda_nightly_turn_pick.lambda_handler"
    assert p["CodeUri"] == "src/" and "VpcConfig" not in p
    assert p["Timeout"] == 300
    assert p["EventInvokeConfig"]["MaximumRetryAttempts"] == 1
    assert p["Layers"] == [{"Ref": "AnthropicLayer"}]
    assert "Events" not in p                      # S3 is wired by the script (BUG-33)
    env, ext = _env(doc, WORKER), _env(doc, "ExtractSessionFunction")
    assert env["ANTHROPIC_API_KEY"] == {"Ref": "ClaudeApiKey"}
    assert env["SUPPORT_WRITER_FUNCTION"] == {"Ref": WRITER}
    assert env["S3_BUCKET"] == {"Ref": "IngestBucketName"}
    for k in ("DEVICE_ANNOUNCEMENT_PATTERNS", "FILTER_AUDIO_EVENT_TAGS"):
        assert env[k] == ext[k], k
    assert {"LambdaInvokePolicy": {"FunctionName": {"Ref": WRITER}}} in p["Policies"]
    assert not any("OPENROUTER" in k for k in env)


def test_worker_reads_what_the_extractor_reads_to_rebuild_turns(doc):
    got, prefixes = _grants(doc, WORKER)
    ext_got, ext_prefixes = _grants(doc, "ExtractSessionFunction")
    turns = {LAKE + "/transcripts/*", LAKE + "/voice_ask/*", LAKE + "/audio_segments/*"}
    assert turns <= ext_got["s3:GetObject"]
    assert got["s3:GetObject"] == turns | {LAKE + "/nightly_requests/*"}
    assert {"transcripts/*", "voice_ask/*"} <= ext_prefixes
    assert prefixes == {"transcripts/*", "voice_ask/*", "nightly_requests/*"}
    assert set(got) == {"s3:GetObject", "s3:ListBucket"}       # no writes, no deletes


def test_writer_is_in_vpc_with_the_db_secret(doc):
    p = _props(doc, WRITER)
    assert p["Handler"] == "lambda_nightly_support_writer.lambda_handler"
    assert p["Timeout"] == 120                 # the worker waits 130 s for it
    ref = _props(doc, "ContinuityLinkWriterFunction")
    assert p["VpcConfig"] == ref["VpcConfig"] and p["Layers"] == ref["Layers"]
    env, ref_env = _env(doc, WRITER), _env(doc, "ContinuityLinkWriterFunction")
    for k in ("PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD"):
        assert env[k] == ref_env[k], k
    assert "Events" not in p


def test_anthropic_layer_is_pinned():
    req = io.open(os.path.join(REPO, "infra", "anthropic-layer", "requirements.txt"),
                  encoding="utf-8").read()
    pins = [ln.strip() for ln in req.splitlines() if ln.strip() and not ln.startswith("#")]
    assert len(pins) == 1 and re.fullmatch(r"anthropic==\d+\.\d+\.\d+", pins[0])


def test_nothing_outside_the_condition_refers_to_the_new_functions(doc):
    for name, res in doc["Resources"].items():
        if res.get("Condition") == COND:
            continue
        blob = json.dumps(res)
        for fn in (WORKER, WRITER, "AnthropicLayer"):
            assert fn not in blob, (name, fn)


# ---------------------------------------------------------------- the wire script

def test_wire_script_routes_nightly_requests_only_when_the_operator_asks():
    text = io.open(WIRE, encoding="utf-8").read()
    assert '"Id":"fs-nightly-turn-pick"' in text
    assert re.search(r'"prefix","Value":"nightly_requests/"\},\{"Name":"suffix",'
                     r'"Value":"\.json"', text)
    assert "nightly-turn-pick" in text
    # gated: never added just because the function exists (it exists in shadow too)
    block = text[text.index("nightly_turn_pick_mode()"):]
    assert 'WIRE_NIGHTLY_TURN_PICK:-}" = "true"' in block[:400]
