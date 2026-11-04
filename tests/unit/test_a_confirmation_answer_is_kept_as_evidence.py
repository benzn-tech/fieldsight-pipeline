"""Task 11: the inspector's answer to a final-check question, from the device.

The answer is parsed (pass / fail with a reason / N/A / "no, that was Unit 2" /
anything else = skip), written as report evidence by the in-VPC hop, and the
walk report is re-queued through item-writer (R31).

Spec: docs/superpowers/specs/2026-11-03-multi-unit-inspection-design.md
Plan: docs/superpowers/plans/2026-11-03-multi-unit-inspection.md, Task 11
"""
import base64
import contextlib
import io
import json
import logging
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone

import pytest

import lambda_check_gaps as h
import lambda_check_gaps_db as hop
import lambda_item_writer as iw
import walk_confirm as wc

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src")
UNITS = ["Unit 1", "Unit 2", "Unit 3"]
KEY = "walk:" + "a" * 40
# 2026-11-03 21:14:05 UTC is 10:14:05 NZDT on the 4th.
NOW = datetime(2026, 11, 3, 21, 14, 5, tzinfo=timezone.utc)
SAY_SAVED = "Got it."
SAY_SKIP = "Skipping this one — it's on your phone."


# ---- parse_answer: one case per rule, in the brief's order -------------------

def test_rule_1_a_no_that_names_a_unit_of_the_walk_is_a_move():
    assert wc.parse_answer("No, that was Unit 2.", "confirm_no", UNITS) == {"move_to": "Unit 2"}
    assert wc.parse_answer("wrong one, unit three", "confirm_no", UNITS) == {"move_to": "Unit 3"}
    assert wc.parse_answer("That's unit 1", "confirm_no", UNITS) == {"move_to": "Unit 1"}


def test_rule_2_a_pass_is_yes():
    for said in ("Pass.", "yes", "Good", "OK", "right", "Correct!"):
        assert wc.parse_answer(said, "not_checked", UNITS) == {"answer": "Yes"}, said


def test_rule_2_a_read_back_no_confirmed_stays_no():
    # R32: on a read-back No every acknowledgement confirms the No.
    for said in ("Right.", "correct", "Yes", "yeah", "OK", "okay", "Good", "That's right",
                 "yep"):
        assert wc.parse_answer(said, "confirm_no", UNITS) == {"answer": "No", "confirmed": True}, said


def test_rule_2_only_an_explicit_pass_turns_a_read_back_no_into_yes():
    for said in ("Pass", "passed", "It passes.", "actually it passes"):
        assert wc.parse_answer(said, "confirm_no", UNITS) == {"answer": "Yes"}, said


def test_rule_3_a_fail_is_no_with_the_rest_as_its_comment():
    assert wc.parse_answer("Fail, the nogging is missing.", "not_checked", UNITS) == {
        "answer": "No", "comment": "the nogging is missing."}
    assert wc.parse_answer("failed", "not_checked", UNITS) == {"answer": "No", "comment": ""}
    assert wc.parse_answer("No - two straps short", "not_checked", UNITS) == {
        "answer": "No", "comment": "two straps short"}


def test_rule_4_not_applicable_is_na():
    for said in ("Not applicable.", "N/A", "n.a.", "na", "it doesn't apply here"):
        assert wc.parse_answer(said, "not_checked", UNITS) == {"answer": "N/A"}, said


def test_an_acknowledgement_with_a_pass_on_a_read_back_no_is_a_skip():
    # R34: ambiguous -- never a confirmed No, never a Yes; it goes to the phone.
    for said in ("yes it passes", "ok, passed", "Right, it passes", "that's right, pass"):
        assert wc.parse_answer(said, "confirm_no", UNITS) is None, said
    assert wc.parse_answer("actually it passes", "confirm_no", UNITS) == {"answer": "Yes"}


def test_rule_4_is_anchored_to_the_start():
    assert wc.parse_answer("pass, n a for the lintel", "not_checked", UNITS) == {"answer": "Yes"}
    assert wc.parse_answer("the lintel is na", "not_checked", UNITS) is None


def test_rule_5_anything_else_is_a_skip():
    assert wc.parse_answer("what's the weather", "not_checked", UNITS) is None
    assert wc.parse_answer("", "not_checked", UNITS) is None
    assert wc.parse_answer("   ", "confirm_no", UNITS) is None


def test_a_unit_mentioned_in_passing_is_not_a_move():
    assert wc.parse_answer("No, the nogging in Unit 2 is missing", "confirm_no", UNITS) == {
        "answer": "No", "comment": "the nogging in Unit 2 is missing"}
    assert wc.parse_answer("That's right, same as unit 2", "confirm_no", UNITS) == {
        "answer": "No", "confirmed": True}
    assert wc.parse_answer("yes, no problems, unit 2 too", "confirm_no", UNITS) == {
        "answer": "No", "confirmed": True}


def test_a_whole_answer_naming_a_unit_is_a_move():
    walk = UNITS + ["Lot 151"]
    assert wc.parse_answer("no, that was unit two", "confirm_no", walk) == {"move_to": "Unit 2"}
    assert wc.parse_answer("wrong, it's Lot 151", "confirm_no", walk) == {"move_to": "Lot 151"}
    assert wc.parse_answer("nope, should be unit 1 actually", "confirm_no", walk) == {
        "move_to": "Unit 1"}
    assert wc.parse_answer("Sorry, it belongs to U3.", "confirm_no", walk) == {"move_to": "Unit 3"}
    assert wc.parse_answer("no, it is unit 2", "confirm_no", walk) == {"move_to": "Unit 2"}


def test_a_move_names_only_a_unit_of_the_walk():
    # A unit outside the walk is not a move: the rules carry on (rule 3 here).
    assert wc.parse_answer("No, that was Unit 9", "confirm_no", UNITS) == {
        "answer": "No", "comment": "that was Unit 9"}
    # A move is only for a read-back No.
    assert wc.parse_answer("No, that was Unit 2", "not_checked", UNITS) == {
        "answer": "No", "comment": "that was Unit 2"}
    # No walk units at all: never a move.
    assert "move_to" not in (wc.parse_answer("no that was unit 2", "confirm_no", []) or {})


# ---- handle -------------------------------------------------------------------

class Hop:
    """The db hop: records each write payload, answers `replies` in turn."""

    def __init__(self, *replies):
        self.calls, self.replies = [], list(replies)

    def __call__(self, payload):
        self.calls.append(payload)
        if self.replies:
            r = self.replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return {"status": "saved", "company_id": "c1", "folder": "Ben", "date": "2026-11-04",
                "walk_key": payload["walk_key"]}


def _event(**kw):
    e = {"action": "confirm", "caller_sub": "sub-1", "walk_key": KEY, "unit": "Unit 3",
         "item_text": "Wall bracing", "kind": "not_checked", "text": "pass", "pressed_at": 1}
    e.update(kw)
    return e


def _run(event, hop_fn=None, stt=None, requeue=None):
    hop_fn = hop_fn or Hop()
    queued = []
    out = wc.handle(event, stt=stt, db_write=hop_fn, now=lambda: NOW,
                    requeue=requeue or (lambda saved: queued.append(saved)))
    return out, hop_fn, queued


def test_a_pass_is_written_through_the_hop_and_the_walk_report_requeued():
    out, hop_fn, queued = _run(_event())
    assert out == {"status": "saved", "say": SAY_SAVED, "next": None}
    assert hop_fn.calls == [{"action": "write_walk_answer", "caller_sub": "sub-1", "walk_key": KEY,
                             "unit": "Unit 3", "item_text": "Wall bracing", "answer": "Yes",
                             "comment": "", "move_to_unit": None, "quote": "pass",
                             "answered_at": "10:14:05", "asked_kind": "not_checked"}]
    assert queued == [{"status": "saved", "company_id": "c1", "folder": "Ben",
                       "date": "2026-11-04", "walk_key": KEY}]


def test_the_question_it_answers_goes_with_the_answer():
    """R36: an answer to a read-back No is the inspector's choice over the
    recording, so the hop is told which kind of question it answers."""
    out, hop_fn, _ = _run(_event(kind="confirm_no", text="pass"))
    assert out["status"] == "saved"
    assert (hop_fn.calls[0]["answer"], hop_fn.calls[0]["asked_kind"]) == ("Yes", "confirm_no")


def test_the_transcript_is_stored_as_the_quote():
    heard = []
    def stt(data, filename):
        heard.append(data)
        return " Failed, the nogging is missing. "
    audio = base64.b64encode(b"RIFF....").decode()
    out, hop_fn, _ = _run(_event(text=None, audio_b64=audio), stt=stt)
    assert heard == [b"RIFF...."]
    row = hop_fn.calls[0]
    assert row["quote"] == "Failed, the nogging is missing."
    assert (row["answer"], row["comment"]) == ("No", "the nogging is missing.")
    assert out["status"] == "saved"


def test_a_skip_writes_nothing():
    out, hop_fn, queued = _run(_event(text="what's the weather"))
    assert out == {"status": "skipped", "say": SAY_SKIP, "next": None}
    assert hop_fn.calls == [] and queued == []


def test_blank_text_beside_audio_uses_the_audio():
    audio = base64.b64encode(b"wav").decode()
    for blank in ("", "   "):
        out, hop_fn, _ = _run(_event(text=blank, audio_b64=audio), stt=lambda d, f: "pass")
        assert out["status"] == "saved" and hop_fn.calls[0]["quote"] == "pass"


def test_nothing_heard_is_a_skip():
    out, hop_fn, _ = _run(_event(text=None, audio_b64=base64.b64encode(b"x").decode()),
                          stt=lambda d, f: "")
    assert out["status"] == "skipped" and hop_fn.calls == []


def test_a_move_writes_the_target_and_says_so():
    out, hop_fn, queued = _run(_event(kind="confirm_no", text="No, that was Unit 2"))
    assert out == {"status": "saved", "say": "Moved to Unit 2.", "next": None}
    row = hop_fn.calls[0]
    assert (row["unit"], row["answer"], row["move_to_unit"]) == ("Unit 3", None, "Unit 2")
    assert len(queued) == 1


def test_a_move_the_hop_refuses_is_answered_by_the_next_rule():
    # The hop is the authority on the walk's units: "Unit 2" is not one of them.
    hop_fn = Hop({"error": wc.MOVE_NOT_IN_WALK})
    out, hop_fn, _ = _run(_event(kind="confirm_no", text="No, that was Unit 2"), hop_fn=hop_fn)
    assert [c["move_to_unit"] for c in hop_fn.calls] == ["Unit 2", None]
    assert (hop_fn.calls[1]["answer"], hop_fn.calls[1]["comment"]) == ("No", "that was Unit 2")
    assert out["say"] == SAY_SAVED


def test_a_move_to_the_same_unit_is_not_a_move():
    out, hop_fn, _ = _run(_event(kind="confirm_no", unit="Unit 2", text="no, that was unit 2"))
    assert hop_fn.calls[0]["move_to_unit"] is None and hop_fn.calls[0]["answer"] == "No"


def test_a_confirmed_no_is_written_as_no():
    out, hop_fn, _ = _run(_event(kind="confirm_no", text="correct"))
    assert hop_fn.calls[0]["answer"] == "No" and out["say"] == SAY_SAVED


def test_a_requeue_failure_never_fails_the_answer(caplog):
    def boom(saved):
        raise RuntimeError("invoke failed")
    with caplog.at_level(logging.INFO):
        out, _, _ = _run(_event(), requeue=boom)
    assert out == {"status": "saved", "say": SAY_SAVED, "next": None}
    assert "requeue" in caplog.text


def test_a_hop_error_saves_nothing_and_queues_nothing():
    for reply in ({"error": "walk not found"}, RuntimeError("hop down")):
        out, hop_fn, queued = _run(_event(), hop_fn=Hop(reply))
        assert out["status"] == "error" and out["say"] == SAY_SKIP and out["next"] is None
        assert queued == []


def test_an_stt_failure_or_no_stt_is_an_error_not_a_raise():
    audio = base64.b64encode(b"x").decode()
    def boom(d, f):
        raise RuntimeError("STT down")
    for stt in (boom, None):
        out, hop_fn, _ = _run(_event(text=None, audio_b64=audio), stt=stt)
        assert out["status"] == "error" and hop_fn.calls == []


def test_the_event_cannot_name_a_company_folder_or_session():
    out, hop_fn, _ = _run(_event(company_id="other", folder="Someone", session="sidX",
                                 user_folder="Someone"))
    assert set(hop_fn.calls[0]) == {"action", "caller_sub", "walk_key", "unit", "item_text",
                                    "answer", "comment", "move_to_unit", "quote", "answered_at",
                                    "asked_kind"}


def test_an_unknown_kind_or_missing_fields_write_nothing():
    for bad in (_event(kind="other"), _event(walk_key=None), _event(item_text=""),
                _event(caller_sub=None)):
        out, hop_fn, _ = _run(bad)
        assert out["status"] == "error" and hop_fn.calls == []


def test_the_transcript_is_never_logged(caplog):
    with caplog.at_level(logging.DEBUG):
        _run(_event(text="Fail, secret words here"))
        _run(_event(text="what's the weather like"))
    assert "secret" not in caplog.text and "weather" not in caplog.text


# ---- the check-gaps handler routes it, and never to general Ask ---------------

def test_the_handler_routes_confirm_to_walk_confirm(monkeypatch):
    seen = {}
    monkeypatch.setattr(h.check_done, "run", lambda *a, **k: pytest.fail("check_done ran"))
    monkeypatch.setattr(h.walk_confirm, "handle",
                        lambda event, **kw: seen.update(kw, event=event) or {"status": "saved"})
    monkeypatch.setattr(h.elevenlabs_utils, "ELEVENLABS_ASK_API_KEY", "k")
    assert h.lambda_handler(_event(), None) == {"status": "saved"}
    assert seen["db_write"] is h.db_hop and seen["stt"] is h.stt_chunk
    assert seen["requeue"] is h.requeue_walk
    assert seen["now"]().tzinfo is not None


class FakeLambda:
    def __init__(self):
        self.calls = []

    def invoke(self, **kw):
        self.calls.append(kw)
        return {"StatusCode": 202}


def test_the_requeue_is_an_async_invoke_of_item_writer(monkeypatch):
    fake = FakeLambda()
    monkeypatch.setattr(h, "_get_lambda", lambda: fake)
    monkeypatch.setattr(h, "ITEM_WRITER_FUNCTION", "iw-fn")
    h.requeue_walk({"status": "saved", "company_id": "c1", "folder": "Ben",
                    "date": "2026-11-04", "walk_key": KEY, "id": "x"})
    call = fake.calls[0]
    assert call["FunctionName"] == "iw-fn" and call["InvocationType"] == "Event"
    assert json.loads(call["Payload"]) == {"action": "requeue_walk", "company_id": "c1",
                                           "folder": "Ben", "date": "2026-11-04",
                                           "walk_key": KEY}


def test_no_item_writer_configured_raises_so_handle_logs_it(monkeypatch):
    monkeypatch.setattr(h, "ITEM_WRITER_FUNCTION", "")
    with pytest.raises(RuntimeError):
        h.requeue_walk({"company_id": "c1", "folder": "Ben", "date": "2026-11-04",
                        "walk_key": KEY})


def test_a_confirmation_never_imports_general_ask():
    env = dict(os.environ, PYTHONPATH=SRC, AWS_DEFAULT_REGION="ap-southeast-2",
               AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="x")
    code = (
        "import sys, datetime, lambda_check_gaps as h\n"
        "out = h.walk_confirm.handle({'action': 'confirm', 'caller_sub': 's', 'walk_key': %r,"
        " 'unit': 'Unit 1', 'item_text': 'x', 'kind': 'not_checked', 'text': 'pass'},"
        " stt=None, db_write=lambda p: {'status': 'saved'}, requeue=lambda s: None,"
        " now=lambda: datetime.datetime.now(datetime.timezone.utc))\n"
        "assert out['status'] == 'saved', out\n"
        "print(sorted(m for m in sys.modules if m.startswith(('ask_', 'lambda_ask'))))\n" % KEY)
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=SRC)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


# ---- the hop: verify, then write exactly one walk_answers row -----------------

USER = {"id": uuid.UUID(int=7), "company_id": uuid.UUID(int=1), "folder_name": "Ben",
        "cognito_sub": "sub-1"}
SITE_CO = "00000000-0000-0000-0000-000000000005"
TPL = "00000000-0000-0000-0000-000000000009"
BODY = {"sections": [{"key": "c", "title": "Framing", "kind": "checklist",
                      "items": ["Wall bracing", "Nogging"]},
                     {"key": "d", "title": "Roof", "kind": "checklist", "items": ["Trusses"]}]}


def _rows(key=None):
    import units
    k = key or units.walk_key(SITE_CO, "Ben", "2026-11-04", TPL, "10:00:00")
    return k, [
        {"company_id": SITE_CO, "report_date": "2026-11-04", "template_id": TPL,
         "start_at": "10:00:00", "units": {"names": ["Unit 1", "Unit 3"], "count": 2},
         "entered": [{"unit": "unit 1", "at": "10:01"}]},
        {"company_id": SITE_CO, "report_date": "2026-11-04", "template_id": TPL,
         "start_at": "10:20:00", "units": None, "entered": [{"unit": "lot two", "at": "10:21"},
                                                            {"unit": "Kitchen", "at": "10:22"}]},
    ]


def _wire_hop(monkeypatch, user=USER, rows=None, body=BODY):
    seen = {"added": [], "conns": 0}
    key, default = _rows()

    @contextlib.contextmanager
    def conn():
        seen["conns"] += 1
        yield "conn"

    monkeypatch.setattr(hop, "get_connection", conn)
    monkeypatch.setattr(hop, "close_cached_connection", lambda: None)
    monkeypatch.setattr(hop, "get_cached_connection", lambda: pytest.fail("write used the read connection"))
    monkeypatch.setattr(hop.users, "get_user_by_sub", lambda c, sub: user if sub == "sub-1" else None)
    monkeypatch.setattr(hop.tenancy_status, "caller_blocked", lambda c, u, where="": None)
    monkeypatch.setattr(hop.inspection_windows, "walk_rows",
                        lambda c, folder, k: (rows if rows is not None else default)
                        if (folder, k) == ("Ben", key) else [])
    monkeypatch.setattr(hop.report_templates, "get_version",
                        lambda c, t, v: {"body": body} if str(t) == TPL else None)
    monkeypatch.setattr(hop.walk_answers, "add",
                        lambda c, row: seen["added"].append(row) or "new-id")
    return key, seen


def _write(key, **kw):
    e = {"action": "write_walk_answer", "caller_sub": "sub-1", "walk_key": key, "unit": "Unit 3",
         "item_text": "Wall bracing", "answer": "Yes", "comment": "", "move_to_unit": None,
         "quote": "pass", "answered_at": "10:14:05"}
    e.update(kw)
    return e


def test_the_hop_writes_the_answer_in_the_walks_company_by_the_caller(monkeypatch):
    key, seen = _wire_hop(monkeypatch)
    out = hop.lambda_handler(_write(key, item_text="trusses"), None)
    assert out == {"status": "saved", "id": "new-id", "company_id": SITE_CO, "folder": "Ben",
                   "date": "2026-11-04", "walk_key": key}
    assert seen["added"] == [{"company_id": SITE_CO, "user_folder": "Ben",
                              "report_date": "2026-11-04", "walk_key": key, "unit": "Unit 3",
                              "item_text": "Trusses", "answer": "Yes", "comment": "",
                              "move_to_unit": None, "source": "device", "quote": "pass",
                              "voice_note_id": None, "answered_at": "10:14:05",
                              "answered_by": str(USER["id"]), "asked_kind": "not_checked"}]


def test_the_hop_stores_the_kind_of_question_answered(monkeypatch):
    key, seen = _wire_hop(monkeypatch)
    assert hop.lambda_handler(_write(key, asked_kind="confirm_no", answer="Yes"),
                              None)["status"] == "saved"
    assert seen["added"][0]["asked_kind"] == "confirm_no"


def test_the_hop_knows_the_entered_units_and_a_move_to_one(monkeypatch):
    key, seen = _wire_hop(monkeypatch)
    out = hop.lambda_handler(_write(key, unit="unit 1", answer=None, move_to_unit="Lot 2"), None)
    assert out["status"] == "saved"
    assert (seen["added"][0]["unit"], seen["added"][0]["move_to_unit"]) == ("Unit 1", "Lot 2")


@pytest.mark.parametrize("change, error", [
    ({"walk_key": "walk:" + "b" * 40}, "walk not found"),
    ({"walk_key": "not-a-key"}, "walk_key"),
    ({"caller_sub": "someone-else"}, "unknown caller"),
    ({"caller_sub": None}, "caller_sub required"),
    ({"unit": "Unit 9"}, "unit not in walk"),
    ({"unit": "Kitchen"}, "unit not in walk"),
    ({"answer": None, "move_to_unit": "Unit 9"}, "move_to_unit not in walk"),
    ({"item_text": "Plumbing"}, "item not in checklist"),
    ({"answer": "Maybe"}, "answer"),
    ({"answer": None, "move_to_unit": None}, "answer"),
    ({"answered_at": "10:14"}, "answered_at"),
    ({"asked_kind": "general"}, "asked_kind invalid"),
    ({"answer": "No", "move_to_unit": "Unit 1"}, "answer with a move_to_unit"),
])
def test_the_hop_refuses_and_writes_nothing(monkeypatch, change, error):
    key, seen = _wire_hop(monkeypatch)
    out = hop.lambda_handler(_write(key, **change), None)
    assert out.get("error", "").startswith(error), out
    assert seen["added"] == []


def test_the_move_error_is_the_one_handle_recognises():
    assert hop.MOVE_NOT_IN_WALK == wc.MOVE_NOT_IN_WALK


def test_a_walk_under_another_folder_is_not_found(monkeypatch):
    other = dict(USER, folder_name="Someone")
    key, seen = _wire_hop(monkeypatch, user=other)
    assert hop.lambda_handler(_write(key), None) == {"error": "walk not found"}
    assert seen["added"] == []


def test_a_blocked_caller_writes_nothing(monkeypatch):
    key, seen = _wire_hop(monkeypatch)
    monkeypatch.setattr(hop.tenancy_status, "caller_blocked", lambda c, u, where="": "company_inactive")
    assert hop.lambda_handler(_write(key), None) == {"error": "company_inactive"}
    assert seen["added"] == []


def test_the_hop_never_takes_a_company_or_folder_from_the_event(monkeypatch):
    key, seen = _wire_hop(monkeypatch)
    hop.lambda_handler(_write(key, company_id=str(uuid.uuid4()), folder="Someone",
                              user_folder="Someone", answered_by=str(uuid.uuid4()),
                              source="phone"), None)
    row = seen["added"][0]
    assert (row["company_id"], row["user_folder"], row["answered_by"], row["source"]) == (
        SITE_CO, "Ben", str(USER["id"]), "device")


def test_the_opening_of_a_walk_under_two_companies_names_its_company(monkeypatch):
    key, rows = _rows()
    stray = dict(rows[1], company_id="00000000-0000-0000-0000-000000000006")
    key, seen = _wire_hop(monkeypatch, rows=[rows[0], stray])
    assert hop.lambda_handler(_write(key), None)["company_id"] == SITE_CO


def test_the_hop_without_an_action_is_the_read_it_always_was(monkeypatch):
    seen = {}
    monkeypatch.setattr(hop, "_facts", lambda e: seen.setdefault("facts", e) and {"user": None})
    monkeypatch.setattr(hop, "_write_walk_answer", lambda e: pytest.fail("wrote"))
    monkeypatch.setattr(hop, "close_cached_connection", lambda: None)
    assert hop.lambda_handler({"caller_sub": "s", "pressed_at": 1}, None) == {"user": None}
    assert hop.lambda_handler({"action": "drop_tables", "caller_sub": "s"}, None) == {
        "error": "unknown action"}


def test_a_hop_write_failure_is_an_error_never_a_raise(monkeypatch):
    key, seen = _wire_hop(monkeypatch)
    def boom(c, row):
        raise RuntimeError("db down")
    monkeypatch.setattr(hop.walk_answers, "add", boom)
    assert hop.lambda_handler(_write(key), None) == {"error": "db"}


def test_a_hop_write_failure_logs_no_exception_text(monkeypatch, caplog):
    # A psycopg DETAIL can carry the row's values, the quote among them.
    key, seen = _wire_hop(monkeypatch)
    def boom(c, row):
        raise ValueError("DETAIL: Failing row contains (secret quote words)")
    monkeypatch.setattr(hop.walk_answers, "add", boom)
    with caplog.at_level(logging.DEBUG):
        assert hop.lambda_handler(_write(key, quote="secret quote words"), None) == {"error": "db"}
    assert "secret" not in caplog.text
    assert "ValueError" in caplog.text and "write_walk_answer" in caplog.text
    assert all(r.exc_info is None for r in caplog.records)


# ---- item-writer: the requeue_walk action, and every other event as today -----

def _requeue_event(**kw):
    e = {"action": "requeue_walk", "company_id": SITE_CO, "folder": "Ben",
         "date": "2026-11-04", "walk_key": KEY}
    e.update(kw)
    return e


def _wire_iw(monkeypatch):
    seen = {"requeued": [], "written": [], "commits": 0}

    @contextlib.contextmanager
    def conn():
        yield "conn"
        seen["commits"] += 1

    monkeypatch.setattr(iw, "get_connection", conn)
    monkeypatch.setattr(iw.checklist_reports, "requeue_walk",
                        lambda c, *a: seen["requeued"].append((c,) + a) or "rid-1")
    monkeypatch.setattr(iw, "write_extraction_items",
                        lambda date, folder, key: seen["written"].append(key) or {"key": key})
    monkeypatch.setattr(iw.pipeline_trace, "flush", lambda s3, bucket: None)
    return seen


def test_item_writer_requeues_the_walk_it_is_asked_to(monkeypatch):
    seen = _wire_iw(monkeypatch)
    assert iw.lambda_handler(_requeue_event(), None) == {"requeued": "rid-1"}
    assert seen["requeued"] == [("conn", SITE_CO, "Ben", "2026-11-04", KEY)]
    assert seen["commits"] == 1 and seen["written"] == []


@pytest.mark.parametrize("change", [
    {"walk_key": "walk:xyz"}, {"walk_key": None}, {"company_id": "not-a-uuid"},
    {"company_id": None}, {"folder": "../x"}, {"folder": "Ben Lin"}, {"folder": ""},
    {"folder": "a/b"}, {"date": "2026-13-01"}, {"date": "04/11/2026"}, {"date": None},
])
def test_item_writer_refuses_a_bad_requeue(monkeypatch, change):
    seen = _wire_iw(monkeypatch)
    out = iw.lambda_handler(_requeue_event(**change), None)
    assert "error" in out and seen["requeued"] == []


def _s3_event(key, **record_extra):
    return {"Records": [dict({"s3": {"object": {"key": key}, "bucket": {"name": "b"}}},
                             **record_extra)]}


def test_an_s3_event_reaches_the_extraction_write_exactly_as_today(monkeypatch):
    seen = _wire_iw(monkeypatch)
    key = "extractions/Ben/2026-11-04/sidabc.json"
    assert iw.lambda_handler(_s3_event(key), None) == {"results": [{"key": key}]}
    assert seen["written"] == [key] and seen["requeued"] == []


def test_an_action_inside_an_s3_record_is_never_acted_on(monkeypatch):
    seen = _wire_iw(monkeypatch)
    key = "extractions/Ben/2026-11-04/sidabc.json"
    iw.lambda_handler(_s3_event(key, **_requeue_event()), None)       # inside the record
    ev = dict(_s3_event(key), **_requeue_event())          # top-level action beside Records
    iw.lambda_handler(ev, None)
    assert seen["requeued"] == [] and seen["written"] == [key, key]


def test_an_empty_event_is_as_today(monkeypatch):
    seen = _wire_iw(monkeypatch)
    assert iw.lambda_handler({}, None) == {"results": []}
    assert iw.lambda_handler(None, None) == {"results": []}
    assert seen["requeued"] == []


# ---- the hop's second write: the final check's question list (R41) -------------

def _store(key, **kw):
    e = {"action": "store_final_check", "caller_sub": "sub-1", "walk_key": key,
         "questions": [{"kind": "not_checked", "unit": "unit three", "item_text": "wall  BRACING",
                        "comment": ""},
                       {"kind": "confirm_no", "unit": "Unit 1", "item_text": "Trusses",
                        "comment": "two short"}]}
    e.update(kw)
    return e


def _wire_store(monkeypatch):
    key, seen = _wire_hop(monkeypatch)
    seen["stored"] = []
    monkeypatch.setattr(hop.walk_final_checks, "add",
                        lambda c, row: seen["stored"].append(row) or "fc-id")
    return key, seen


def test_the_hop_stores_the_final_checks_questions_under_the_walks_company(monkeypatch, caplog):
    key, seen = _wire_store(monkeypatch)
    with caplog.at_level(logging.INFO):
        out = hop.lambda_handler(_store(key), None)
    assert out == {"status": "saved", "id": "fc-id", "walk_key": key, "questions": 2,
                   "skipped": 0}
    assert seen["stored"] == [{
        "company_id": SITE_CO, "user_folder": "Ben", "report_date": "2026-11-04", "walk_key": key,
        "questions": [{"kind": "not_checked", "unit": "Unit 3", "item_text": "Wall bracing",
                       "comment": ""},
                      {"kind": "confirm_no", "unit": "Unit 1", "item_text": "Trusses",
                       "comment": "two short"}]}]
    assert seen["added"] == []                              # not an answer
    assert seen["conns"] == 1                               # its own transaction
    assert "Wall bracing" not in caplog.text and "two short" not in caplog.text


@pytest.mark.parametrize("change, error", [
    ({"walk_key": "walk:" + "b" * 40}, "walk not found"),
    ({"walk_key": "nope"}, "walk_key invalid"),
    ({"caller_sub": "someone-else"}, "unknown caller"),
    ({"questions": "all of them"}, "questions invalid"),
    ({"questions": None}, "questions invalid"),
    ({"questions": [{"kind": "not_checked", "unit": "Unit 1", "item_text": "Trusses"}] * 401},
     "questions invalid"),
])
def test_a_refused_final_check_stores_nothing(monkeypatch, change, error):
    key, seen = _wire_store(monkeypatch)
    out = hop.lambda_handler(_store(key, **change), None)
    assert out.get("error") == error, out
    assert seen["stored"] == []


def test_a_question_that_no_longer_fits_the_walk_is_skipped_not_the_rest(monkeypatch, caplog):
    """The template edited mid-day, a unit dropped: those questions are left
    out and counted; the others are stored."""
    key, seen = _wire_store(monkeypatch)
    good = {"kind": "not_checked", "unit": "Unit 1", "item_text": "Trusses", "comment": ""}
    bad = [{"kind": "general", "unit": "Unit 1", "item_text": "Trusses"},
           {"kind": "not_checked", "unit": "Unit 9", "item_text": "Trusses"},
           {"kind": "not_checked", "unit": "Unit 1", "item_text": "Plumbing"},
           {"kind": "not_checked", "unit": "Unit 1", "item_text": "Trusses", "comment": 3},
           "not a question"]
    with caplog.at_level(logging.INFO):
        out = hop.lambda_handler(_store(key, questions=bad[:2] + [good] + bad[2:]), None)
    assert out == {"status": "saved", "id": "fc-id", "walk_key": key, "questions": 1,
                   "skipped": 5}
    assert seen["stored"][0]["questions"] == [good]
    assert "skipped=5" in caplog.text and "Plumbing" not in caplog.text


def test_an_empty_final_check_is_stored_and_all_unfit_is_refused(monkeypatch):
    """Nothing open is a list too (it must win over an older one); a list of
    which nothing fits is not "nothing open" and is refused."""
    key, seen = _wire_store(monkeypatch)
    assert hop.lambda_handler(_store(key, questions=[]), None)["questions"] == 0
    assert seen["stored"][0]["questions"] == []
    out = hop.lambda_handler(_store(key, questions=[
        {"kind": "not_checked", "unit": "Unit 9", "item_text": "Trusses"}]), None)
    assert out == {"error": "no question fits the walk", "skipped": 1}
    assert len(seen["stored"]) == 1


def test_a_blocked_caller_stores_no_final_check(monkeypatch):
    key, seen = _wire_store(monkeypatch)
    monkeypatch.setattr(hop.tenancy_status, "caller_blocked", lambda c, u, where="": "company_inactive")
    assert hop.lambda_handler(_store(key), None) == {"error": "company_inactive"}
    assert seen["stored"] == []


def test_the_hop_has_exactly_two_writes():
    src = open(hop.__file__, encoding="utf-8").read()
    assert "exactly two writes" in src
    assert src.count("walk_answers.add(") == 1 and src.count("walk_final_checks.add(") == 1
