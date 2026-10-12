"""Task 8b: meeting-minutes folder discovery uses one delimiter listing of transcripts/ (every folder, including
ones in no directory entry); only a listing failure scans the prefix, with a WARNING; the per-folder
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


class _Paginator:
    def __init__(self, owner):
        self.owner = owner

    def paginate(self, Bucket, Prefix, Delimiter):
        self.owner.delimiter_calls.append((Prefix, Delimiter))
        if self.owner.fail:
            raise RuntimeError("listing failed")
        folders = sorted({k.split("/")[1] for k in KEYS})
        yield {"CommonPrefixes": [{"Prefix": f"transcripts/{f}/"} for f in folders]}


class _S3:
    def __init__(self):
        self.delimiter_calls = []
        self.fail = False

    def get_paginator(self, name):
        return _Paginator(self)


@pytest.fixture
def s3ed(monkeypatch):
    prefixes, seen = [], []
    fake = _S3()
    monkeypatch.setattr(mm, "s3_client", fake)

    def lst(bucket, prefix):
        prefixes.append(prefix)
        return [{"key": k} for k in KEYS if k.startswith(prefix)]
    monkeypatch.setattr(mm, "list_s3_objects", lst)
    monkeypatch.setattr(mm, "load_user_mapping", lambda bucket: {})
    monkeypatch.setattr(mm, "download_json_from_s3", lambda b, k: seen.append(k) or {
        "results": {"transcripts": [{"transcript": "slab"}], "items": []}})
    monkeypatch.setattr(mm.agent_turn_filter, "apply_agent_filter", lambda turns, *a, **k: (turns, None))
    return prefixes, seen, fake


def test_every_folder_is_found_by_one_delimiter_listing_not_a_prefix_walk(s3ed):
    prefixes, seen, fake = s3ed
    mm.collect_transcripts("b", D)
    assert fake.delimiter_calls == [("transcripts/", "/")]
    assert "transcripts/" not in prefixes          # no whole-prefix object listing
    # Ghost_Z is in no directory entry and is still collected (legacy folders hold transcripts)
    assert sorted(seen) == sorted(KEYS)


def test_a_listing_failure_scans_the_prefix_with_a_warning(s3ed, caplog):
    prefixes, seen, fake = s3ed
    fake.fail = True
    with caplog.at_level(logging.WARNING):
        mm.collect_transcripts("b", D)
    assert prefixes[0] == "transcripts/"
    assert sorted(seen) == sorted(KEYS)
    assert any("folder listing failed" in r.message for r in caplog.records)


def test_a_user_filter_lists_no_folders_at_all(s3ed):
    prefixes, seen, fake = s3ed
    mm.collect_transcripts("b", D, user_filter="Ada_L")
    assert fake.delimiter_calls == []
    assert prefixes == [f"transcripts/Ada_L/{D}/"]
