"""The backfill_lake_objects operator task: dry run by default, a write only on a boolean."""
import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")


class Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def seen(monkeypatch):
    calls = []
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: Conn())
    monkeypatch.setattr(org, "s3", lambda: "s3")

    def fake(conn, s3_client, **kw):
        calls.append(kw)
        return {"ok": True}
    monkeypatch.setattr(org, "backfill_lake_objects", fake)
    return calls


def _go(**event):
    return org.lambda_handler({"task": "backfill_lake_objects", **event}, None)


def test_no_flags_is_a_dry_run(seen):
    assert _go() == {"ok": True}
    assert seen[0]["apply"] is False and seen[0]["tag"] is False
    assert seen[0]["time_budget_s"] == 780


@pytest.mark.parametrize("event,expected", [
    ({"apply": True}, True),
    ({"dry_run": False}, True),
    ({"apply": True, "dry_run": True}, False),     # disagreement: the dry run wins
    ({"dry_run": True}, False),
    ({"apply": "true"}, False),                    # strings never write
    ({"apply": 1}, False),
    ({"dry_run": "false"}, False),
    ({"dry_run": 0}, False),
])
def test_only_booleans_turn_writing_on(seen, event, expected):
    _go(**event)
    assert seen[0]["apply"] is expected


def test_tag_and_budget_and_cursor_pass_through(seen):
    _go(apply=True, tag=True, time_budget_s=5, start_after="k", start_bucket="b",
        prefixes=["transcripts/"], bucket="only")
    kw = seen[0]
    assert kw["tag"] is True and kw["time_budget_s"] == 5 and kw["start_after"] == "k"
    assert kw["start_bucket"] == "b" and kw["prefixes"] == ["transcripts/"]
    assert kw["buckets"] == ["only"]
    _go(tag="yes", time_budget_s=True)
    assert seen[1]["tag"] is False and seen[1]["time_budget_s"] == 780
