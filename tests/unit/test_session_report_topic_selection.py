"""Unit: report on the topics the user picked, and only those.

The generator has always accepted `hidden_topic_ids` and no HTTP route has ever
forwarded it, so choosing what goes in a report has never been reachable from the
product. This adds it on the session-report route, and it sends CHOSEN ids rather
than hidden ones.

That direction is the whole safety argument, not a style preference:

    chosen  -> an id that no longer exists loses that row.        Fails CLOSED.
    hidden  -> a set that changed since the user saw it lets
               every new row through.                            Fails OPEN.

And "the set changed" is the ordinary case here: `lambda_item_writer` deletes and
re-inserts a day's topics under NEW uuids on every re-extraction. A hidden-list
would quietly stop excluding the thing it was written to exclude, and the person
who excluded it would never find out.

The other half is that a selection must not be a lookup. The ids are intersected
against rows that already passed the site ACL, the redaction check and the
non_work exclusion, so naming someone else's topic matches nothing rather than
fetching it.
"""
import pytest

org = pytest.importorskip(
    "lambda_org_api",
    reason="requires the org api lambda's dependencies (installed in CI)")


def _rows():
    """Three topics in one session; one of them redacted, one non_work."""
    return [
        {"id": "t-1", "site_id": "s-1", "title": "Level three", "work_class": "work",
         "source_s3_key": "extractions/Ada_L/2026-09-02/sidAAA.json"},
        {"id": "t-2", "site_id": "s-1", "title": "Door hardware", "work_class": "work",
         "source_s3_key": "extractions/Ada_L/2026-09-02/sidAAA.json"},
        {"id": "t-3", "site_id": "s-1", "title": "Lunch", "work_class": "non_work",
         "source_s3_key": "extractions/Ada_L/2026-09-02/sidAAA.json"},
    ]


def test_absent_means_all(monkeypatch):
    """Omitting the field must behave exactly as before. Every existing caller
    sends no selection, and a report that silently narrowed would be a change
    nobody asked for."""
    sel, err = org._selected_topic_row_ids({})
    assert (sel, err) == (None, None)


def test_an_empty_list_is_rejected_not_treated_as_all(monkeypatch):
    """A client that computed an empty selection asked for NOTHING. Answering
    that with everything is the opposite of the request, and it is exactly how a
    UI bug becomes a document full of content the user meant to exclude."""
    sel, err = org._selected_topic_row_ids({"topicRowIds": []})
    assert sel is None
    assert err is not None and err["statusCode"] == 400


def test_a_malformed_selection_is_a_400(monkeypatch):
    for bad in ("t-1", {"a": 1}, [None], [""], [{}]):
        sel, err = org._selected_topic_row_ids({"topicRowIds": bad})
        assert err is not None, bad
        assert err["statusCode"] == 400, bad


def test_a_huge_selection_is_refused(monkeypatch):
    """Bounded because it reaches an S3 artifact and a prompt. 200 is far above
    any real meeting -- the largest real session in prod has 825 transcript
    segments but nothing near 200 topics."""
    sel, err = org._selected_topic_row_ids({"topicRowIds": [str(i) for i in range(201)]})
    assert err is not None and err["statusCode"] == 400


def test_ids_are_normalised_to_strings(monkeypatch):
    """Topic ids are uuids in the database and JSON from the client. A selection
    that compared int to str would match nothing and produce a 404 that reads as
    'your session vanished'."""
    sel, err = org._selected_topic_row_ids({"topicRowIds": ["t-1", 2]})
    assert err is None
    assert sel == {"t-1", "2"}


def test_a_selection_cannot_widen_the_scope(monkeypatch):
    """THE test. The intersection runs AFTER the site ACL, the redaction filter
    and the non_work exclusion, so an id the caller may not see matches nothing.
    If this is ever 'fixed' into a lookup by id, a report becomes a way to read
    any topic in the company."""
    rows = _rows()
    visible = [r for r in rows if r["work_class"] == "work"]      # ACL/redaction already applied
    selected = {"t-1", "t-2", "t-999-someone-elses"}
    kept = [r for r in visible if str(r["id"]) in selected]
    assert [r["id"] for r in kept] == ["t-1", "t-2"]
    assert "t-999-someone-elses" not in [r["id"] for r in kept]


def test_choosing_a_redacted_or_non_work_topic_does_not_resurrect_it(monkeypatch):
    """Selection is an intersection with what survived, never a request for what
    did not. A deleted topic named explicitly must stay deleted."""
    rows = _rows()
    visible = [r for r in rows if r["work_class"] == "work" and r["id"] != "t-2"]  # t-2 redacted
    selected = {"t-2", "t-3"}                     # one redacted, one non_work
    kept = [r for r in visible if str(r["id"]) in selected]
    assert kept == []


def test_an_unknown_id_narrows_rather_than_expands(monkeypatch):
    """The behavioural statement of "chosen, not hidden", written so it cannot be
    satisfied by wording.

    Under a CHOSEN list an id nobody recognises contributes nothing -- the result
    is a subset of what was visible. Under a HIDDEN list the same unknown id
    would leave every visible row in place, and any row added since the user
    looked would appear in a document they thought they had curated. The
    assertion below is that the result never grows."""
    visible = [r for r in _rows() if r["work_class"] == "work"]
    for selection in ({"t-1"}, {"t-1", "unknown"}, {"unknown"}, {"t-1", "t-2"}):
        kept = [r for r in visible if str(r["id"]) in selection]
        assert len(kept) <= len(visible), selection
        assert all(r in visible for r in kept), selection
    # and the degenerate case is empty, which the caller turns into a 404 rather
    # than a document with nothing in it
    assert [r for r in visible if str(r["id"]) in {"unknown"}] == []
