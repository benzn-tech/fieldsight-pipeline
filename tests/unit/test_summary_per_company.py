"""Task 10: the nightly generator writes one summary per company.

The lake-wide reports/{date}/summary_report.json mixes every tenant. Each company now also gets
reports/{date}/companies/{company_id}/summary_report.json built ONLY from its own folders.
Left out of every company's summary (rulings C1, R10a): a folder whose directory entry has
has_external, and a folder with no directory entry / no company (legacy, e.g. Ben_Lin).
The whole generate_daily_report path runs here; only I/O and the model are faked.
"""
import json
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")

rg = pytest.importorskip("lambda_report_generator", reason="requires boto3/urllib3 (installed in CI)")
import directory  # noqa: E402
import lake_keys  # noqa: E402

CO_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CO_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
CO_C = "cccccccc-cccc-cccc-cccc-cccccccccccc"
DATE = "2026-10-12"

DOC = {"version": 2, "sites": {}, "people": {
    "Ada": {"name": "Ada", "company_id": CO_A},
    "Al": {"name": "Al", "company_id": CO_A},
    "Bo": {"name": "Bo", "company_id": CO_B},
    "Ext": {"name": "Ext", "company_id": CO_A, "has_external": True},
    "Cy": {"name": "Cy", "company_id": CO_C},        # a company with folders that day
    "NoCo": {"name": "NoCo"},                        # entry without a company
}}


@pytest.fixture
def run(monkeypatch):
    """run(folders, doc=DOC, flag=None, force=False) -> (puts, prompts, logs)."""
    def _run(folders, doc=DOC, flag=None, force=False, caplog=None):
        puts, prompts = [], []
        if flag is None:
            monkeypatch.delenv("SUMMARY_LAKE_WIDE", raising=False)
        else:
            monkeypatch.setenv("SUMMARY_LAKE_WIDE", flag)
        data = {f: {"transcripts": [{"timestamp": None, "device": "d", "who": f}],
                    "photos": [], "total_duration": 60.0, "total_words": 10}
                for f in folders}
        monkeypatch.setattr(rg, "load_prompt_templates", lambda b: None)
        monkeypatch.setattr(rg, "list_s3_objects",
                            lambda b, p: [{"key": f"transcripts/{f}/{DATE}/x.json"} for f in folders])
        monkeypatch.setattr(rg, "get_user_site_mapping", lambda b: ({}, {}, {}, {}))
        monkeypatch.setattr(rg, "read_meeting_manifest", lambda *a, **k: None)
        monkeypatch.setattr(rg, "process_user_data", lambda b, u, d, exclude_keys=None: data[u])
        monkeypatch.setattr(rg, "check_report_freshness", lambda *a, **k: (False, 0))
        monkeypatch.setattr(rg, "check_summary_freshness", lambda *a, **k: (False, 0))
        monkeypatch.setattr(rg, "build_weather_block_for_site", lambda *a, **k: None)
        monkeypatch.setattr(rg, "build_weather_findings", lambda *a, **k: None)
        monkeypatch.setattr(rg, "programme_for_site", lambda *a, **k: None)
        monkeypatch.setattr(rg, "generate_word_document", lambda *a, **k: None)
        monkeypatch.setattr(rg, "correlate_photos_with_transcripts", lambda t, p: t)
        monkeypatch.setattr(rg, "save_debug_record", lambda *a, **k: None)
        monkeypatch.setattr(directory, "load", lambda c, b: doc)
        monkeypatch.setattr(directory, "reset_cache", lambda: None)

        def prompt(correlated, who, *a, **k):
            if who == "All Workers":
                return "SUMMARY:" + ",".join(sorted(t["who"] for t in correlated))
            return "USER"
        monkeypatch.setattr(rg, "build_daily_prompt", prompt)

        def claude(p, max_tokens=4096):
            if not p.startswith("SUMMARY:"):
                return None, "per-user model off in this test"
            prompts.append(p)
            return json.dumps({"executive_summary": "s", "topics": []}), None
        monkeypatch.setattr(rg, "call_claude_structured", claude)

        def put(bucket, key, body, **kw):
            puts.append((key, json.loads(body) if key.endswith(".json") else None,
                         kw.get("owner_hint")))
        monkeypatch.setattr(rg, "lake_put", put)
        rg.generate_daily_report(DATE, force=force)
        return puts, prompts
    return _run


def _company_prompts(prompts):
    return {p for p in prompts}


def test_each_company_summary_holds_only_its_own_folders(run, caplog):
    caplog.set_level("INFO")
    puts, prompts = run(["Ada", "Al", "Bo", "Ext", "NoCo", "Ben_Lin"], flag="on")
    # lake-wide sees everyone (unchanged); A sees Ada+Al only; B sees Bo only
    assert sorted(prompts) == sorted([
        "SUMMARY:Ada,Al,Ben_Lin,Bo,Ext,NoCo",
        "SUMMARY:Ada,Al",
        "SUMMARY:Bo",
    ])
    # has_external: one INFO line naming the folder; legacy/no-company: counted
    assert any("Ext: has_external" in r.message for r in caplog.records)
    assert any("2 folder(s) with no directory company" in r.message for r in caplog.records)


def test_written_keys_and_ownership(run):
    puts, _ = run(["Ada", "Al", "Bo"], flag="on")
    summaries = {k: (body, hint) for k, body, hint in puts if "summary_report" in k
                 and k.endswith(".json") and "_debug" not in k}
    assert set(summaries) == {
        f"reports/{DATE}/summary_report.json",
        f"reports/{DATE}/companies/{CO_A}/summary_report.json",
        f"reports/{DATE}/companies/{CO_B}/summary_report.json",
    }
    body, hint = summaries[f"reports/{DATE}/companies/{CO_A}/summary_report.json"]
    assert hint == {"company_id": CO_A}
    assert body["users"] == ["Ada", "Al"]
    assert body["_report_metadata"]["scope"] == "company"
    assert body["_report_metadata"]["company_id"] == CO_A
    assert body["_report_metadata"]["recordings_processed"] == 2
    assert "scope" not in summaries[f"reports/{DATE}/summary_report.json"][0]["_report_metadata"]
    # every company key is a company_summary for that company, as the index will read it
    for k in summaries:
        if "/companies/" in k:
            assert lake_keys.parse(k)["kind"] == "company_summary"


def test_an_external_member_is_in_no_company_summary(run):
    puts, prompts = run(["Ada", "Ext", "Bo"], flag="on")
    assert "SUMMARY:Ada" in prompts and "SUMMARY:Bo" in prompts
    assert not any("Ext" in p for p in prompts if p != "SUMMARY:Ada,Bo,Ext")


def test_one_call_per_company_with_folders_and_none_for_empty_companies(run, caplog):
    caplog.set_level("INFO")
    _, prompts = run(["Ada", "Al", "Bo"], flag="on")  # Cy (company C) has no folder that day
    assert len(prompts) == 3                       # A, B and the lake-wide one
    assert any("Per-company summaries: 2 model call(s)" in r.message for r in caplog.records)
    assert not any(CO_C in p for p in prompts)


def test_flag_off_skips_only_the_lake_wide_summary(run):
    puts, prompts = run(["Ada", "Bo"], flag="off")
    keys = {k for k, _b, _h in puts}
    assert f"reports/{DATE}/summary_report.json" not in keys
    assert {f"reports/{DATE}/companies/{CO_A}/summary_report.json",
            f"reports/{DATE}/companies/{CO_B}/summary_report.json"} <= keys
    assert sorted(prompts) == ["SUMMARY:Ada", "SUMMARY:Bo"]      # no lake-wide model call either


def test_flag_is_read_at_call_time_and_defaults_off(run, monkeypatch):
    # Retired 2026-10-26: unset means OFF; only an explicit "on" (any case) writes it.
    puts, prompts = run(["Ada"], flag=None)
    keys = {k for k, _b, _h in puts}
    assert f"reports/{DATE}/summary_report.json" not in keys
    assert f"reports/{DATE}/companies/{CO_A}/summary_report.json" in keys
    assert prompts == ["SUMMARY:Ada"]
    puts, _ = run(["Ada"], flag="ON")
    assert f"reports/{DATE}/summary_report.json" in {k for k, _b, _h in puts}
    puts, _ = run(["Ada"], flag="anything-else")
    assert f"reports/{DATE}/summary_report.json" not in {k for k, _b, _h in puts}


def test_stale_check_ignores_a_leftover_lake_wide_summary_when_off(monkeypatch):
    # A summary_report.json written before the switch is never rewritten once off; its
    # recordings_processed must not decide staleness (it would re-flag the day nightly).
    docs = {f"reports/{DATE}/summary_report.json": {"_report_metadata": {"recordings_processed": 1}},
            f"reports/{DATE}/Ada/daily_report.json": {"_report_metadata": {"recordings_processed": 3}}}
    monkeypatch.setattr(rg, "download_json_from_s3", lambda b, k: docs.get(k))
    monkeypatch.setattr(rg, "list_s3_objects", lambda b, p: [{"key": k} for k in docs if k.startswith(p)])
    monkeypatch.delenv("SUMMARY_LAKE_WIDE", raising=False)
    assert rg.get_report_transcript_count("bkt", DATE) == 3
    monkeypatch.setenv("SUMMARY_LAKE_WIDE", "on")
    assert rg.get_report_transcript_count("bkt", DATE) == 1


def test_unreadable_directory_writes_no_company_summary(run):
    puts, prompts = run(["Ada", "Bo"], doc={"people": {}}, flag="on")
    assert not any("/companies/" in k for k, _b, _h in puts)
    assert prompts == ["SUMMARY:Ada,Bo"]                          # lake-wide unaffected
    # Default (lake-wide off): fails closed -- no day summary of any kind is written.
    puts, prompts = run(["Ada", "Bo"], doc={"people": {}})
    assert not any("summary_report" in k for k, _b, _h in puts)
    assert prompts == []
