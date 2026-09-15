"""A report's scope drops deleted content by BOTH arms of the tombstone.

  topic arm  -- the rows that exist now (redactions keyed by topic id);
  source arm -- the rows ingest re-creates tomorrow under NEW uuids, which only a
                recording tombstone on the source key still names.

The session route applied only the first. Source prefixes are read for every
folder the rows come from, because a merged meeting's rows -- and its tombstone --
sit under the lead's folder. Spec 2026-09-15 §11.2.
"""
import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

DATE = "2026-09-10"
SID_A = "sid" + "a" * 32
SID_B = "sid" + "b" * 32
GRP = "grp" + "c" * 32
CALLER = {"id": "u-1", "company_id": "c-1", "folder_name": "James_Lamb", "global_role": "admin"}


def _row(rid, key, work_class="work"):
    return {"id": rid, "source_s3_key": key, "work_class": work_class, "site_name": "Waipuna"}


ROWS = [
    _row("t-1", f"extractions/James_Lamb/{DATE}/{SID_A}.json"),
    _row("t-2", f"extractions/James_Lamb/{DATE}/{SID_B}.json"),
    _row("t-3", f"extractions/Lead_F/{DATE}/{GRP}.json"),
    _row("t-4", f"reports/{DATE}/James_Lamb/daily_report.json"),
    _row("t-5", f"extractions/James_Lamb/{DATE}/{SID_A}.json", work_class="non_work"),
]


@pytest.fixture
def scope(monkeypatch):
    asked = []
    state = {"redacted": {}, "prefixes": {}}
    monkeypatch.setattr(org, "_day_report_rows", lambda conn, caller, folder, date: list(ROWS))
    monkeypatch.setattr(org.redactions, "list_active_for_topics",
                        lambda conn, ids: dict(state["redacted"]))

    def prefixes(conn, folder=None, date=None):
        asked.append(folder)
        return list(state["prefixes"].get(folder, []))

    monkeypatch.setattr(org.redactions, "deleted_source_prefixes", prefixes)
    return state, asked


def _ids(rows):
    return [r["id"] for r in rows]


def test_a_day_keeps_every_session_and_drops_non_extraction_and_non_work(scope):
    assert _ids(org._report_rows_in_scope(None, CALLER, "James_Lamb", DATE)) == ["t-1", "t-2", "t-3"]


def test_a_session_scope_keeps_only_that_session(scope):
    assert _ids(org._report_rows_in_scope(None, CALLER, "James_Lamb", DATE,
                                          session_id=SID_B)) == ["t-2"]


def test_the_topic_arm_drops_a_tombstoned_topic(scope):
    state, _ = scope
    state["redacted"] = {"t-2": {"id": "r-1"}}
    assert "t-2" not in _ids(org._report_rows_in_scope(None, CALLER, "James_Lamb", DATE))


def test_the_source_arm_drops_a_recreated_topic_no_topic_tombstone_names(scope):
    state, _ = scope
    state["prefixes"] = {"James_Lamb": [f"extractions/James_Lamb/{DATE}/{SID_A}"]}
    assert _ids(org._report_rows_in_scope(None, CALLER, "James_Lamb", DATE)) == ["t-2", "t-3"]


def test_a_merged_meeting_is_dropped_by_the_leads_tombstone(scope):
    state, asked = scope
    state["prefixes"] = {"Lead_F": [f"extractions/Lead_F/{DATE}/{GRP}"]}
    assert "t-3" not in _ids(org._report_rows_in_scope(None, CALLER, "James_Lamb", DATE))
    assert sorted(set(asked)) == ["James_Lamb", "Lead_F"]


def test_a_selection_only_narrows(scope):
    got = org._report_rows_in_scope(None, CALLER, "James_Lamb", DATE, selected={"t-2", "t-999"})
    assert _ids(got) == ["t-2"]


def test_a_failed_tombstone_lookup_raises_rather_than_including_everything(scope, monkeypatch):
    def boom(conn, folder=None, date=None):
        raise RuntimeError("redactions unreadable")
    monkeypatch.setattr(org.redactions, "deleted_source_prefixes", boom)
    with pytest.raises(RuntimeError):
        org._report_rows_in_scope(None, CALLER, "James_Lamb", DATE)
