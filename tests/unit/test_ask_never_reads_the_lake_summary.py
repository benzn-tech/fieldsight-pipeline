"""Ask never puts the lake-wide summary into the prompt.

load_report fell back to reports/{date}/summary_report.json -- built across EVERY
tenant's daily reports -- whenever the asked-about person had no daily report or
minutes that day, and the prompt then carried other companies' content into the
answer. Live on prod: prod has no RAG_SEARCH_FUNCTION, so Ask takes this legacy
path (see test_lambda_ask_agent_rag.py). Owner decision 2026-10-05: the lake-wide
summary is never served below platform_admin, and this lambda cannot tell callers
apart by company, so it is never served here at all.
"""
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")

laa = pytest.importorskip("lambda_ask_agent", reason="requires boto3/urllib3 (installed in CI)")

DATE = "2026-08-14"
SUMMARY_KEY = f"reports/{DATE}/summary_report.json"


@pytest.fixture
def lake(monkeypatch):
    objects = {SUMMARY_KEY: {"topics": [{"title": "OTHER TENANT SECRET"}]}}
    read = []

    def fake_download(bucket, key):
        read.append(key)
        return objects.get(key)

    monkeypatch.setattr(laa, "download_json_from_s3", fake_download)
    monkeypatch.setattr(laa, "_deleted_sessions", lambda *a, **k: set())
    return objects, read


def test_a_person_with_no_report_gets_none_not_the_summary(lake):
    _, read = lake
    assert laa.load_report("bucket", DATE, "Ben Lin") == (None, None)
    assert SUMMARY_KEY not in read


def test_a_person_with_a_report_still_gets_it(lake):
    """Positive control: the per-person path is untouched, so the None above is the
    removed fallback and not a broken loader."""
    objects, read = lake
    objects[f"reports/{DATE}/Ben_Lin/daily_report.json"] = {"topics": [{"title": "mine"}]}
    data, kind = laa.load_report("bucket", DATE, "Ben Lin")
    assert kind == "daily" and data["topics"][0]["title"] == "mine"
    assert SUMMARY_KEY not in read
