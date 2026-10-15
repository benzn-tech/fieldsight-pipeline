"""A checklist report names what was not done (owner, 2026-10-16).

THE test runs the real status and summing code over a real result body
through a fake s3() -- not a stubbed status function, which would leave the
new code a seam nobody executes."""
import io
import json

import pytest

org = pytest.importorskip("lambda_org_api")

KEY = "session_report_results/x/2026-10-16/day/r1.json"


class FakeS3:
    def __init__(self, body=None, missing=False):
        self.body, self.missing = body, missing

    def get_object(self, Bucket, Key):
        if self.missing:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(json.dumps(self.body).encode("utf-8"))}


def status(monkeypatch, body=None, missing=False):
    monkeypatch.setattr(org, "s3", lambda: FakeS3(body, missing))
    return org._checklist_report_status(KEY)


GAPS = {"not_covered": [{"n": 3, "text": "Cover to steel checked"}],
        "failed": [{"n": 2, "text": "Edge protection installed", "comment": "north side",
                    "responsible": "Mike", "due": "Fri 17 Oct 2026"}]}


def test_THE_a_done_report_carries_its_gaps_with_the_checklist_named(monkeypatch):
    body = {"status": "done", "checklists": {
        "Pre-pour": {"items": 5, "gaps": GAPS},
        "Housekeeping": {"items": 2, "gaps": {"not_covered": [{"n": 1, "text": "Bins emptied"}], "failed": []}}}}
    st, err, gaps = status(monkeypatch, body)
    assert (st, err) == ("done", None)
    assert gaps["notCovered"] == [
        {"n": 3, "text": "Cover to steel checked", "checklist": "Pre-pour"},
        {"n": 1, "text": "Bins emptied", "checklist": "Housekeeping"}]
    assert gaps["failed"] == [dict(GAPS["failed"][0], checklist="Pre-pour")]


def test_all_covered_is_two_empty_lists_not_null(monkeypatch):
    body = {"status": "done", "checklists": {"Pre-pour": {"gaps": {"not_covered": [], "failed": []}}}}
    assert status(monkeypatch, body)[2] == {"notCovered": [], "failed": []}


@pytest.mark.parametrize("checklists", [
    None, {}, {"Pre-pour": {"items": 5}}, [], "x", {"Pre-pour": "x"},
    {"Pre-pour": {"gaps": "x"}}, {"Pre-pour": {"gaps": {"not_covered": "x"}}},
])
def test_no_gaps_known_is_null_and_the_status_stands(monkeypatch, checklists):
    body = {"status": "done"}
    if checklists is not None:
        body["checklists"] = checklists
    st, err, gaps = status(monkeypatch, body)
    assert st == "done" and gaps is None


def test_a_report_not_done_has_no_gaps(monkeypatch):
    assert status(monkeypatch, missing=True) == ("pending", None, None)
    body = {"status": "error", "error": "boom", "checklists": {"P": {"gaps": GAPS}}}
    assert status(monkeypatch, body) == ("error", "boom", None)


def test_the_payload_passes_the_gaps_through(monkeypatch):
    import datetime
    import uuid
    monkeypatch.setattr(org, "s3", lambda: FakeS3({"status": "done", "checklists": {"P": {"gaps": GAPS}}}))
    p = org._checklist_report_payload({
        "id": uuid.UUID(int=1), "report_date": datetime.date(2026, 10, 16), "check_name": "pre-pour",
        "template_name": "Pre-pour", "template_id": uuid.UUID(int=2), "start_at": "10:00:00",
        "end_at": "10:05:00", "segments": None, "request_id": "r1", "result_key": KEY})
    assert p["status"] == "done" and p["gaps"]["failed"][0]["checklist"] == "P"
