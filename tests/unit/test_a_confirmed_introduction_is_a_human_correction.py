"""Confirming a self-introduction must delegate to `speaker_corrections`, exactly like
confirming a name proposal (Task 6) -- mirrors
`test_a_confirmed_proposal_is_a_human_correction.py`, including its `FakeConn.outcomes`
transaction probe.

The reason is the same string: `recompute_company_floor` counts only
`source='correction'` rows, and a confirmation written any other way would name the
person correctly and calibrate nothing.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")
vw = pytest.importorskip("lambda_voiceprint_writer", reason="requires psycopg")

CO = "11111111-1111-1111-1111-111111111111"
SUG = "44444444-4444-4444-4444-444444444444"
SESSION = "sid" + "b" * 32
SRC = f"Ben1_2026-09-29_09-00-00_{SESSION}_c0000.json"

CALLER = {"id": "u-1", "cognito_sub": "sub-1", "company_id": CO, "email": "a@x.nz",
          "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
          "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-09-29"}


class FakeConn:
    outcomes = []

    def __enter__(self):
        return self

    def transaction(self):
        conn = self

        class _Tx:
            def __enter__(self):
                return conn

            def __exit__(self, exc_type, *a):
                FakeConn.outcomes.append("rollback" if exc_type else "commit")
                return False

        return _Tx()

    def __exit__(self, *a):
        return False

    def cursor(self, row_factory=None):
        return self

    def execute(self, sql, params=None):
        return self

    def fetchone(self):
        return None


def _row():
    return {"id": SUG, "heard_name": "Petros", "company_name": "Cassidy",
            "quote": "Hi, this is Petros from Cassidy",
            "session_base": SESSION, "source_filename": SRC, "speaker_label": "spk_0",
            "user_folder": "Ben1", "session_date": "2026-09-29",
            "start_sec": 0.0, "end_sec": 6.0, "state": "confirmed"}


def _event(decision="confirmed", display_name=None, sub="sub-1"):
    body = {"decision": decision}
    if display_name is not None:
        body["display_name"] = display_name
    return {"httpMethod": "POST", "path": "/api/org/name-suggestions/" + SUG,
            "body": json.dumps(body),
            "requestContext": {"authorizer": {"claims": {"sub": sub}}}}


@pytest.fixture
def wired(monkeypatch):
    queued = []

    class FakeS3:
        def put_object(self, **kw):
            queued.append(json.loads(kw["Body"]))
            return {}

    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "on")
    monkeypatch.setattr(org, "get_connection", lambda: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(CALLER))
    monkeypatch.setattr(org, "s3", lambda: FakeS3())
    monkeypatch.setattr(org.speaker_intro_suggestions, "decide",
                        lambda conn, co, sid, state, decided_by=None: dict(_row(), state=state))
    monkeypatch.setattr(org.users, "get_by_folder_name",
                        lambda conn, co, folder: {"id": "u-9", "folder_name": folder})
    monkeypatch.setattr(org, "_session_turns", lambda conn, f, d, sb: [
        {"source_filename": SRC, "speaker_label": "spk_0", "start_sec": 0.0, "end_sec": 4.0},
        {"source_filename": SRC, "speaker_label": "spk_0", "start_sec": 9.0, "end_sec": 40.0},
        {"source_filename": SRC, "speaker_label": "spk_1", "start_sec": 50.0, "end_sec": 55.0},
    ])
    return queued


def test_a_confirmation_queues_a_correction_using_the_heard_name(wired):
    resp = org.lambda_handler(_event("confirmed"), None)
    assert resp["statusCode"] == 202, resp
    assert len(wired) == 1
    art = wired[0]
    assert art["correction"]["display_name"] == "Petros"
    assert art["correction"]["source_filename"] == SRC


def test_an_edited_display_name_wins_over_the_heard_name(wired):
    org.lambda_handler(_event("confirmed", display_name="Petros Pan"), None)
    assert wired[0]["correction"]["display_name"] == "Petros Pan"


def test_the_longest_turn_of_the_cluster_is_marked_not_the_intro_itself(wired):
    """Spec correction 5: 3 s is enough to detect, not to enrol. The window the
    correction marks is the cluster's LONGEST turn, same as a confirmed name proposal."""
    org.lambda_handler(_event("confirmed"), None)
    c = wired[0]["correction"]
    assert (c["start_sec"], c["end_sec"]) == (9.0, 40.0)


def test_falls_back_to_the_suggestions_own_window_when_the_cluster_is_gone(wired, monkeypatch):
    """When `_session_turns` no longer has the cluster, the intro is at least 3 s --
    propagation still works from the suggestion's own start/end."""
    monkeypatch.setattr(org, "_session_turns", lambda conn, f, d, sb: [])
    org.lambda_handler(_event("confirmed"), None)
    c = wired[0]["correction"]
    assert (c["start_sec"], c["end_sec"]) == (0.0, 6.0)


def test_a_rejection_queues_nothing(wired):
    resp = org.lambda_handler(_event("rejected"), None)
    assert resp["statusCode"] == 200, resp
    assert wired == []


def test_dismissed_or_empty_decision_is_400_and_queues_nothing(wired):
    for bad in ("dismissed", "", "ignored"):
        resp = org.lambda_handler(_event(bad), None)
        assert resp["statusCode"] == 400, repr(bad)
    assert wired == []


def test_an_empty_display_name_after_strip_is_refused(wired):
    resp = org.lambda_handler(_event("confirmed", display_name="   "), None)
    assert resp["statusCode"] == 400
    assert wired == []


def test_the_session_id_sent_to_speaker_corrections_is_the_source_filename(wired, monkeypatch):
    seen = {}
    real = org.speaker_corrections

    def _spy(conn, caller, session_base, event):
        seen["session_base"] = session_base
        return real(conn, caller, session_base, event)

    monkeypatch.setattr(org, "speaker_corrections", _spy)
    org.lambda_handler(_event("confirmed"), None)
    assert seen["session_base"] == SRC


def test_a_row_of_another_company_is_404(wired, monkeypatch):
    monkeypatch.setattr(org.speaker_intro_suggestions, "decide",
                        lambda conn, co, sid, state, decided_by=None: None)
    resp = org.lambda_handler(_event("confirmed"), None)
    assert resp["statusCode"] == 404
    assert wired == []


def test_a_refused_correction_leaves_the_suggestion_pending(wired, monkeypatch):
    FakeConn.outcomes = []
    monkeypatch.setattr(org, "speaker_corrections",
                        lambda conn, caller, sb, event: org.error("refused", 400))
    resp = org.lambda_handler(_event("confirmed"), None)
    assert resp["statusCode"] == 400
    assert FakeConn.outcomes == ["rollback"], FakeConn.outcomes


def test_the_writer_records_a_confirmed_introduction_as_a_human_correction(wired, monkeypatch):
    """The assertion this whole file exists for, same as the proposals' file: replay the
    queued artifact into the writer's real handler and read back `source`."""
    org.lambda_handler(_event("confirmed"), None)
    art = wired[0]

    stored = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(vw, "get_connection", lambda: Conn())
    monkeypatch.setattr(vw, "record_turn_name",
                        lambda conn, co, **kw: stored.append(kw) or {"id": "t"})
    monkeypatch.setattr(vw, "add_sample", lambda conn, co, *a, **kw: {"id": "s"})
    monkeypatch.setattr(vw, "record_attempt",
                        lambda conn, co, vp, outcome, detail=None: None)
    monkeypatch.setattr(vw, "live_turn_names", lambda conn, co, sb: [])
    monkeypatch.setattr(vw, "rejected_names", lambda conn, co, sb: set())

    vw.lambda_handler({
        "op": "propagation", "company_id": CO,
        "session_base": art["session_base"],
        "results": [{"turn_ref": SRC + "@9.0", "asserted": True,
                     "display_name": art["correction"]["display_name"],
                     "state": "confirmed"}],
    }, None)

    assert stored, "the writer stored no turn name"
    assert "correction" in [k.get("source") for k in stored]
