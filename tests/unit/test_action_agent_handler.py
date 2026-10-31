"""Unit: the ActionAgent worker's skeleton (spec 2026-11-01 §4.1, §4.5, §14 S1/S8/S9).

S3 event -> claim -> stages -> store/fail. Every client is a fake: no boto3 call, no model,
no network. The stage runner is injected, so these tests pin the seam Task 11/12 plug into.
"""
import io
import json

import pytest

import lambda_action_agent as agent

PACK = "11111111-2222-4333-8444-555555555555"
SUB = "cognito-sub-1"
SITE = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
COMPANY = "99999999-8888-4777-8666-555555555555"


class _ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self, objects=None, forbidden=()):
        self.objects = dict(objects or {})
        self.forbidden = set(forbidden)
        self.calls = []

    def _lookup(self, op, Bucket, Key):
        self.calls.append((op, Bucket, Key))
        if Key in self.forbidden:
            raise _ClientError("403")
        if (Bucket, Key) not in self.objects:
            raise _ClientError("NoSuchKey" if op == "get_object" else "404")
        return self.objects[(Bucket, Key)]

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self._lookup("get_object", Bucket, Key))}

    def head_object(self, Bucket, Key):
        self._lookup("head_object", Bucket, Key)
        return {}

    def put_object(self, **kw):
        self.calls.append(("put_object", kw["Bucket"], kw["Key"]))
        self.objects[(kw["Bucket"], kw["Key"])] = kw.get("Body", b"")
        return {}


class FakeOrgApi:
    """A lambda client whose org-api answers each internal task from a script."""

    def __init__(self, claim=None, heartbeat=None, store=None, function_error=None):
        self.invocations = []
        self.claim = claim if claim is not None else _claimed()
        self.heartbeat = heartbeat or (lambda ev: {"state": "running"})
        self.store = store or (lambda ev: {"ok": True})
        self.function_error = function_error or {}

    def invoke(self, FunctionName, InvocationType, Payload):
        ev = json.loads(Payload)
        self.invocations.append((FunctionName, InvocationType, ev))
        task = ev.get("task")
        if task in self.function_error:
            return {"StatusCode": 200, "FunctionError": "Unhandled",
                    "Payload": io.BytesIO(b'{"errorMessage": "boom"}')}
        if task == "action_pack_claim":
            body = self.claim(ev) if callable(self.claim) else self.claim
        elif task == "action_pack_heartbeat":
            body = self.heartbeat(ev)
        elif task == "action_pack_store":
            body = self.store(ev)
        elif task == "action_pack_fail":
            body = {"ok": True}
        else:
            raise AssertionError(f"unexpected invoke {ev}")
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(body).encode())}

    def tasks(self):
        return [ev["task"] for _, _, ev in self.invocations]

    def of(self, task):
        return [ev for _, _, ev in self.invocations if ev["task"] == task]


def _claimed(pack_id=PACK):
    return {"claimed": True, "pack": {
        "id": pack_id, "site_id": SITE, "company_id": COMPANY, "instruction": "x",
        "created_by": "u1", "created_by_sub": SUB, "created_at": "2026-11-01T00:00:00+00:00"}}


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _request(pack_id=PACK):
    return json.dumps({"pack_id": pack_id, "caller_sub": SUB, "site_id": SITE}).encode()


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ACTION_PACKS", "internal")
    monkeypatch.setenv("ORG_API_FUNCTION", "fs-org-api")
    monkeypatch.setenv("RAG_SEARCH_FUNCTION", "fs-rag-search")
    monkeypatch.setenv("S3_BUCKET", "data-bkt")
    monkeypatch.setenv("LAKE_BUCKET", "lake-bkt")


def _key(pack_id=PACK, ext="json"):
    return f"action_pack_requests/{pack_id}.{ext}"


def _s3_with_request(pack_id=PACK):
    return FakeS3({("data-bkt", _key(pack_id)): _request(pack_id)})


GOOD_PAYLOAD = {"title": "t", "reading": "r", "sources": [], "actions": []}


def _run(key, s3, lam, runner, clock=None, remaining_s=900.0):
    return agent.process_key(key, s3=s3, lambda_client=lam, stage_runner=runner,
                             clock=clock or Clock(), remaining_s=remaining_s)


def _event(*keys):
    return {"Records": [{"s3": {"bucket": {"name": "data-bkt"}, "object": {"key": k}}}
                        for k in keys]}


# ---- the switch and the key filter ----------------------------------------------------

@pytest.mark.parametrize("value", [None, "", "off", "OFF ", "yes", "true"])
def test_flag_off_returns_without_any_invoke_or_read(env, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("ACTION_PACKS")
    else:
        monkeypatch.setenv("ACTION_PACKS", value)
    s3, lam, ran = _s3_with_request(), FakeOrgApi(), []
    assert _run(_key(), s3, lam, lambda ctx: ran.append(ctx)) == "disabled"
    assert lam.invocations == [] and s3.calls == [] and ran == []


def test_flag_off_handler_builds_no_client(env, monkeypatch):
    monkeypatch.setenv("ACTION_PACKS", "off")
    monkeypatch.setattr(agent, "_s3", lambda: pytest.fail("no s3 client with the flag off"))
    monkeypatch.setattr(agent, "_lambda", lambda: pytest.fail("no lambda client"))
    assert agent.lambda_handler(_event(_key()), None) == {"ok": True, "outcomes": ["disabled"]}


@pytest.mark.parametrize("key", [
    "action_pack_requests/not-a-uuid.json",
    f"action_pack_requests/{PACK}.cancel",
    f"action_pack_requests/x/{PACK}.json",
    f"other/{PACK}.json",
    f"action_pack_requests/{PACK}.json.bak",
])
def test_keys_that_are_not_a_pack_request_are_ignored(env, key):
    s3, lam = _s3_with_request(), FakeOrgApi()
    assert _run(key, s3, lam, lambda ctx: pytest.fail("ran")) == "ignored"
    assert lam.invocations == [] and s3.calls == []


def test_parse_request_key_normalises_the_uuid():
    assert agent.parse_request_key(_key(PACK.upper())) == PACK
    assert agent.parse_request_key(_key()) == PACK


def test_handler_url_decodes_the_key(env, monkeypatch):
    seen = []
    monkeypatch.setattr(agent, "process_key", lambda key, **kw: seen.append(key) or "x")
    monkeypatch.setattr(agent, "_s3", lambda: object())
    monkeypatch.setattr(agent, "_lambda", lambda: object())
    agent.lambda_handler(_event("action_pack_requests/a%2Bb+c.json"), None)
    assert seen == ["action_pack_requests/a+b c.json"]


# ---- the request object and the claim --------------------------------------------------

def test_missing_request_object_exits_before_the_claim(env):
    s3, lam = FakeS3(), FakeOrgApi()
    assert _run(_key(), s3, lam, lambda ctx: pytest.fail("ran")) == "no_request"
    assert lam.invocations == []


def test_request_whose_pack_id_disagrees_with_its_key_is_ignored(env):
    other = "11111111-2222-4333-8444-000000000000"
    s3 = FakeS3({("data-bkt", _key()): _request(other)})
    lam = FakeOrgApi()
    assert _run(_key(), s3, lam, lambda ctx: pytest.fail("ran")) == "bad_request"
    assert lam.invocations == []


def test_missed_claim_exits_without_the_stage_runner_or_any_other_invoke(env):
    s3, lam = _s3_with_request(), FakeOrgApi(claim={"claimed": False})
    assert _run(_key(), s3, lam, lambda ctx: pytest.fail("stage runner called")) == "lost"
    assert lam.tasks() == ["action_pack_claim"]
    fn, kind, ev = lam.invocations[0]
    assert (fn, kind) == ("fs-org-api", "RequestResponse")
    assert ev == {"task": "action_pack_claim", "pack_id": PACK}
    # no cancel probe, no heartbeat: nothing after the lost claim
    assert [c[0] for c in s3.calls] == ["get_object"]


@pytest.mark.parametrize("claim, outcome", [
    ({"claimed": False, "reason": "company_inactive"}, "lost"),
    ({"claimed": False, "error": "disabled"}, "lost"),
    ({"claimed": "yes", "pack": {}}, "lost"),
    ({"claimed": True}, "lost"),
    ({"statusCode": 404, "body": "{}"}, "claim_error"),   # an API envelope: a routing miss
    (["not", "a", "dict"], "claim_error"),
])
def test_any_claim_answer_but_claimed_true_spends_nothing(env, claim, outcome):
    s3, lam = _s3_with_request(), FakeOrgApi(claim=lambda ev: claim)
    assert _run(_key(), s3, lam, lambda ctx: pytest.fail("ran")) == outcome
    assert lam.tasks() == ["action_pack_claim"]


def test_claim_function_error_spends_nothing_and_does_not_fail_the_pack(env):
    """A crashed claim rolled back its transaction: the pack is still queued (the sweep
    owns it). Failing it from here could fail a pack another delivery is running."""
    s3 = _s3_with_request()
    lam = FakeOrgApi(function_error={"action_pack_claim"})
    assert _run(_key(), s3, lam, lambda ctx: pytest.fail("ran")) == "claim_error"
    assert lam.tasks() == ["action_pack_claim"]


def test_claim_for_a_different_pack_is_failed_internal_and_not_run(env):
    s3 = _s3_with_request()
    lam = FakeOrgApi(claim=_claimed("11111111-2222-4333-8444-000000000000"))
    assert _run(_key(), s3, lam, lambda ctx: pytest.fail("ran")) == "failed:internal"
    assert lam.of("action_pack_fail") == [
        {"task": "action_pack_fail", "pack_id": PACK, "reason": "internal"}]


def test_duplicate_delivery_runs_the_stages_once(env):
    state = {"claimed": False}

    def claim(ev):
        if state["claimed"]:
            return {"claimed": False}
        state["claimed"] = True
        return _claimed()

    s3, lam, runs = _s3_with_request(), FakeOrgApi(claim=claim), []

    def runner(ctx):
        runs.append(ctx.pack_id)
        return dict(GOOD_PAYLOAD)

    assert _run(_key(), s3, lam, runner) == "stored"
    assert _run(_key(), s3, lam, runner) == "lost"
    assert runs == [PACK]
    assert lam.tasks().count("action_pack_store") == 1


def test_handler_processes_each_record_and_reports_outcomes(env, monkeypatch):
    s3 = _s3_with_request()
    lam = FakeOrgApi()
    monkeypatch.setattr(agent, "_s3", lambda: s3)
    monkeypatch.setattr(agent, "_lambda", lambda: lam)
    monkeypatch.setattr(agent, "run_stages", lambda ctx: dict(GOOD_PAYLOAD))

    class Ctx:
        def get_remaining_time_in_millis(self):
            return 899_000

    out = agent.lambda_handler(_event(_key(), "other/x.json"), Ctx())
    assert out == {"ok": True, "outcomes": ["stored", "ignored"]}


# ---- the context the stages receive ----------------------------------------------------

def test_context_carries_the_pack_and_the_caller_identity_from_the_claim(env):
    s3, lam, seen = _s3_with_request(), FakeOrgApi(), []

    def runner(ctx):
        seen.append(ctx)
        return dict(GOOD_PAYLOAD)

    _run(_key(), s3, lam, runner)
    ctx = seen[0]
    assert ctx.pack_id == PACK and ctx.site_id == SITE and ctx.company_id == COMPANY
    assert ctx.caller_sub == SUB                  # from the database row, not the request
    assert ctx.pack["instruction"] == "x"
    assert ctx.s3 is s3 and ctx.lambda_client is lam
    assert ctx.data_bucket == "data-bkt" and ctx.lake_bucket == "lake-bkt"
    assert ctx.org_api_function == "fs-org-api"
    assert ctx.rag_search_function == "fs-rag-search"


def test_budget_is_600s_with_write_and_verify_reserves(env):
    clock, seen = Clock(1000.0), []
    _run(_key(), _s3_with_request(), FakeOrgApi(),
         lambda ctx: seen.append(ctx.budget) or dict(GOOD_PAYLOAD), clock=clock,
         remaining_s=899.0)
    b = seen[0]
    assert b.end == 1600.0
    assert b.verify_deadline == 1600.0
    assert b.write_deadline == 1510.0
    assert b.gather_deadline == 1420.0
    clock.t = 1400.0
    assert b.left(b.gather_deadline) == 20.0
    clock.t = 1500.0
    assert b.left(b.gather_deadline) == 0.0


def test_budget_never_runs_into_the_store_reserve_when_lambda_time_is_short(env):
    seen = []
    _run(_key(), _s3_with_request(), FakeOrgApi(),
         lambda ctx: seen.append(ctx.budget) or dict(GOOD_PAYLOAD), clock=Clock(0.0),
         remaining_s=400.0)
    assert seen[0].end == 400.0 - agent.STORE_RESERVE_S


# ---- checkpoints: cancel marker and heartbeat ------------------------------------------

def test_checkpoint_sends_heartbeat_with_plan_fields(env):
    s3, lam = _s3_with_request(), FakeOrgApi()

    def runner(ctx):
        ctx.checkpoint(reading="the reading", title="the title")
        ctx.checkpoint()
        return dict(GOOD_PAYLOAD)

    assert _run(_key(), s3, lam, runner) == "stored"
    beats = lam.of("action_pack_heartbeat")
    assert beats[0] == {"task": "action_pack_heartbeat", "pack_id": PACK,
                        "reading": "the reading", "title": "the title"}
    assert beats[1] == {"task": "action_pack_heartbeat", "pack_id": PACK}
    marker = ("data-bkt", f"action_pack_requests/{PACK}.cancel")
    assert sum(1 for c in s3.calls if c[1:] == marker) >= 2


def test_checkpoint_truncates_plan_fields_to_the_store_caps(env):
    import action_pack_view
    lam = FakeOrgApi()

    def runner(ctx):
        ctx.checkpoint(reading="r" * (action_pack_view.READING_MAX_CHARS + 50),
                       title="t" * (action_pack_view.TITLE_MAX_CHARS + 50))
        return dict(GOOD_PAYLOAD)

    _run(_key(), _s3_with_request(), lam, runner)
    beat = lam.of("action_pack_heartbeat")[0]
    assert len(beat["reading"]) == action_pack_view.READING_MAX_CHARS
    assert len(beat["title"]) == action_pack_view.TITLE_MAX_CHARS


def test_cancel_marker_stops_the_run_at_the_next_checkpoint_and_nothing_is_stored(env):
    s3, lam, rounds = _s3_with_request(), FakeOrgApi(), []

    def runner(ctx):
        for i in range(5):
            ctx.checkpoint()
            rounds.append(i)
            if i == 1:   # the user presses cancel during round 2
                s3.objects[("data-bkt", f"action_pack_requests/{PACK}.cancel")] = b"{}"
        return dict(GOOD_PAYLOAD)

    assert _run(_key(), s3, lam, runner) == "cancelled"
    assert rounds == [0, 1]
    assert "action_pack_store" not in lam.tasks()
    assert "action_pack_fail" not in lam.tasks()


@pytest.mark.parametrize("answer", [{"state": "cancelled"}, {"state": "failed"},
                                    {"state": None}, {"error": "disabled"}])
def test_heartbeat_that_is_not_running_stops_the_run_quietly(env, answer):
    s3, lam = _s3_with_request(), FakeOrgApi(heartbeat=lambda ev: answer)

    def runner(ctx):
        ctx.checkpoint()
        pytest.fail("ran past a stopped checkpoint")

    assert _run(_key(), s3, lam, runner) == "cancelled"
    assert lam.tasks() == ["action_pack_claim", "action_pack_heartbeat"]


def test_a_crashed_heartbeat_does_not_kill_the_run(env):
    """Liveness is best effort: the cancel marker was already checked, and a store into a
    cancelled pack is refused by org-api anyway."""
    lam = FakeOrgApi(function_error={"action_pack_heartbeat"})

    def runner(ctx):
        ctx.checkpoint()
        return dict(GOOD_PAYLOAD)

    assert _run(_key(), _s3_with_request(), lam, runner) == "stored"


def test_a_cancel_marker_read_that_is_denied_fails_the_pack_internal(env):
    """403 is an IAM fault, not "no marker": it must not be read as "keep going"."""
    s3 = _s3_with_request()
    s3.forbidden.add(f"action_pack_requests/{PACK}.cancel")
    lam = FakeOrgApi()

    def runner(ctx):
        ctx.checkpoint()
        return dict(GOOD_PAYLOAD)

    assert _run(_key(), s3, lam, runner) == "failed:internal"
    assert "action_pack_store" not in lam.tasks()


def test_a_cancel_after_the_last_round_is_caught_before_the_store(env):
    s3, lam = _s3_with_request(), FakeOrgApi()

    def runner(ctx):
        s3.objects[("data-bkt", f"action_pack_requests/{PACK}.cancel")] = b"{}"
        return dict(GOOD_PAYLOAD)

    assert _run(_key(), s3, lam, runner) == "cancelled"
    assert "action_pack_store" not in lam.tasks()


# ---- the store and its answers ----------------------------------------------------------

def test_payload_is_stored_once(env):
    lam = FakeOrgApi()
    assert _run(_key(), _s3_with_request(), lam, lambda ctx: dict(GOOD_PAYLOAD)) == "stored"
    assert lam.of("action_pack_store") == [
        {"task": "action_pack_store", "pack_id": PACK, "payload": GOOD_PAYLOAD}]
    assert "action_pack_fail" not in lam.tasks()


@pytest.mark.parametrize("answer", [{"error": "payload_too_large"},
                                    {"error": "invalid", "detail": "sources"},
                                    {"unexpected": True}])
def test_store_rejections_fail_the_pack_internal(env, answer):
    lam = FakeOrgApi(store=lambda ev: answer)
    assert _run(_key(), _s3_with_request(), lam,
                lambda ctx: dict(GOOD_PAYLOAD)) == "failed:internal"
    assert lam.of("action_pack_fail") == [
        {"task": "action_pack_fail", "pack_id": PACK, "reason": "internal"}]


def test_store_function_error_fails_the_pack_internal(env):
    lam = FakeOrgApi(function_error={"action_pack_store"})
    assert _run(_key(), _s3_with_request(), lam,
                lambda ctx: dict(GOOD_PAYLOAD)) == "failed:internal"
    assert lam.of("action_pack_fail")[0]["reason"] == "internal"


def test_store_refused_stops_quietly(env):
    lam = FakeOrgApi(store=lambda ev: {"error": "refused"})
    assert _run(_key(), _s3_with_request(), lam,
                lambda ctx: dict(GOOD_PAYLOAD)) == "refused"
    assert "action_pack_fail" not in lam.tasks()


@pytest.mark.parametrize("payload", [None, [], "x"])
def test_a_non_dict_payload_fails_the_pack_internal(env, payload):
    lam = FakeOrgApi()
    assert _run(_key(), _s3_with_request(), lam, lambda ctx: payload) == "failed:internal"
    assert "action_pack_store" not in lam.tasks()


# ---- exceptions -> reason codes ----------------------------------------------------------

@pytest.mark.parametrize("exc, reason", [
    (agent.BudgetExceeded(), "budget"),
    (agent.ModelFailure(), "model"),
    (agent.ToolsFailure(), "tools"),
    (agent.VerifierFailure(), "verifier"),
    (agent.StageError(), "internal"),
    (NotImplementedError(), "internal"),
    (KeyError("x"), "internal"),
    (RuntimeError("secret instruction text"), "internal"),
])
def test_exceptions_map_to_reason_codes(env, exc, reason, caplog):
    lam = FakeOrgApi()

    def runner(ctx):
        raise exc

    with caplog.at_level("INFO"):
        assert _run(_key(), _s3_with_request(), lam, runner) == f"failed:{reason}"
    assert lam.of("action_pack_fail") == [
        {"task": "action_pack_fail", "pack_id": PACK, "reason": reason}]
    assert "action_pack_store" not in lam.tasks()
    assert "secret instruction text" not in caplog.text


def test_cancelled_raised_by_a_stage_does_nothing_further(env):
    lam = FakeOrgApi()

    def runner(ctx):
        raise agent.Cancelled()

    assert _run(_key(), _s3_with_request(), lam, runner) == "cancelled"
    assert lam.tasks() == ["action_pack_claim"]


def test_every_reason_code_is_one_the_store_accepts():
    from repositories import action_packs
    for cls in (agent.StageError, agent.BudgetExceeded, agent.ModelFailure,
                agent.ToolsFailure, agent.VerifierFailure):
        assert cls.reason in action_packs.FAILURE_REASONS


def test_a_failed_fail_invoke_is_logged_not_raised(env):
    lam = FakeOrgApi(function_error={"action_pack_fail"})

    def runner(ctx):
        raise agent.ModelFailure()

    assert _run(_key(), _s3_with_request(), lam, runner) == "failed:model"


def test_the_default_stage_runner_is_the_stages_module(env, monkeypatch):
    """run_stages delegates to action_pack_stages.run (Task 11); no model or tool is reached
    here -- the stages module is faked."""
    import action_pack_stages
    seen = []
    monkeypatch.setattr(action_pack_stages, "run",
                        lambda ctx: seen.append(ctx.pack_id) or dict(GOOD_PAYLOAD))
    lam = FakeOrgApi()
    assert agent.process_key(_key(), s3=_s3_with_request(), lambda_client=lam,
                             clock=Clock(), remaining_s=900.0) == "stored"
    assert seen == [PACK]


def test_an_unbuilt_verifier_fails_internal(env, monkeypatch):
    """An untyped exception from the stages (here NotImplementedError, what the verifier seam
    raised before Task 12) fails the claimed pack `internal` (behind the flag only)."""
    import action_pack_stages

    def stages(ctx):
        raise NotImplementedError("verifier")
    monkeypatch.setattr(action_pack_stages, "run", stages)
    lam = FakeOrgApi()
    assert agent.process_key(_key(), s3=_s3_with_request(), lambda_client=lam,
                             clock=Clock(), remaining_s=900.0) == "failed:internal"
    assert lam.of("action_pack_fail")[0]["reason"] == "internal"


# ---- model settings ------------------------------------------------------------------

def test_model_settings_fall_back_to_the_existing_ask_settings(monkeypatch):
    for k in ("ACTION_WRITER_MODEL", "ACTION_VERIFIER_MODEL", "ACTION_VERIFIER_FALLBACKS",
              "ACTION_TOKEN_CAP", "ASK_JUDGE_MODEL", "CORROBORATION_CHEAP_MODEL"):
        monkeypatch.delenv(k, raising=False)
    import llm_tools_client
    import web_answer
    assert agent.writer_model() == llm_tools_client.DEFAULT_MODEL
    assert agent.verifier_model() == web_answer.CHEAP_MODEL
    assert agent.verifier_fallbacks() == []
    assert agent.token_cap() == agent.DEFAULT_TOKEN_CAP
    monkeypatch.setenv("ASK_JUDGE_MODEL", "judge/x")
    assert agent.writer_model() == "judge/x"
    monkeypatch.setenv("ACTION_WRITER_MODEL", "w/x")
    monkeypatch.setenv("ACTION_VERIFIER_MODEL", "v/x")
    monkeypatch.setenv("ACTION_VERIFIER_FALLBACKS", " a/b , ,c/d,a/b")
    monkeypatch.setenv("ACTION_TOKEN_CAP", "1234")
    assert (agent.writer_model(), agent.verifier_model()) == ("w/x", "v/x")
    assert agent.verifier_fallbacks() == ["a/b", "c/d"]
    assert agent.token_cap() == 1234
    monkeypatch.setenv("ACTION_TOKEN_CAP", "nonsense")
    assert agent.token_cap() == agent.DEFAULT_TOKEN_CAP


# ---- fix round 1 ---------------------------------------------------------------------

def test_a_request_read_error_is_a_per_record_outcome_not_a_crash(env, monkeypatch):
    """A 403/5xx on the request object must not escape lambda_handler (it would abort the
    other records in the event, and with retries 0 nothing would say so)."""
    s3 = _s3_with_request()
    s3.forbidden.add(_key())
    lam = FakeOrgApi(claim=lambda ev: _claimed(ev["pack_id"]))
    assert _run(_key(), s3, lam, lambda ctx: pytest.fail("ran")) == "request_error"
    assert lam.invocations == []
    other = "11111111-2222-4333-8444-000000000000"
    s3.objects[("data-bkt", _key(other))] = _request(other)
    monkeypatch.setattr(agent, "_s3", lambda: s3)
    monkeypatch.setattr(agent, "_lambda", lambda: lam)
    monkeypatch.setattr(agent, "run_stages", lambda ctx: dict(GOOD_PAYLOAD))
    out = agent.lambda_handler(_event(_key(), _key(other)), None)
    assert out == {"ok": True, "outcomes": ["request_error", "stored"]}


def test_ctx_gives_each_stage_its_deadline_and_the_time_left(env):
    clock, seen = Clock(1000.0), []
    _run(_key(), _s3_with_request(), FakeOrgApi(),
         lambda ctx: seen.append(ctx) or dict(GOOD_PAYLOAD), clock=clock, remaining_s=899.0)
    ctx = seen[0]
    assert ctx.deadline_for("gather") == 1420.0
    assert ctx.deadline_for("write") == 1510.0
    assert ctx.deadline_for("verify") == 1600.0
    clock.t = 1500.0
    assert ctx.left("gather") == 0.0
    assert ctx.left("write") == 10.0
    assert ctx.left() == 100.0
    with pytest.raises(ValueError):
        ctx.deadline_for("plan")
