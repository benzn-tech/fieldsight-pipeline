"""Tests for the final-pass continuity linker behind CONTINUITY_LINKER.

Spec: docs/superpowers/specs/2026-11-08-continuity-linker-design.md (section 3 L1-L8, section 4).

On the FINAL pass with the flag on, the extraction call gets NO prior-items block; a second,
thinking-off call ("continuity_link_final") states which new item continues which, and its
answer is attached onto the children before the unchanged resolve / filter_suggestions run.
Live passes are untouched.

Fixtures follow tests/unit/test_extract_session_continuity.py (FakeS3 + monkeypatched flags).
"""
import json
import uuid

import pytest

import item_continuity as ic
from tests.unit.test_lambda_extract_session import (
    BUCKET,
    FakeS3,
    OUT_KEY,
    SEG1_KEY,
    SESSION_BASE,
    les,
    llm_utils,
    make_transcribe_json,
)

OLD_AT = "2020-01-01T00:00:00Z"
SEG1_NAME = SEG1_KEY.rsplit("/", 1)[-1]
SPOKEN = "the crane booking is done for Tuesday morning now"


class _Calls:
    """call_llm double: records every call and answers from a list, in order."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, prompt, max_tokens=4096, force_json=False, enable_thinking=None,
                 model=None, deadline=None, caller="unknown", info=None):
        self.calls.append({"prompt": prompt, "enable_thinking": enable_thinking,
                           "caller": caller, "deadline": deadline, "max_tokens": max_tokens})
        answer = self.answers[len(self.calls) - 1]
        if callable(answer):
            answer = answer()
        if isinstance(answer, tuple):
            return answer
        if info is not None:
            info["model"] = "fake-model"
        return (answer if isinstance(answer, str) else json.dumps(answer)), None


def _prior(prior_ids):
    return {
        "extracted_at": OLD_AT,
        "topics": [{"topic_title": "t", "action_items": [
            {"action": "Book the crane for Tuesday", "item_id": prior_ids[0],
             "responsible": None, "deadline": None, "priority": None},
            {"action": "Order the scaffold boards", "item_id": prior_ids[1],
             "responsible": None, "deadline": None, "priority": None},
        ]}],
    }


def _extraction_answer():
    """Child 1 is a reworded crane item (needs the linker); child 2 is exact text."""
    return {"topics": [{"topic_title": "t", "action_items": [
        {"action": "Crane booked for Tuesday morning", "responsible": None, "deadline": None,
         "priority": None},
        {"action": "Order the scaffold boards", "responsible": None, "deadline": None,
         "priority": None},
    ]}], "declared_site": None}


LINK_ANSWER = {"links": [
    {"ref": "N1", "continues": {"id": "A1", "starts": "Book the crane for Tuesday"}},
]}


def _setup(monkeypatch, answers, linker=True, speaker=None):
    prior_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    fake_s3 = FakeS3({
        SEG1_KEY: json.dumps(make_transcribe_json(SPOKEN, speaker=speaker)),
        OUT_KEY: json.dumps(_prior(prior_ids)),
    })
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(les, "DECLARE_CONTINUITY", True)
    monkeypatch.setattr(les, "STATUS_SUGGESTIONS", True)
    monkeypatch.setattr(les, "CONTINUITY_LINKER", linker)
    fake = _Calls(answers)
    monkeypatch.setattr(llm_utils, "call_llm", fake)
    return fake, prior_ids, fake_s3


def _children(extraction):
    return extraction["topics"][0]["action_items"]


def test_final_with_linker_on_sends_no_block_to_extraction_and_calls_linker_unthinking(
        monkeypatch):
    fake, prior_ids, _ = _setup(monkeypatch, [_extraction_answer(), LINK_ANSWER])

    out = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE, final=True)

    assert len(fake.calls) == 2
    assert "<<<PRIOR_ITEMS>>>" not in fake.calls[0]["prompt"]
    assert fake.calls[0]["caller"] == "extract_session_final"

    # The extraction prompt is exactly the flag-off prompt for the same turns.
    off, _, _ = _setup(monkeypatch, [_extraction_answer()], linker=False)
    monkeypatch.setattr(les, "DECLARE_CONTINUITY", False)
    les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE, final=True)
    assert fake.calls[0]["prompt"] == off.calls[0]["prompt"]

    link = fake.calls[1]
    assert link["caller"] == "continuity_link_final"
    assert link["enable_thinking"] is False
    assert link["max_tokens"] == les.LINKER_MAX_TOKENS == 2000
    assert "<<<NEW_ITEMS>>>" in link["prompt"]
    # STATUS_SUGGESTIONS on -> the linker gets the status rule.
    assert "For an action item you link to an earlier item" in link["prompt"]

    cont = out["continuity"]
    assert cont["linker"]["status"] == "ok"
    assert cont["linker"]["dropped"] == 0
    assert cont["linker"]["model"] == "fake-model"
    assert isinstance(cont["linker"]["ms"], int)
    assert set(cont["linker"]) == {"status", "ms", "dropped", "model"}
    assert cont["question_set"] == ic.LINKER_QUESTION_SET
    assert "error" not in cont

    reworded, exact = _children(out)
    assert reworded["item_id"] == prior_ids[0]
    assert exact["item_id"] == prior_ids[1]
    assert "continues" not in reworded
    accepted = [c for c in cont["claims"] if c["outcome"] == "accepted"]
    assert [c["prior_item_id"] for c in accepted] == [prior_ids[0]]


def test_extraction_models_own_continues_is_discarded_when_the_linker_runs(monkeypatch):
    """The extraction prompt carried no block, so a `continues` there is noise: only the
    linker's answer may claim an earlier item."""
    answer = _extraction_answer()
    answer["topics"][0]["action_items"][0]["continues"] = {
        "id": "A1", "starts": "Book the crane for Tuesday"}
    answer["topics"][0]["action_items"][0]["status_suggestion"] = {
        "status": "done", "at": "10:00:00", "quote": SPOKEN}
    fake, prior_ids, _ = _setup(monkeypatch, [answer, {"links": []}])

    out = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE, final=True)

    reworded = _children(out)[0]
    assert reworded["item_id"] != prior_ids[0]
    assert "status_suggestion" not in reworded
    assert not [c for c in out["continuity"]["claims"] if c["outcome"] == "accepted"]


def test_linker_transcript_is_the_text_the_extraction_saw(monkeypatch):
    fake, _, _ = _setup(monkeypatch, [_extraction_answer(), LINK_ANSWER], speaker="spk_0")

    les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE, final=True,
                        speaker_names={(SEG1_NAME, "spk_0"): "Ben"})

    link_prompt = fake.calls[1]["prompt"]
    transcript = link_prompt.split('## Session Transcript\n"""\n', 1)[1].rsplit('\n"""', 1)[0]
    assert "Ben: " + SPOKEN in transcript
    assert transcript in fake.calls[0]["prompt"]


def test_live_pass_ignores_linker_flag(monkeypatch):
    fake, _, _ = _setup(monkeypatch, [_extraction_answer()])

    out = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE, min_interval_s=0)

    assert len(fake.calls) == 1
    prompt = fake.calls[0]["prompt"]
    assert "<<<PRIOR_ITEMS>>>" in prompt
    assert prompt.index("<<<PRIOR_ITEMS>>>") < prompt.index("## Session Transcript")
    assert "linker" not in out["continuity"]
    assert out["continuity"]["question_set"] == ic.QUESTION_SET


@pytest.mark.parametrize("second", [(None, "boom"), "not json",
                                    lambda: (_ for _ in ()).throw(RuntimeError("x"))])
def test_linker_failure_writes_extraction_with_exact_matches_and_no_continuity_error(
        monkeypatch, second):
    fake, prior_ids, fake_s3 = _setup(monkeypatch, [_extraction_answer(), second])

    out = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE, final=True)

    assert len(fake.calls) == 2
    written = json.loads(fake_s3.objects[OUT_KEY])
    cont = written["continuity"]
    assert cont["linker"]["status"] == "error"
    assert cont["linker"]["model"] is None
    assert "error" not in cont
    reworded, exact = _children(written)
    assert exact["item_id"] == prior_ids[1]
    assert reworded["item_id"] not in prior_ids
    assert out["continuity"]["linker"]["status"] == "error"


def test_linker_skipped_when_budget_short(monkeypatch):
    fake, prior_ids, _ = _setup(monkeypatch, [_extraction_answer()])

    out = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE, final=True,
                              remaining_ms=60_000)

    assert len(fake.calls) == 1
    assert out["continuity"]["linker"] == {"status": "skipped_budget", "ms": 0,
                                           "dropped": 0, "model": None}
    assert _children(out)[1]["item_id"] == prior_ids[1]


def test_linker_deadline_is_capped(monkeypatch):
    fake, _, _ = _setup(monkeypatch, [_extraction_answer(), LINK_ANSWER])

    les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE, final=True,
                        remaining_ms=900_000)

    assert fake.calls[1]["deadline"] == les.LINKER_CAP_S == 120


def test_linker_deadline_shrinks_with_the_budget(monkeypatch):
    fake, _, _ = _setup(monkeypatch, [_extraction_answer(), LINK_ANSWER])

    les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE, final=True,
                        remaining_ms=150_000)

    assert 30 <= fake.calls[1]["deadline"] <= 90


def test_flag_off_artifact_and_prompts_unchanged(monkeypatch):
    fake, _, _ = _setup(monkeypatch, [_extraction_answer()], linker=False)

    out = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE, final=True)

    assert len(fake.calls) == 1
    assert "<<<PRIOR_ITEMS>>>" in fake.calls[0]["prompt"]
    assert "linker" not in out["continuity"]
    assert out["continuity"]["question_set"] == ic.QUESTION_SET


def test_no_prior_means_no_linker_call(monkeypatch):
    fake = _Calls([_extraction_answer()])
    fake_s3 = FakeS3({SEG1_KEY: json.dumps(make_transcribe_json(SPOKEN))})
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(les, "DECLARE_CONTINUITY", True)
    monkeypatch.setattr(les, "CONTINUITY_LINKER", True)
    monkeypatch.setattr(llm_utils, "call_llm", fake)

    out = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE, final=True)

    assert len(fake.calls) == 1
    assert "linker" not in out["continuity"]


@pytest.mark.parametrize("broken", ["assign_refs", "render_linker_prompt", "attach_links"])
def test_run_linker_never_raises(monkeypatch, broken):
    def _boom(*a, **k):
        raise ValueError("defect")
    monkeypatch.setattr(ic, broken, _boom)
    monkeypatch.setattr(llm_utils, "call_llm", _Calls([LINK_ANSWER]))
    rec = les._run_linker([], [{"action_items": [{"action": "x"}]}], "text",
                          budget_deadline=None)
    assert rec["status"] == "error"
    assert rec["model"] is None and rec["dropped"] == 0
    assert set(rec) == {"status", "ms", "dropped", "model"}


class _Ctx:
    def get_remaining_time_in_millis(self):
        return 123_456


def test_handler_passes_the_remaining_budget_on_the_final_branch(monkeypatch):
    seen = {}

    def _fake(bucket, user_folder, date, session_base, **kw):
        seen.update(kw)
        return None

    monkeypatch.setattr(les, "extract_session", _fake)
    monkeypatch.setattr(les, "parse_final_request",
                        lambda bucket, key: ("Benl1", "2026-07-06", SESSION_BASE, 0, None))
    les.lambda_handler({"Records": [{"s3": {"object": {
        "key": "extraction_requests/" + SESSION_BASE + ".json"}}}]}, _Ctx())
    assert seen["remaining_ms"] == 123_456
    assert seen["final"] is True

    seen.clear()
    les.lambda_handler({"Records": [{"s3": {"object": {
        "key": "extraction_requests/" + SESSION_BASE + ".json"}}}]}, None)
    assert seen["remaining_ms"] is None


def test_run_linker_failure_logs_a_warning_with_the_type_only(monkeypatch, caplog):
    """Spec L7: WARNING, counts only -- no traceback (it can carry item text) and no message."""
    def _boom(*a, **k):
        raise ValueError("secret item text")
    monkeypatch.setattr(ic, "attach_links", _boom)
    monkeypatch.setattr(llm_utils, "call_llm", _Calls([LINK_ANSWER]))
    with caplog.at_level("DEBUG"):
        rec = les._run_linker([], [{"action_items": [{"action": "x"}]}], "text",
                              budget_deadline=None)
    assert rec["status"] == "error"
    mine = [r for r in caplog.records if "continuity linker" in r.getMessage()]
    assert mine and all(r.levelname == "WARNING" for r in mine)
    assert all(r.exc_info is None for r in mine)
    assert "ValueError" in mine[0].getMessage()
    assert "secret item text" not in caplog.text


def test_run_linker_unparseable_answer_logs_no_item_text(monkeypatch, caplog):
    """Final review: the linker answer is parsed quietly -- extract_json's failure line would
    otherwise log the raw answer (item text) at ERROR."""
    monkeypatch.setattr(llm_utils, "call_llm", _Calls(["not json <<SECRET ITEM TEXT>>"]))
    with caplog.at_level("DEBUG"):
        rec = les._run_linker([], [{"action_items": [{"action": "x"}]}], "text",
                              budget_deadline=None)
    assert rec["status"] == "error"
    assert "SECRET ITEM TEXT" not in caplog.text
    mine = [r for r in caplog.records if "continuity linker" in r.getMessage()]
    assert mine and "answer unusable" in mine[0].getMessage()
