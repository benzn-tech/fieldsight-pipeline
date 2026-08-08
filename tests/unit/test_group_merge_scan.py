"""Unit: the standing scan that claims settled groups (Phase C, Task 4).

Every test here is a regression guard for a way the FIRST design failed. That
one hung the merge check on a finalize event, and a group becomes mergeable at a
moment when nothing is firing:

  * on the tick that finalizes the last member, that member is still
    `finalizing` with a fresh last_segment_at, so the settled test is
    necessarily false;
  * it becomes true a tick later, when reconcile flips it to `sent` — and
    sweep() only iterates DUE sessions, of which there are then none;
  * the LEAD carries no group_id of its own, so a check reading it off the
    finalizing row had nothing to look up when the lead stopped last;
  * a `sent` session never re-enters the due path, so the late-arrival re-merge
    had no trigger either.

sweep_groups takes injected collaborators for the same reason the rest of this
module does: it must be testable without a database or S3.
"""
import pytest

fc = pytest.importorskip("lambda_finalize_claim", reason="requires psycopg (installed in CI)")

GID = "a" * 32
JOINER = "b" * 32


def _members(company="co-1"):
    return [{"session_id": GID, "user_id": "u1", "company_id": company},
            {"session_id": JOINER, "user_id": "u2", "company_id": company}]


def _ctx(conn, row):
    return {"recipient": "x@y.z", "folder": "Ben_UCPK", "date": "2026-08-07",
            "siteName": None, "timeRange": None}


def _run(**over):
    kw = dict(
        list_due=lambda conn: [{"group_id": GID, "company_id": "co-1"}],
        claim=lambda conn, gid, key: True,
        mark_result=lambda conn, gid, r: None,
        span_ok=lambda conn, gid: True,
        members_of=lambda conn, gid: _members(),
        resolve_ctx=_ctx,
        enqueue=lambda art: None,
    )
    kw.update(over)
    return fc.sweep_groups(object(), **kw)


def test_a_due_group_is_claimed_and_enqueued_with_every_member():
    sent = []
    out = _run(enqueue=sent.append)
    assert len(sent) == 1
    art = sent[0]
    assert art["groupId"] == GID and art["leadSessionId"] == GID
    assert {m["sessionBase"] for m in art["members"]} == {"sid" + GID, "sid" + JOINER}
    assert art["mergedKey"].endswith(f"grp{GID}.json")
    assert art["mergedKey"].startswith("extractions/Ben_UCPK/2026-08-07/")
    assert out and out[0]["status"] == "claimed"


def test_it_runs_when_no_session_is_due():
    # THE regression. The first design asked only while finalizing a session, so
    # a group that settled between ticks was never looked at again. Nothing here
    # references a due session at all — that is the point.
    sent = []
    _run(enqueue=sent.append)
    assert len(sent) == 1, "the scan must not depend on a session being due"


def test_a_losing_claim_enqueues_nothing():
    sent = []
    out = _run(claim=lambda conn, gid, key: False, enqueue=sent.append)
    assert sent == [] and out[0]["status"] == "lost-claim"


def test_a_stale_group_is_rejected_and_never_enqueued():
    # A device that kept a group overnight presents it again the next day and
    # nothing on the device objects. Merging yesterday into today reads
    # perfectly fluently, which is why this guard is unconditional.
    sent, results = [], []
    _run(span_ok=lambda conn, gid: False,
         mark_result=lambda conn, gid, r: results.append(r), enqueue=sent.append)
    assert sent == [] and results == ["rejected"]


def test_a_cross_company_group_is_rejected():
    # Phase A's rejection fires only when the lead row EXISTS at join time; an
    # unknown lead is deliberately accepted so joining does not depend on call
    # ordering. So a cross-company group is representable in the data and the
    # server must re-check here.
    sent, results = [], []
    mixed = [{"session_id": GID, "user_id": "u1", "company_id": "co-1"},
             {"session_id": JOINER, "user_id": "u2", "company_id": "co-2"}]
    _run(members_of=lambda conn, gid: mixed,
         mark_result=lambda conn, gid, r: results.append(r), enqueue=sent.append)
    assert sent == [] and results == ["rejected"]


def test_a_group_whose_members_resolve_to_nothing_is_marked_empty():
    # Settled with nothing usable. Without a terminal result it would sit in the
    # candidate set being re-read every minute forever.
    sent, results = [], []
    _run(resolve_ctx=lambda conn, row: {"folder": None, "date": None},
         mark_result=lambda conn, gid, r: results.append(r), enqueue=sent.append)
    assert sent == [] and results == ["empty"]


def test_the_merged_key_uses_the_LEADs_folder_and_date():
    # A group can straddle NZ midnight, so members legitimately have different
    # dates. The artifact takes the lead's; each member's own date is used for
    # its own key (that part is extract-session's job).
    sent = []

    def ctx(conn, row):
        if row["session_id"] == GID:
            return {"folder": "Lead_Folder", "date": "2026-08-07"}
        return {"folder": "Joiner_Folder", "date": "2026-08-08"}

    _run(resolve_ctx=ctx, enqueue=sent.append)
    assert sent[0]["mergedKey"] == f"extractions/Lead_Folder/2026-08-07/grp{GID}.json"


def test_a_group_with_no_lead_context_still_merges():
    # The lead may never have uploaded, so it may have no folder/date at all.
    # The group id is an identifier, not a claim of authorship — falling back to
    # a member's folder keeps the meeting rather than discarding it.
    sent = []

    def ctx(conn, row):
        if row["session_id"] == GID:
            return {"folder": None, "date": None}
        return {"folder": "Joiner_Folder", "date": "2026-08-08"}

    _run(resolve_ctx=ctx, enqueue=sent.append)
    assert sent[0]["mergedKey"] == f"extractions/Joiner_Folder/2026-08-08/grp{GID}.json"


class _SavepointConn:
    """`with conn.transaction()` -> a nested savepoint. See
    test_finalize_claim for why the scan runs inside one."""

    def transaction(self):
        class _Tx:
            def __enter__(self_): return None
            def __exit__(self_, *a): return False
        return _Tx()


def test_the_flag_being_off_means_the_scan_never_runs(monkeypatch):
    monkeypatch.setattr(fc, "ENABLE_GROUP_MERGE", False)
    called = []
    fc._sweep_groups_contained(_SavepointConn(), scan=lambda conn: called.append(1))
    assert called == [], "prod must be inert with the flag off"


def test_the_flag_being_on_runs_the_scan(monkeypatch):
    monkeypatch.setattr(fc, "ENABLE_GROUP_MERGE", True)
    called = []
    fc._sweep_groups_contained(_SavepointConn(),
                               scan=lambda conn: called.append(1) or [])
    assert called == [1]


def test_the_handler_actually_calls_the_scan(monkeypatch):
    # The gap this whole phase exists to close: Phases A and B shipped three
    # working functions that no production code called. A scan nobody invokes
    # is the same defect wearing a different hat.
    monkeypatch.setattr(fc, "sweep", lambda conn: [])
    monkeypatch.setattr(fc, "reconcile", lambda conn, r: [])
    called = []
    monkeypatch.setattr(fc, "_sweep_groups_contained",
                        lambda conn: called.append(1) or [])

    class _Conn:
        def __enter__(self): return object()
        def __exit__(self, *a): return False
    monkeypatch.setitem(__import__("sys").modules, "db.connection",
                        type("M", (), {"get_connection": staticmethod(lambda: _Conn())}))

    fc.lambda_handler({}, None)
    assert called == [1], "the standing scan is not wired into the sweep"


def test_the_scan_runs_after_reconcile(monkeypatch):
    # reconcile is what moves a claimed session to `sent`, and `sent` is what
    # makes its group settled. Scanning first would always be one tick behind.
    order = []
    monkeypatch.setattr(fc, "sweep", lambda conn: order.append("sweep") or [])
    monkeypatch.setattr(fc, "reconcile",
                        lambda conn, r: order.append("reconcile") or [])
    monkeypatch.setattr(fc, "_sweep_groups_contained",
                        lambda conn: order.append("groups") or [])

    class _Conn:
        def __enter__(self): return object()
        def __exit__(self, *a): return False
    monkeypatch.setitem(__import__("sys").modules, "db.connection",
                        type("M", (), {"get_connection": staticmethod(lambda: _Conn())}))

    fc.lambda_handler({}, None)
    assert order == ["sweep", "reconcile", "groups"]
