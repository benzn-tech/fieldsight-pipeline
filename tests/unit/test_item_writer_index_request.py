"""item-writer emits the search index request post-commit, behind SEARCH_INDEX_WRITE."""
import json

from tests.unit.test_lambda_item_writer import (  # noqa: F401
    EXTRACTION_KEY, FakeS3, iw, make_extraction, wired,
)


def _spy(wired):
    built, emitted = [], []

    def build(conn, key, **kw):
        built.append((key, kw))
        return {"extraction_key": key, **kw}

    wired.setattr(iw.index_request, "build", build)
    wired.setattr(iw.index_request, "emit", lambda s3, bucket, req: emitted.append(req) or "k")
    return built, emitted


def test_one_request_when_flag_on(wired):
    built, emitted = _spy(wired)
    wired.setattr(iw, "SEARCH_INDEX_WRITE", True)

    result = iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert result == {"skipped": False, "topics": 1}
    assert len(emitted) == 1
    _, kw = built[0]
    assert kw["site_id"] == "site-1" and kw["owner_company_id"] == "co-1"
    assert kw["session_base"] == "Benl1_2026-07-06_10-00-00" and kw["report_date"] == "2026-07-06"
    assert kw["replaces"] == [] and kw["member_bases"] == []


def test_none_when_flag_off(wired):
    built, emitted = _spy(wired)
    wired.setattr(iw, "SEARCH_INDEX_WRITE", False)
    iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)
    assert built == [] and emitted == []


def test_zero_topic_session_still_emits(wired):
    built, emitted = _spy(wired)
    wired.setattr(iw, "SEARCH_INDEX_WRITE", True)
    wired.setattr(iw, "_s3_client",
                  FakeS3({EXTRACTION_KEY: json.dumps(make_extraction(topics=[]))}))
    assert iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY) == {
        "skipped": False, "topics": 0}
    assert len(emitted) == 1


def test_no_request_for_skipped_session(wired):
    built, emitted = _spy(wired)
    wired.setattr(iw, "SEARCH_INDEX_WRITE", True)
    wired.setattr(iw.lambda_ingest, "resolve_site", lambda *a: None)
    assert iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)["skipped"]
    assert emitted == []


def test_emit_failure_is_logged_and_does_not_raise(wired, caplog, capsys):
    _spy(wired)
    wired.setattr(iw, "SEARCH_INDEX_WRITE", True)

    def boom(*a):
        raise RuntimeError("s3 down")

    wired.setattr(iw.index_request, "emit", boom)
    with caplog.at_level("ERROR"):
        result = iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert result == {"skipped": False, "topics": 1}
    assert "search index request could not be written" in caplog.text
    assert "SearchIndexRequestFailed" in capsys.readouterr().out


def test_group_tier_carries_replaces_and_member_bases(wired):
    built, emitted = _spy(wired)
    wired.setattr(iw, "SEARCH_INDEX_WRITE", True)
    members = ["extractions/Ben_X/2026-07-06/A_2026-07-06_10-00-00.json",
               "extractions/Sam_Y/2026-07-06/B_2026-07-06_10-00-30.json"]
    wired.setattr(iw, "_s3_client", FakeS3({EXTRACTION_KEY: json.dumps(make_extraction(
        tier="group", groupId="G1", mergedMembers=members))}))
    wired.setattr(iw, "ENABLE_GROUP_MERGE", False)

    iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    _, kw = built[0]
    assert kw["tier"] == "group"
    assert kw["replaces"] == members
    assert kw["member_bases"] == ["A_2026-07-06_10-00-00", "B_2026-07-06_10-00-30"]
