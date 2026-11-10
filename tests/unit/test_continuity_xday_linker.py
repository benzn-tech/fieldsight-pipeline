"""Unit: hop 2 of cross-day item continuity -- the non-VPC linker (spec 2026-11-09 X3/X6/X7/X9/X10).

S3, the model and the writer invoke are stubbed. The linker stays idle unless BOTH its own
CROSS_DAY_CONTINUITY env and the artifact's `mode` are "on"; a model failure costs no invoke
and no raise (a retry would only repeat the paid call); a failed writer invoke raises, so the
S3 retry is fail-closed; neither logs nor the EMF lines carry item text.
"""
import io
import json
import logging
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import continuity_xday  # noqa: E402
import lambda_continuity_xday_linker as linker  # noqa: E402
import llm_utils  # noqa: E402

SITE = "11111111-1111-1111-1111-111111111111"
SECRET_NEW = "Chase the plumber about the riser leak ZQX-NEW"
SECRET_OLD = "Plumber to fix riser leak on level three ZQX-OLD"
SECRET_OTHER = "Order scaffold boards for block B ZQX-OTHER"
SECRET_NEW2 = "Toolbox talk on ladders ZQX-NEW2"
SECRET_NEW3 = "Confirm pour date with the engineer ZQX-NEW3"
SECRETS = ("ZQX-NEW", "ZQX-OLD", "ZQX-OTHER", "ZQX-NEW2", "ZQX-NEW3",
           "Plumber", "plumber", "scaffold", "Toolbox", "engineer")


def _request(mode="on"):
    new_items = {"action_items": [
        {"stable_id": "s-new", "item_id": "i-new", "text": SECRET_NEW},
        {"stable_id": "s-new2", "item_id": "i-new2", "text": SECRET_NEW2},
        {"stable_id": "s-new3", "item_id": "i-new3", "text": SECRET_NEW3},
    ]}
    candidates = {"action_items": [
        {"stable_id": "s-old", "item_id": "i-old", "text": SECRET_OLD,
         "first_date": "2026-11-02", "first_seen_recorded_at": None},
        {"stable_id": "s-other", "item_id": "i-other", "text": SECRET_OTHER,
         "first_date": "2026-11-03", "first_seen_recorded_at": None},
    ]}
    req = continuity_xday.build_request(
        site_id=SITE, company_id="c1", extraction_key="extractions/x/final.json",
        extracted_at="2026-11-09T01:00:00Z", date="2026-11-09", new_items=new_items,
        candidates=candidates, precap_counts={"action_items": 2})
    req["trigger"] = "final"
    req["lexical_skipped_budget"] = 0
    if mode is not None:
        req["mode"] = mode
    return req


GOOD_ANSWER = json.dumps({"links": [
    {"ref": "N1", "continues": {"id": "A1", "starts": "Plumber to fix riser"}}]})


class _FakeS3:
    def __init__(self, body):
        self.body = body
        self.gets = []

    def get_object(self, Bucket, Key):
        self.gets.append((Bucket, Key))
        return {"Body": io.BytesIO(self.body)}


class _FakeLambda:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or {
            "StatusCode": 200,
            "Payload": io.BytesIO(json.dumps(
                {"inserted": 1, "skipped": {"dst_gone": 0}, "records": 2}).encode())}

    def invoke(self, **kw):
        self.calls.append(kw)
        return self.response


class _Ctx:
    def get_remaining_time_in_millis(self):
        return 600_000


def _event(key="continuity_xday_requests/%s/2026-11-09/abc.json" % SITE):
    return {"Records": [{"s3": {"bucket": {"name": "lake"},
                                "object": {"key": key}}}]}


@pytest.fixture
def wire(monkeypatch):
    """Install fakes; returns a dict the test fills/reads."""
    state = {"llm_calls": [], "answer": (GOOD_ANSWER, None)}

    def setup(request, env="on", lam=None):
        s3 = _FakeS3(json.dumps(request).encode() if not isinstance(request, bytes)
                     else request)
        lam = lam or _FakeLambda()
        monkeypatch.setattr(linker, "s3", lambda: s3)
        monkeypatch.setattr(linker, "lambda_client", lambda: lam)
        monkeypatch.setenv("CROSS_DAY_CONTINUITY", env)
        monkeypatch.setattr(linker, "LINK_WRITER_FUNCTION", "fs-test-continuity-link-writer")
        monkeypatch.setattr(linker, "S3_BUCKET", "lake")

        def fake_call_llm(prompt, max_tokens=4096, force_json=False, enable_thinking=None,
                          model=None, deadline=None, caller="unknown", info=None):
            state["llm_calls"].append({"prompt": prompt, "enable_thinking": enable_thinking,
                                       "caller": caller, "deadline": deadline})
            text, err = state["answer"]
            if info is not None and err is None:
                info["model"] = "qwen3.8-flash"
                info["served_model"] = "qwen/qwen3.8-flash-2026-10"
            return text, err

        monkeypatch.setattr(llm_utils, "call_llm", fake_call_llm)
        state["s3"], state["lam"] = s3, lam
        return state

    return setup


def _no_text(blob):
    for s in SECRETS:
        assert s not in blob, s


# --- the gate ---------------------------------------------------------------------------------

@pytest.mark.parametrize("env,mode", [
    ("shadow", "on"), ("off", "on"), ("on", "shadow"), ("on", "off"), ("on", None),
    ("shadow", "shadow"),
])
def test_idle_unless_both_env_and_artifact_say_on(wire, env, mode):
    st = wire(_request(mode=mode), env=env)
    linker.lambda_handler(_event(), _Ctx())
    assert st["llm_calls"] == []
    assert st["lam"].calls == []


def test_env_off_does_not_even_read_the_artifact(wire):
    st = wire(_request(), env="off")
    linker.lambda_handler(_event(), _Ctx())
    assert st["s3"].gets == []


# --- the happy path ---------------------------------------------------------------------------

def test_on_calls_the_model_once_thinking_off_and_invokes_the_writer_once(wire):
    st = wire(_request())
    linker.lambda_handler(_event(), _Ctx())
    assert len(st["llm_calls"]) == 1
    call = st["llm_calls"][0]
    assert call["enable_thinking"] is False
    assert call["caller"] == "continuity_xday"
    assert call["prompt"] == continuity_xday.render_prompt(_request())
    # the chain may not eat the writer's window
    assert call["deadline"] is not None and call["deadline"] <= 600 - 120

    assert len(st["lam"].calls) == 1
    kw = st["lam"].calls[0]
    assert kw["FunctionName"] == "fs-test-continuity-link-writer"
    assert kw["InvocationType"] == "RequestResponse"
    payload = json.loads(kw["Payload"])
    assert payload["links"] == [{"src_stable_id": "s-new", "src_item_id": "i-new",
                                 "src_kind": "action_item", "dst_stable_id": "s-old"}]
    assert [c["outcome"] for c in payload["claims"]] == ["accepted"]
    assert payload["model"] == {"provider": llm_utils.LLM_PROVIDER, "model": "qwen3.8-flash",
                                "model_version": "qwen/qwen3.8-flash-2026-10"}


def test_the_payload_carries_no_item_text_but_keeps_every_other_field(wire):
    st = wire(_request())
    linker.lambda_handler(_event(), _Ctx())
    raw = st["lam"].calls[0]["Payload"]
    _no_text(raw)
    req = json.loads(raw)["request"]
    for entry in req["new_items"] + req["candidates"]:
        assert "text" not in entry
    full = _request()
    for k in full:
        assert k in req, k
    assert req["candidates"][0]["first_date"] == "2026-11-02"
    assert req["new_items"][0]["stable_id"] == "s-new"
    assert req["mode"] == "on" and req["trigger"] == "final"


def test_reads_the_artifact_from_its_key(wire):
    st = wire(_request())
    key = "continuity_xday_requests/%s/2026-11-09/a%%2Bb.json" % SITE
    linker.lambda_handler(_event(key), _Ctx())
    assert st["s3"].gets == [("lake", "continuity_xday_requests/%s/2026-11-09/a+b.json" % SITE)]


# --- fail-closed writer -----------------------------------------------------------------------

def test_a_writer_function_error_raises_so_s3_retries(wire):
    lam = _FakeLambda({"StatusCode": 200, "FunctionError": "Unhandled",
                       "Payload": io.BytesIO(b'{"errorMessage": "boom"}')})
    wire(_request(), lam=lam)
    with pytest.raises(RuntimeError):
        linker.lambda_handler(_event(), _Ctx())


def test_a_non_200_invoke_raises(wire):
    lam = _FakeLambda({"StatusCode": 500, "Payload": io.BytesIO(b"{}")})
    wire(_request(), lam=lam)
    with pytest.raises(RuntimeError):
        linker.lambda_handler(_event(), _Ctx())


# --- model failure: no invoke, no raise -------------------------------------------------------

@pytest.mark.parametrize("answer", [
    (None, "HTTP 503"),
    ("I think N1 continues A1 " + SECRET_OLD, None),
    ("[1, 2, 3]", None),
    ("", None),
])
def test_a_model_error_or_unparseable_answer_invokes_nothing_and_does_not_raise(
        wire, caplog, capsys, answer):
    st = wire(_request())
    st["answer"] = answer
    with caplog.at_level(logging.DEBUG):
        out = linker.lambda_handler(_event(), _Ctx())
    assert st["lam"].calls == []
    assert out["model_failed"] == 1
    _no_text(caplog.text)
    _no_text(capsys.readouterr().out)


def test_a_malformed_artifact_is_logged_and_skipped(wire, caplog):
    st = wire(b"not json")
    with caplog.at_level(logging.DEBUG):
        linker.lambda_handler(_event(), _Ctx())
    assert st["llm_calls"] == [] and st["lam"].calls == []


# --- logs and metrics carry counts only -------------------------------------------------------

def test_logs_contain_no_item_text(wire, caplog, capsys):
    st = wire(_request())
    st["answer"] = (json.dumps({"links": [
        {"ref": "N1", "continues": {"id": "A1", "starts": "Plumber to fix riser"}},
        {"ref": "N2", "continues": {"id": "A9", "starts": SECRET_NEW2}}]}), None)
    with caplog.at_level(logging.DEBUG):
        linker.lambda_handler(_event(), _Ctx())
    _no_text(caplog.text)
    _no_text(capsys.readouterr().out)


def _emf_lines(out):
    lines = []
    for ln in out.splitlines():
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        if isinstance(obj, dict) and "_aws" in obj:
            lines.append(obj)
    return lines


def test_emits_xday_links_by_outcome(wire, capsys, monkeypatch):
    monkeypatch.setenv("STAGE", "test")
    st = wire(_request())
    st["answer"] = (json.dumps({"links": [
        {"ref": "N1", "continues": {"id": "A1", "starts": "Plumber to fix riser"}},
        {"ref": "N2", "continues": {"id": "A2", "starts": "x"}},
        {"ref": "N3", "continues": {"id": "A9", "starts": "y"}}]}), None)
    linker.lambda_handler(_event(), _Ctx())
    emf = _emf_lines(capsys.readouterr().out)
    by = {}
    for obj in emf:
        m = obj["_aws"]["CloudWatchMetrics"][0]
        assert m["Namespace"] == "FieldSight/Pipeline"
        assert m["Dimensions"] == [["Stage", "Outcome"]]
        assert m["Metrics"] == [{"Name": "XdayLinks", "Unit": "Count"}]
        assert obj["Stage"] == "test"
        by[obj["Outcome"]] = obj["XdayLinks"]
    assert by["accepted"] == 1
    assert by["rejected_echo"] == 1
    assert by["rejected_existence"] == 1
    assert by["fuse_tripped"] == 0
    assert by["inserted"] == 1
    assert by["skipped"] == 0


def _four_new_request():
    """4 new items, 3 candidates: room for 3 of 4 linked, which trips the fuse (X7 rev 4)."""
    new_items = {"action_items": [
        {"stable_id": f"s-new{i}", "item_id": f"i-new{i}", "text": t}
        for i, t in enumerate((SECRET_NEW, SECRET_NEW2, SECRET_NEW3, "Fourth item ZQX-NEW4"))]}
    candidates = {"action_items": [
        {"stable_id": "s-old", "item_id": "i-old", "text": SECRET_OLD,
         "first_date": "2026-11-02", "first_seen_recorded_at": None},
        {"stable_id": "s-other", "item_id": "i-other", "text": SECRET_OTHER,
         "first_date": "2026-11-03", "first_seen_recorded_at": None},
        {"stable_id": "s-third", "item_id": "i-third", "text": "Book the crane for the lift",
         "first_date": "2026-11-04", "first_seen_recorded_at": None},
    ]}
    req = continuity_xday.build_request(
        site_id=SITE, company_id="c1", extraction_key="extractions/x/final.json",
        extracted_at="2026-11-09T01:00:00Z", date="2026-11-09", new_items=new_items,
        candidates=candidates, precap_counts={"action_items": 3})
    req["mode"] = "on"
    return req


def test_a_fuse_trip_is_counted_and_nothing_accepted_is_sent(wire, capsys):
    st = wire(_four_new_request())
    st["answer"] = (json.dumps({"links": [
        {"ref": "N1", "continues": {"id": "A1", "starts": "Plumber to fix riser"}},
        {"ref": "N2", "continues": {"id": "A2", "starts": "Order scaffold boards for"}},
        {"ref": "N3", "continues": {"id": "A3", "starts": "Book the crane for"}}]}), None)
    linker.lambda_handler(_event(), _Ctx())
    payload = json.loads(st["lam"].calls[0]["Payload"])
    assert payload["links"] == []
    assert {c["guard"] for c in payload["claims"]} == {"fuse"}
    by = {o["Outcome"]: o["XdayLinks"] for o in _emf_lines(capsys.readouterr().out)}
    assert by["fuse_tripped"] == 1 and by["rejected_fuse"] == 3


def test_two_of_three_new_items_linked_is_under_the_fuse_floor(wire, capsys):
    """3 new items, 2 linked: below the floor of 4 the fuse does not apply (X7 rev 4)."""
    st = wire(_request())
    st["answer"] = (json.dumps({"links": [
        {"ref": "N1", "continues": {"id": "A1", "starts": "Plumber to fix riser"}},
        {"ref": "N2", "continues": {"id": "A2", "starts": "Order scaffold boards for"}}]}), None)
    linker.lambda_handler(_event(), _Ctx())
    payload = json.loads(st["lam"].calls[0]["Payload"])
    assert len(payload["links"]) == 2
    by = {o["Outcome"]: o["XdayLinks"] for o in _emf_lines(capsys.readouterr().out)}
    assert by["fuse_tripped"] == 0 and by["accepted"] == 2


# --- the writer client and a failing judge (final review I3/I4) -------------------------------

def test_the_writer_client_waits_for_the_writer_and_never_retries(monkeypatch):
    made = []
    monkeypatch.setattr(linker, "_lambda_client", None)
    monkeypatch.setattr(linker.boto3, "client",
                        lambda name, **kw: made.append((name, kw)) or object())
    linker.lambda_client()
    linker.lambda_client()                      # cached: one client
    assert len(made) == 1 and made[0][0] == "lambda"
    cfg = made[0][1]["config"]
    assert cfg.read_timeout == 130 and cfg.connect_timeout == 5
    assert cfg.retries == {"max_attempts": 0}


@pytest.mark.parametrize("target", ["judge", "strip_texts", "payload"])
def test_a_failure_after_the_model_call_invokes_nothing_and_does_not_raise(
        wire, monkeypatch, caplog, capsys, target):
    st = wire(_request())

    def boom(*a, **k):
        raise ValueError("leaked " + SECRET_NEW)

    if target == "judge":
        monkeypatch.setattr(continuity_xday, "judge", boom)
    elif target == "strip_texts":
        monkeypatch.setattr(linker, "strip_texts", boom)
    else:
        monkeypatch.setattr(linker, "_writer_payload", boom)
    with caplog.at_level(logging.DEBUG):
        out = linker.lambda_handler(_event(), _Ctx())
    assert len(st["llm_calls"]) == 1
    assert st["lam"].calls == []
    assert out["judge_failed"] == 1 and out["processed"] == 0
    assert "ValueError" in caplog.text
    _no_text(caplog.text)
    printed = capsys.readouterr().out
    _no_text(printed)
    by = {o["Outcome"]: o["XdayLinks"] for o in _emf_lines(printed)}
    assert by == {"judge_failed": 1}


def test_an_unreadable_writer_result_does_not_raise(wire):
    lam = _FakeLambda({"StatusCode": 200, "Payload": io.BytesIO(b'["not", "a", "dict"]')})
    wire(_request(), lam=lam)
    out = linker.lambda_handler(_event(), _Ctx())
    assert out["processed"] == 1 and len(lam.calls) == 1


# --- wiring -----------------------------------------------------------------------------------

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def _block(text, header):
    start = text.index(f"\n  {header}:\n")
    nxt = re.search(r"\n  [A-Za-z][A-Za-z0-9]*:\n", text[start + 1:])
    return text[start:start + 1 + nxt.start()] if nxt else text[start:]


def _template():
    return io.open(os.path.join(ROOT, "src", "template.yaml"), encoding="utf-8").read()


def test_template_declares_the_linker():
    b = _block(_template(), "ContinuityXdayLinkerFunction")
    assert '"${P}-continuity-xday-linker"' in b
    assert "Handler: lambda_continuity_xday_linker.lambda_handler" in b
    assert "Condition: HasDb" in b
    assert not re.search(r"^      VpcConfig:", b, re.M), "the linker calls the model: outside the VPC"
    assert re.search(r"EventInvokeConfig:\n\s+MaximumRetryAttempts: 1\n", b)
    assert "LINK_WRITER_FUNCTION: !Ref ContinuityLinkWriterFunction" in b
    assert "CROSS_DAY_CONTINUITY: !Ref CrossDayContinuity" in b
    assert "STAGE: !Ref Stage" in b
    assert "S3_BUCKET: !Ref IngestBucketName" in b
    assert "QWEN_MODEL: !Ref QwenModelPlus" in b
    assert "QWEN_MODEL_NONTHINKING: !Ref QwenModelPlusNonThinking" in b
    assert re.search(r"LambdaInvokePolicy:\n\s+FunctionName: !Ref ContinuityLinkWriterFunction", b)
    assert re.search(r"Action: s3:GetObject\n\s+Resource:\n\s+- !Sub arn:aws:s3:::"
                     r"\$\{IngestBucketName\}/continuity_xday_requests/\*", b)
    assert "- continuity_xday_requests/*" in b   # ListBucket condition: 404, not 403


def test_linker_timeout_covers_the_model_chain_plus_the_writer():
    text = _template()
    b = _block(text, "ContinuityXdayLinkerFunction")
    w = _block(text, "ContinuityLinkWriterFunction")
    timeout = int(re.search(r"^      Timeout: (\d+)", b, re.M).group(1))
    writer = int(re.search(r"^      Timeout: (\d+)", w, re.M).group(1))
    budget = int(re.search(r"LLM_CHAIN_BUDGET_SECONDS: '(\d+)'", b).group(1))
    assert budget + writer < timeout
    assert linker.WRITER_RESERVE_S >= writer


def test_wire_script_routes_the_prefix_to_the_linker():
    sys.path.insert(0, os.path.dirname(__file__))
    from test_search_index_wiring import _wire_rules
    rules = _wire_rules()
    mine = [r for r in rules if r[0] == "fs-continuity-xday"]
    assert mine == [("fs-continuity-xday", "XDAY_LINKER_ARN", "continuity_xday_requests/",
                     ".json")]
    for o in rules:
        if o[0] != "fs-continuity-xday":
            assert not o[2].startswith(mine[0][2]) and not mine[0][2].startswith(o[2]), o
    script = io.open(os.path.join(ROOT, "scripts", "wire-s3-events.sh"), encoding="utf-8").read()
    assert 'XDAY_LINKER_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:' \
           '${PREFIX}-continuity-xday-linker"' in script
    assert "ContinuityXdayLinker=${PREFIX}-continuity-xday-linker" in script
