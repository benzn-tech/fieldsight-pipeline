"""Ask routing eval runner (plan 2026-10-16 phase 4, Task 3).

Fake boto3 clients, no network. The runner is loaded by path: the directory
name has a hyphen.
"""
import importlib.util
import io
import json
import os

import pytest
from botocore.exceptions import ClientError

import ask_voice_turn as avt

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EVAL_DIR = os.path.join(REPO, "tools", "ask-routing-eval")


def _load(name):
    spec = importlib.util.spec_from_file_location("ask_routing_eval_" + name,
                                                  os.path.join(EVAL_DIR, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


run = _load("run")

SUB = "d95e24e8-0000-0000-0000-000000000000"
BUCKET = "data-bucket"
STACK = "fieldsight-test"


# ------------------------------------------------------------------- fakes

class FakeS3:
    def __init__(self):
        self.objs = {}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objs[Key] = Body if isinstance(Body, bytes) else Body.encode()

    def get_object(self, Bucket, Key):
        if Key not in self.objs:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
        return {"Body": io.BytesIO(self.objs[Key])}


class FakeLambda:
    def __init__(self, s3, flag="screen,voice", routes=None):
        self.s3 = s3
        self.env = {"ASK_AGENTIC": flag, "ASK_TURN_BUCKET": BUCKET, "ENABLE_WEB_ANSWER": "true",
                    "VOICE_WEB_ENABLED": "true", "QWEN_API_KEY": "secret-key"}
        self.invokes = []
        self.config_calls = []
        self.routes = routes if routes is not None else {}
        self.n = 0

    def get_function_configuration(self, FunctionName):
        self.config_calls.append(FunctionName)
        return {"Environment": {"Variables": dict(self.env)}}

    def _payload(self, body):
        return {"Payload": io.BytesIO(json.dumps(body).encode())}

    def invoke(self, FunctionName, Payload, InvocationType="RequestResponse"):
        ev = json.loads(Payload)
        self.invokes.append({"fn": FunctionName, "type": InvocationType, "event": ev})
        if FunctionName.endswith("-ask-voice-worker"):
            avt.put_state(self.s3, BUCKET, ev["caller_sub"], ev["turn_id"],
                          {"status": "done", "answerText": "Poured.", "web": False,
                           "basis": {}, "audioBase64": "QUFBQQ==", "audioFormat": "wav"})
            self.routes[ev["turn_id"]] = {"turn": ev["turn_id"], "channel": "voice"}
            return {"StatusCode": 202}
        if FunctionName.endswith("-org-api"):
            return self._payload({"statusCode": 200, "body": json.dumps(
                {"sites": [{"id": "site-1", "name": "UC PK"}]})})
        if ev["path"] == "/api/ask/start":
            self.n += 1
            turn = "%032x" % self.n
            self.routes[turn] = {"turn": turn, "channel": "screen"}
            return self._payload({"statusCode": 200, "body": json.dumps({"turn_id": turn})})
        if ev["path"] == "/api/ask/result":
            q = ev["queryStringParameters"]
            body = {"status": "done", "result": {"answer": "Poured [1].",
                                                 "citations": [{"report_date": "2026-09-03"}],
                                                 "from_web": False}}
            if q["since"] == "pending":
                body = {"status": "searching_records"}
            return self._payload({"statusCode": 200, "body": json.dumps(body)})
        raise AssertionError("unexpected invoke " + FunctionName)


class FakeLogs:
    def __init__(self, routes, page=2, drop=()):
        self.routes = routes
        self.page = page
        self.drop = set(drop)
        self.calls = []

    def filter_log_events(self, **kw):
        self.calls.append(kw)
        events = [{"message": "[INFO]\t2026-10-16T00:00:00Z\treq\tASK_ROUTE %s\n"
                              % json.dumps(r, sort_keys=True)}
                  for t, r in self.routes.items() if t not in self.drop]
        events.append({"message": "[INFO]\tvoice ask: rag=1.0s turn=x"})
        start = int(kw.get("nextToken") or 0)
        chunk = events[start:start + self.page]
        out = {"events": chunk}
        if start + self.page < len(events):
            out["nextToken"] = str(start + self.page)
        return out


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture
def world(tmp_path):
    s3 = FakeS3()
    lam = FakeLambda(s3)
    logs = FakeLogs(lam.routes)
    clock = Clock()
    made = []

    def factory():
        made.append(1)
        return {"lambda": lam, "logs": logs, "s3": s3}
    return {"s3": s3, "lam": lam, "logs": logs, "clock": clock, "factory": factory,
            "made": made, "out": str(tmp_path)}


def main(world, *argv):
    return run.main(list(argv), make_clients=world["factory"], clock=world["clock"],
                    sleep=world["clock"].sleep, results_dir=world["out"])


def never():
    raise AssertionError("an AWS client was made")


# ------------------------------------------------------------------ refusals

@pytest.mark.parametrize("stack", ["fieldsight-prod", "FieldSight-PROD", "prod"])
def test_prod_stack_exits_2_before_any_client(stack):
    rc = run.main(["--stack", stack, "--sub", SUB, "--channel", "screen", "--repeat", "1",
                   "--confirm-calls", "100000"], make_clients=never)
    assert rc == 2


def test_prod_stack_refused_even_on_dry_run():
    assert run.main(["--stack", "fieldsight-prod", "--sub", SUB, "--channel", "both",
                     "--dry-run"], make_clients=never) == 2


def test_missing_confirm_calls_exits_2_without_a_client():
    assert run.main(["--stack", STACK, "--sub", SUB, "--channel", "screen", "--repeat", "1"],
                    make_clients=never) == 2


def test_confirm_calls_below_the_estimate_exits_2(capsys):
    est = run.estimate(run.selection(run.load_set(), "screen", None), 3)["total"]
    assert run.main(["--stack", STACK, "--sub", SUB, "--channel", "screen", "--repeat", "3",
                     "--confirm-calls", str(est - 1)], make_clients=never) == 2


def test_dry_run_prints_the_estimate_and_makes_no_client(capsys):
    assert run.main(["--stack", STACK, "--sub", SUB, "--channel", "both", "--repeat", "3",
                     "--dry-run"], make_clients=never) == 0
    out = capsys.readouterr().out
    est = run.estimate(run.selection(run.load_set(), "both", None), 3)
    assert "screen: %d" % est["screen"] in out and "voice: %d" % est["voice"] in out
    assert "total: %d" % est["total"] in out


@pytest.mark.parametrize("flag,channel", [("", "screen"), ("voice", "screen"),
                                          ("screen", "voice"), ("screen", "both")])
def test_flag_without_the_channel_exits_2_before_any_turn(world, flag, channel):
    world["lam"].env["ASK_AGENTIC"] = flag
    rc = main(world, "--stack", STACK, "--sub", SUB, "--channel", channel, "--repeat", "1",
              "--confirm-calls", "100000")
    assert rc == 2
    assert world["lam"].config_calls == [STACK + "-ask-voice-worker"]
    assert world["lam"].invokes == []


# ------------------------------------------------------------------ estimate

def _it(group, channels=("screen", "voice")):
    return {"id": group, "group": group, "question": "q", "channels": list(channels),
            "expect": {}}


def test_estimate_arithmetic():
    items = [_it("project_day"), _it("general"), _it("mixed"), _it("web_cap"),
             _it("scoped_general", ("screen",))]
    for ch in ("screen", "voice"):
        assert run.turn_cost("project_day", ch) == 2
        assert run.turn_cost("explicit_web", ch) == 4.5
        assert run.turn_cost("mixed", ch) == 5.5
        assert run.turn_cost("web_cap", ch) == 5.5
    # Spec 2026-10-16 §6: the screen offers the web on a general question
    # instead of running it, so its web calls are not counted.
    assert run.turn_cost("general", "screen") == 2
    assert run.turn_cost("general", "voice") == 4.5
    est = run.estimate(run.selection(items, "both", None), 3)
    # screen: (2 + 2 + 5.5 + 5.5 + 2) * 3 = 51; voice: (2 + 4.5 + 5.5 + 5.5) * 3
    # = 52.5 -> 53.
    assert est == {"screen": 51, "voice": 53, "total": 104}
    assert run.estimate(run.selection(items, "voice", None), 1) == {"voice": 18, "total": 18}


def test_screen_offer_brings_the_full_set_estimate_down():
    # Before the screen offer (spec 2026-10-16) the full set cost screen 239,
    # voice 233, total 472 at --repeat 3. Only screen general moved: 5 items x
    # 2.5 web calls x 3 repeats = 37.5 fewer.
    both = run.estimate(run.selection(run.load_set(), "both", None), 3)
    assert both == {"screen": 201, "voice": 233, "total": 434}
    assert both["screen"] < 239 and both["total"] < 472
    assert run.estimate(run.selection(run.load_set(), "screen", None), 3) == {
        "screen": 201, "total": 201}


def test_selection_only_and_channels():
    items = [_it("project_day"), _it("scoped_general", ("screen",))]
    sel = run.selection(items, "voice", None)
    assert [(i["id"], c) for i, c in sel] == [("project_day", "voice")]
    sel = run.selection(items, "both", ["scoped_general"])
    assert [(i["id"], c) for i, c in sel] == [("scoped_general", "screen")]


def test_full_set_dry_run_numbers():
    est = run.estimate(run.selection(run.load_set(), "both", None), 3)
    assert est["total"] == est["screen"] + est["voice"]


# ------------------------------------------------------------------- drivers

def test_screen_driver_event_shape(world):
    item = {"id": "sg1", "group": "scoped_general", "question": "Fire watch rules?",
            "channels": ["screen"], "body": {"site_id": "@visible_site"}, "expect": {}}
    turn, terminal, wall = run.screen_turn(world["lam"], STACK + "-api", SUB, item, "site-9",
                                           clock=world["clock"], sleep=world["clock"].sleep)
    start, poll1, poll2 = world["lam"].invokes
    assert start["fn"] == STACK + "-api" and start["type"] == "RequestResponse"
    ev = start["event"]
    assert ev["httpMethod"] == "POST" and ev["path"] == "/api/ask/start"
    assert ev["requestContext"]["authorizer"]["claims"]["sub"] == SUB
    assert json.loads(ev["body"]) == {"question": "Fire watch rules?", "tz": "Pacific/Auckland",
                                      "offer_web": True, "site_id": "site-9"}
    assert poll1["event"]["httpMethod"] == "GET" and poll1["event"]["path"] == "/api/ask/result"
    assert poll1["event"]["queryStringParameters"] == {"turn": turn, "since": "pending"}
    assert poll2["event"]["queryStringParameters"] == {"turn": turn,
                                                      "since": "searching_records"}
    assert terminal["status"] == "done" and terminal["result"]["answer"] == "Poured [1]."


def test_screen_driver_sends_web_intent_only_for_explicit_web(world):
    ew = {"id": "ew1", "group": "explicit_web", "channels": ["screen"], "expect": {},
          "question": "What are the current NZS 3604 bracing requirements? Search online."}
    run.screen_turn(world["lam"], STACK + "-api", SUB, ew, None, clock=world["clock"],
                    sleep=world["clock"].sleep)
    run.screen_turn(world["lam"], STACK + "-api", SUB, _it("general"), None,
                    clock=world["clock"], sleep=world["clock"].sleep)
    bodies = [json.loads(i["event"]["body"]) for i in world["lam"].invokes
              if i["event"].get("path") == "/api/ask/start"]
    assert bodies[0] == {"question": ew["question"], "tz": "Pacific/Auckland",
                         "offer_web": True, "web_intent": True}
    assert bodies[1] == {"question": "q", "tz": "Pacific/Auckland", "offer_web": True}


def test_screen_driver_gives_up_after_70_s(world):
    class Slow(FakeLambda):
        def invoke(self, FunctionName, Payload, InvocationType="RequestResponse"):
            ev = json.loads(Payload)
            if ev.get("path") == "/api/ask/result":
                self.invokes.append({"fn": FunctionName, "event": ev})
                world["clock"].t += 20
                return self._payload({"statusCode": 200, "body": json.dumps(
                    {"status": "searching_records"})})
            return super().invoke(FunctionName, Payload, InvocationType)
    lam = Slow(world["s3"])
    _, terminal, wall = run.screen_turn(lam, STACK + "-api", SUB, _it("general"), None,
                                        clock=world["clock"], sleep=world["clock"].sleep)
    assert terminal["status"] == "timeout" and wall >= 70


def test_voice_driver_state_and_worker_event(world):
    item = {"id": "ew1", "group": "explicit_web", "channels": ["voice"], "expect": {},
            "question": "What are the current NZS 3604 bracing requirements? Search online."}
    seen = []
    real = avt.put_state

    def spy(s3, bucket, sub, turn_id, state):
        seen.append((bucket, sub, turn_id, dict(state)))
        return real(s3, bucket, sub, turn_id, state)
    turn, terminal, wall = run.voice_turn(
        world["lam"], world["s3"], STACK + "-ask-voice-worker", BUCKET, SUB, item,
        clock=world["clock"], sleep=world["clock"].sleep, put_state=spy)
    assert avt.TURN_RE.match(turn)
    (bucket, sub, tid, state), = seen
    assert (bucket, sub, tid) == (BUCKET, SUB, turn) and state["status"] == "pending"
    assert isinstance(state["created_at"], float)
    (inv,) = world["lam"].invokes
    assert inv["fn"] == STACK + "-ask-voice-worker" and inv["type"] == "Event"
    assert inv["event"] == {"turn_id": turn, "caller_sub": SUB, "transcript": item["question"],
                            "tz": "Pacific/Auckland", "web_intent": True, "offer_web": True}
    assert terminal["status"] == "done" and "audioBase64" not in terminal


def test_voice_driver_web_intent_false_without_the_phrase(world):
    run.voice_turn(world["lam"], world["s3"], STACK + "-ask-voice-worker", BUCKET, SUB,
                   _it("general"), clock=world["clock"], sleep=world["clock"].sleep)
    assert world["lam"].invokes[0]["event"]["web_intent"] is False


def test_voice_driver_uses_the_service_state_key(world):
    turn, _, _ = run.voice_turn(world["lam"], world["s3"], STACK + "-ask-voice-worker", BUCKET,
                                SUB, _it("general"), clock=world["clock"],
                                sleep=world["clock"].sleep)
    assert avt.state_key(SUB, turn) in world["s3"].objs


# ------------------------------------------------------------------ log join

def test_parse_route_line():
    msg = '[INFO]\t2026-10-16T00:00:00Z\treq\tASK_ROUTE {"turn": "ab", "refs_r": 1}\n'
    assert run.parse_route(msg) == {"turn": "ab", "refs_r": 1}
    assert run.parse_route("voice ask: turn=ab") is None
    assert run.parse_route("ASK_ROUTE {not json") is None


def test_join_paginates_and_keys_on_turn(world):
    routes = {"a" * 32: {"turn": "a" * 32, "x": 1}, "b" * 32: {"turn": "b" * 32, "x": 2},
              "c" * 32: {"turn": "c" * 32, "x": 3},
              "legacy": {"kind": "general"}}               # a _rag_answer line: no turn
    logs = FakeLogs(routes, page=2)
    got = run.join_routes(logs, "/aws/lambda/w", ["a" * 32, "c" * 32], 1000, 2000,
                          clock=world["clock"], sleep=world["clock"].sleep)
    assert got == {"a" * 32: routes["a" * 32], "c" * 32: routes["c" * 32]}
    kw = logs.calls[0]
    assert kw["logGroupName"] == "/aws/lambda/w" and kw["filterPattern"] == '"ASK_ROUTE"'
    assert kw["startTime"] <= 1000 and kw["endTime"] >= 2000
    assert len(logs.calls) >= 2                            # paginated


def test_join_missing_line_retries_then_gives_up(world):
    logs = FakeLogs({"a" * 32: {"turn": "a" * 32}})
    got = run.join_routes(logs, "/aws/lambda/w", ["a" * 32, "b" * 32], 1000, 2000,
                          clock=world["clock"], sleep=world["clock"].sleep, wait_s=60)
    assert set(got) == {"a" * 32}
    assert len(logs.calls) > 1


# --------------------------------------------------------------- end to end

def test_full_run_writes_results_and_a_missing_route_is_unscored(world, capsys):
    rc = main(world, "--stack", STACK, "--sub", SUB, "--channel", "both", "--repeat", "1",
              "--only", "pd1,sg1", "--confirm-calls", "100000")
    files = os.listdir(world["out"])
    assert len(files) == 1 and files[0].endswith("-both.json")
    with open(os.path.join(world["out"], files[0]), encoding="utf-8") as f:
        data = json.load(f)
    assert data["run_date_utc"] and data["stack"] == STACK
    assert data["flags"]["ASK_AGENTIC"] == "screen,voice"
    assert "QWEN_API_KEY" not in data["flags"]
    assert {t["item"]["id"] + "/" + t["channel"] for t in data["turns"]} == {
        "pd1/screen", "pd1/voice", "sg1/screen"}
    assert "QUFBQQ" not in json.dumps(data)               # no audio in the results
    # Screen turns record the offer opt-in they were sent with; voice turns do not.
    assert {(t["channel"], t.get("offer_web")) for t in data["turns"]} == {
        ("screen", True), ("voice", None)}
    # The scoped item got the org-api site id.
    starts = [i["event"] for i in world["lam"].invokes
              if i["event"].get("path") == "/api/ask/start"]
    assert {json.loads(e["body"]).get("site_id") for e in starts} == {None, "site-1"}
    assert "GATE" in capsys.readouterr().out
    assert rc in (0, 1)


def test_full_run_drops_a_route_line(world):
    class DropLogs(FakeLogs):
        def filter_log_events(self, **kw):
            out = super().filter_log_events(**kw)
            out["events"] = [e for e in out["events"] if '"channel": "voice"' not in e["message"]]
            return out
    world["logs"] = DropLogs(world["lam"].routes, page=100)
    factory = lambda: {"lambda": world["lam"], "logs": world["logs"], "s3": world["s3"]}  # noqa: E731
    rc = run.main(["--stack", STACK, "--sub", SUB, "--channel", "voice", "--repeat", "1",
                   "--only", "pd1", "--confirm-calls", "100000"], make_clients=factory,
                  clock=world["clock"], sleep=world["clock"].sleep, results_dir=world["out"])
    (name,) = os.listdir(world["out"])
    with open(os.path.join(world["out"], name), encoding="utf-8") as f:
        data = json.load(f)
    (turn,) = data["turns"]
    assert turn["route"] is None and turn["verdict"]["verdict"] == "unscored"
    assert data["gate"]["pass"] is False and rc == 1


def test_relative_items_record_the_run_date(world):
    main(world, "--stack", STACK, "--sub", SUB, "--channel", "screen", "--repeat", "1",
         "--only", "pe1", "--confirm-calls", "100000")
    (name,) = os.listdir(world["out"])
    with open(os.path.join(world["out"], name), encoding="utf-8") as f:
        data = json.load(f)
    assert data["run_date_nz"] and data["relative_items"] == ["pe1"]
