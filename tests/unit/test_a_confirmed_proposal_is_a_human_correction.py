"""Confirming a proposal must produce `source='correction'`. Nothing else about it matters.

THE WHOLE FEATURE TURNS ON ONE STRING, and getting it wrong is silent in every direction.

`recompute_company_floor` counts only `source='correction'` rows. A company needs 20 of
them before it has a rejection floor, and without a floor `decide_name` can never return
`confirmed` -- every automatic name stays a guess with a question mark, forever. The
proposal queue exists to produce those 20 rows; the names it fixes along the way are a side
effect.

So a confirmation written straight to the database, or queued as a propagation, would:

  * name the person correctly
  * render correctly in the transcript
  * satisfy the user who clicked it
  * contribute NOTHING to the floor
  * pass every test that checks the name

That is the same shape as the score which never crossed the embedder/writer seam for 604
rows, and as the label_map the hop dropped -- each half correct, the join silently empty.
This file asks the only question that catches it: what `source` reaches the writer.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")
vw = pytest.importorskip("lambda_voiceprint_writer", reason="requires psycopg")

CO = "11111111-1111-1111-1111-111111111111"
VP = "22222222-2222-2222-2222-222222222222"
PROP = "33333333-3333-3333-3333-333333333333"
SESSION = "Benl1_2026-08-13_11-49-00_sid9db9293e82b94a4d9611572b1233f82d"
SRC = "Benl1_2026-08-13_11-49-00_off0.0_to60.0_srcwav.json"

CALLER = {"id": "u-1", "cognito_sub": "sub-1", "company_id": CO, "email": "a@x.nz",
          "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
          "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-08-13"}


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self, row_factory=None):
        return self

    def execute(self, sql, params=None):
        return self

    def fetchone(self):
        return None


def _event(decision="confirmed"):
    return {"httpMethod": "POST", "path": "/api/org/name-proposals/" + PROP,
            "body": json.dumps({"decision": decision}),
            "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}}}


@pytest.fixture
def wired(monkeypatch):
    """org-api with its database and S3 replaced, capturing the artifact it queues."""
    queued = []

    class FakeS3:
        def put_object(self, **kw):
            queued.append(json.loads(kw["Body"]))
            return {}

    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "on")
    monkeypatch.setattr(org, "get_connection", lambda: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(CALLER))
    monkeypatch.setattr(org, "s3", lambda: FakeS3())
    monkeypatch.setattr(org.speaker_name_proposals, "decide",
                        lambda conn, co, pid, state, decided_by=None: {
                            "id": PROP, "voiceprint_id": VP, "session_base": SESSION,
                            "source_filename": SRC, "speaker_label": "spk_0",
                            "user_folder": "Ben_UCPK", "session_date": "2026-08-13",
                            "state": state})
    # The folder the correction names must resolve inside the caller's company; the guard
    # that checks it is the cross-tenant one and is exercised in its own file.
    monkeypatch.setattr(org.users, "get_by_folder_name",
                        lambda conn, co, folder: {"id": "u-9", "folder_name": folder})
    monkeypatch.setattr(org.voiceprints, "get_profile",
                        lambda conn, co, vid: {"id": VP, "display_name": "Ben Lin"})
    monkeypatch.setattr(org, "_session_turns", lambda conn, f, d, sb: [
        {"source_filename": SRC, "speaker_label": "spk_0", "start_sec": 0.0, "end_sec": 4.0},
        {"source_filename": SRC, "speaker_label": "spk_0", "start_sec": 9.0, "end_sec": 31.0},
        {"source_filename": SRC, "speaker_label": "spk_1", "start_sec": 40.0, "end_sec": 55.0},
    ])
    return queued


def test_a_confirmation_queues_the_same_artifact_a_rename_does(wired):
    """It must go through `speaker_corrections`, not around it."""
    resp = org.lambda_handler(_event("confirmed"), None)
    assert resp["statusCode"] == 202, resp
    assert len(wired) == 1, "a confirmation queued no correction artifact"
    art = wired[0]
    assert art["correction"]["display_name"] == "Ben Lin"
    assert art["correction"]["source_filename"] == SRC


def test_the_longest_turn_in_the_cluster_is_the_one_marked(wired):
    """A cluster is many turns and the correction marks one window.

    The longest carries the most evidence for the enrolment a correction may also trigger --
    the homogeneity guard refuses thin windows -- and taking the first would hand it
    whichever passage happened to be transcribed first.
    """
    org.lambda_handler(_event("confirmed"), None)
    c = wired[0]["correction"]
    assert (c["start_sec"], c["end_sec"]) == (9.0, 31.0), c


def test_the_writer_records_a_confirmation_as_a_human_correction(wired, monkeypatch):
    """**The assertion this whole file exists for.**

    The artifact the confirmation queued is replayed into the writer's real handler, and the
    `source` it stores is read back. A propagation here means the queue runs, names people,
    looks healthy and calibrates nothing.
    """
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

    # The shape the embedder hands the writer for the turn the human actually asserted.
    # `asserted` is what selects 'correction' over 'correction_propagation' in the writer,
    # so this is the row under test.
    vw.lambda_handler({
        "op": "propagation", "company_id": CO,
        "session_base": art["session_base"],
        "results": [{"turn_ref": SRC + "@9.0", "asserted": True,
                     "display_name": art["correction"]["display_name"],
                     "state": "confirmed"}],
    }, None)

    assert stored, "the writer stored no turn name"
    sources = [k.get("source") for k in stored]
    assert "correction" in sources, (
        "a confirmed proposal was stored as " + repr(sources) + ". Only source='correction' "
        "is counted by recompute_company_floor, so anything else means this queue names "
        "people correctly and calibrates nothing -- and no other assertion would notice")


def test_a_rejection_writes_no_name_at_all(wired):
    """A rejection is a judgement about a fact, not a naming act. It must queue nothing."""
    resp = org.lambda_handler(_event("rejected"), None)
    assert resp["statusCode"] == 200, resp
    assert wired == [], "a rejection queued a correction artifact; it named somebody"


def test_closing_the_dialog_is_not_a_decision_this_route_accepts(wired):
    """Dismissal must leave the row pending for the bell.

    A route that accepted it as a state would let a mis-click consume five candidates, and
    at one confirmation per gesture with twenty needed that is a quarter of a company's
    calibration, gone with nothing recording that it happened.
    """
    for bad in ("dismissed", "pending", "", "ignored"):
        resp = org.lambda_handler(_event(bad), None)
        assert resp["statusCode"] == 400, repr(bad) + " was accepted as a decision"
    assert wired == []
