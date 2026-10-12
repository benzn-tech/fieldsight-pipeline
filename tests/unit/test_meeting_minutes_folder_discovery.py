"""Task 8b: meeting-minutes folder discovery reads config/directory.json people instead of
listing transcripts/; an absent directory scans the prefix with a WARNING; the per-folder
date listing is unchanged."""
import logging
import os

import pytest

os.environ.setdefault("S3_BUCKET", "b")
os.environ.setdefault("ANTHROPIC_API_KEY", "k")

mm = pytest.importorskip("lambda_meeting_minutes",
                         reason="requires the meeting minutes lambda's dependencies (installed in CI)")

D = "2026-10-01"
KEYS = [f"transcripts/Ada_L/{D}/a1.json", f"transcripts/Bo_K/{D}/b1.json",
        f"transcripts/Ghost_Z/{D}/g1.json"]


@pytest.fixture
def s3ed(monkeypatch):
    prefixes, seen = [], []

    def lst(bucket, prefix):
        prefixes.append(prefix)
        return [{"key": k} for k in KEYS if k.startswith(prefix)]
    monkeypatch.setattr(mm, "list_s3_objects", lst)
    monkeypatch.setattr(mm, "load_user_mapping", lambda bucket: {})
    monkeypatch.setattr(mm, "download_json_from_s3", lambda b, k: seen.append(k) or {
        "results": {"transcripts": [{"transcript": "slab"}], "items": []}})
    monkeypatch.setattr(mm.agent_turn_filter, "apply_agent_filter", lambda turns, *a, **k: (turns, None))
    return prefixes, seen


def test_folders_come_from_the_directory_and_transcripts_is_never_listed(monkeypatch, s3ed):
    prefixes, seen = s3ed
    monkeypatch.setattr(mm.directory, "load",
                        lambda c, b: {"people": {"Ada_L": {}, "Bo_K": {}}, "sites": {}})
    mm.collect_transcripts("b", D)
    assert "transcripts/" not in prefixes
    assert sorted(prefixes) == [f"transcripts/Ada_L/{D}/", f"transcripts/Bo_K/{D}/"]
    assert sorted(seen) == sorted(KEYS[:2])


def test_absent_directory_scans_the_prefix_with_a_warning(monkeypatch, s3ed, caplog):
    prefixes, seen = s3ed
    monkeypatch.setattr(mm.directory, "load", lambda c, b: {"people": {}, "sites": {}})
    with caplog.at_level(logging.WARNING):
        mm.collect_transcripts("b", D)
    assert prefixes[0] == "transcripts/"
    assert sorted(seen) == sorted(KEYS)
    assert any("directory absent" in r.message for r in caplog.records)


def test_a_user_filter_does_not_read_the_directory_at_all(monkeypatch, s3ed):
    prefixes, seen = s3ed

    def boom(*a):
        raise AssertionError("directory read with a user filter")
    monkeypatch.setattr(mm.directory, "load", boom)
    mm.collect_transcripts("b", D, user_filter="Ada_L")
    assert prefixes == [f"transcripts/Ada_L/{D}/"]
