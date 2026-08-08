"""Unit: the group's merge-state row is created wherever a group_id is persisted.

The row must exist independently of the LEAD. The lead's /open is
fire-and-forget at record-start and a site is routinely offline then, so a
design that waited for the lead row would leave exactly the groups this feature
exists for — the ones formed in a shed with no signal — permanently unclaimable.

There are TWO paths a group_id arrives on, and both persist it via
ensure_open(group_id=...):

  * POST /api/org/sessions/{id}/open  — the live join, when there is signal
  * the upload-url adopt path         — the offline join, hours later

The second is not a fallback; it is the primary path for the use case (hand a
spare unit to an inspector in a shed). A group row created only on /open would
silently never merge for it.
"""
import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

LEAD = "a" * 32
JOINER = "b" * 32
CALLER = {"id": "u-1", "company_id": "c-1"}


@pytest.fixture
def created(monkeypatch):
    rows = []
    monkeypatch.setattr(org.session_group, "ensure_row",
                        lambda conn, gid, cid: rows.append((gid, cid)))
    return rows


def test_a_joiner_creates_the_group_row(created):
    org._ensure_group_state(object(), "c-1", LEAD)
    assert created == [(LEAD, "c-1")]


def test_a_solo_recording_creates_no_group_row(created):
    org._ensure_group_state(object(), "c-1", None)
    assert created == [], "a solo session must not create group state"


def test_a_failure_to_record_the_group_never_breaks_the_call(monkeypatch, created):
    # Losing the group row costs a merge. Raising here would cost the /open or,
    # worse, the upload — and the upload is the synchronous no-retry route where
    # a 500 strands the recording (BUG-43's family).
    def boom(conn, gid, cid):
        raise RuntimeError("db hiccup")
    monkeypatch.setattr(org.session_group, "ensure_row", boom)
    org._ensure_group_state(object(), "c-1", LEAD)      # must not raise


def test_the_live_join_path_creates_it(monkeypatch, created):
    monkeypatch.setattr(org.meeting_session, "get", lambda conn, sid: None)
    monkeypatch.setattr(org.meeting_session, "group_ended_at", lambda conn, gid: None)
    monkeypatch.setattr(org.meeting_session, "ensure_open",
                        lambda *a, **k: {"session_id": JOINER, "status": "open",
                                         "version": 1, "group_id": LEAD})
    # No siteId in the body, so no site lookup happens.
    org.session_open(object(), CALLER, JOINER, {"groupId": LEAD})
    assert created == [(LEAD, "c-1")]


def test_the_offline_upload_path_creates_it_too(monkeypatch, created):
    # THE case the feature exists for: joined in a shed with no signal, so
    # /open never landed and the group only ever arrives on the upload.
    monkeypatch.setattr(org.meeting_session, "get", lambda conn, sid: None)
    monkeypatch.setattr(org.meeting_session, "group_ended_at", lambda conn, gid: None)
    monkeypatch.setattr(org.meeting_session, "ensure_open", lambda *a, **k: None)
    fname = f"ben_2026-08-08_10-00-00_sid{JOINER}_c0000.wav"
    org._adopt_group_from_upload(object(), CALLER, {"groupId": LEAD}, fname, "audio", None)
    assert created == [(LEAD, "c-1")], \
        "a group that only ever arrives on an upload must still be mergeable"
