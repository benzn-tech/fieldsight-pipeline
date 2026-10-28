"""Retiring config/user_mapping.json: the readers take identity from the
published directory (config/directory.json), keyed by recording folder.

The file was hand-edited in August and nothing writes it. Every report stamped
from it carried the August answer, and a missing file made each reader fall
back to `{}` without a word.
"""
import io
import json
import logging
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("S3_BUCKET", "b")
os.environ.setdefault("ANTHROPIC_API_KEY", "k")

import directory  # noqa: E402

OLD_KEY = "config/user_mapping.json"   # the frozen file; nothing in directory.py reads it now
TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "..", "src", "template.yaml")

DIRECTORY_DOC = {
    "version": 1, "published_at": "2026-10-08T00:00:00+00:00",
    "people": {
        "Deandre__Alberts": {"name": "Deandre Alberts", "role": "gm", "company_id": "co-1",
                             "primary_site": "id-a", "sites": ["id-a"]},
        "Ben_Lin": {"name": "Ben Lin", "role": "pm", "company_id": "co-2",
                    "primary_site": None, "sites": ["id-a", "id-b"]},
    },
    "sites": {
        "id-a": {"slug": "main-site", "name": "Alpha School", "location": "Chch",
                 "client": "MoE", "company_id": "co-1"},
        "id-b": {"slug": "main-site", "name": "Alpha Schooling", "company_id": "co-2"},
    },
}
LEGACY_DOC = {
    "mapping": {"Benl1": {"name": "Ben Lin", "role": "site_manager",
                          "primary_site": "uc-pk", "sites": ["uc-pk"]},
                "Benl2": "Plain String"},
    "sites": {"uc-pk": {"name": "UC PK", "latitude": None, "longitude": None}},
}


class _NoSuchKey(Exception):
    def __init__(self):
        super().__init__("NoSuchKey")
        self.response = {"Error": {"Code": "NoSuchKey"}}


try:
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover
    ClientError = None


class FakeS3:
    def __init__(self, objects=None):
        self.objects = {k: json.dumps(v) for k, v in (objects or {}).items()}
        self.gets = []

    def get_object(self, Bucket, Key):
        self.gets.append(Key)
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key].encode("utf-8"))}


@pytest.fixture(autouse=True)
def fresh_cache():
    directory.reset_cache()
    yield
    directory.reset_cache()


# ---- directory.load ---------------------------------------------------------------

def test_THE_load_prefers_the_directory_and_never_touches_the_old_file():
    s3 = FakeS3({directory.KEY: DIRECTORY_DOC, OLD_KEY: LEGACY_DOC})
    doc = directory.load(s3, "b")
    assert doc["people"]["Deandre__Alberts"]["role"] == "gm"
    assert s3.gets == [directory.KEY]


def test_THE_the_old_file_is_never_read_even_when_the_directory_is_missing(caplog):
    """M5: the transition fallback is gone. A bucket that still holds the frozen file
    but has lost directory.json gets an empty directory and an ERROR -- never the
    August answer."""
    s3 = FakeS3({OLD_KEY: LEGACY_DOC})
    with caplog.at_level(logging.WARNING):
        doc = directory.load(s3, "b")
        directory.load(s3, "b")      # cached: no second read
    assert doc == directory.empty()
    assert OLD_KEY not in s3.gets and s3.gets == [directory.KEY]
    assert not [r for r in caplog.records if "user_mapping" in r.getMessage()]
    errs = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errs) == 1 and directory.KEY in errs[0].getMessage()


def test_the_legacy_adapter_is_gone():
    for name in ("LEGACY_KEY", "LEGACY_PREFIX", "adapt_legacy"):
        assert not hasattr(directory, name), name


def test_THE_both_missing_is_an_error_not_a_silent_empty(caplog):
    with caplog.at_level(logging.ERROR):
        doc = directory.load(FakeS3(), "b")
    assert doc["people"] == {} and doc["sites"] == {}
    errs = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errs and directory.KEY in errs[0].getMessage()


def test_a_403_on_a_missing_key_reads_as_absent_not_as_a_crash():
    class Denied(FakeS3):
        def get_object(self, Bucket, Key):
            if Key == directory.KEY:
                raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")
            return super().get_object(Bucket, Key)

    s3 = Denied({OLD_KEY: LEGACY_DOC})
    doc = directory.load(s3, "b")
    assert doc == directory.empty()
    assert OLD_KEY not in s3.gets


def _errors_after_load(caplog, s3):
    with caplog.at_level(logging.ERROR):
        doc = directory.load(s3, "b")
    return doc, [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_a_403_on_the_directory_is_reported_as_an_error(caplog):
    class Denied(FakeS3):
        def get_object(self, Bucket, Key):
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")
    doc, errs = _errors_after_load(caplog, Denied())
    assert doc == directory.empty() and errs and directory.KEY in errs[-1].getMessage()


def test_an_s3_failure_that_is_not_absence_is_an_empty_directory_and_an_error(caplog):
    class Throttled(FakeS3):
        def get_object(self, Bucket, Key):
            self.gets.append(Key)
            raise ClientError({"Error": {"Code": "SlowDown"}}, "GetObject")
    s3 = Throttled({OLD_KEY: LEGACY_DOC})
    doc, errs = _errors_after_load(caplog, s3)
    assert doc == directory.empty() and s3.gets == [directory.KEY]
    assert errs and directory.KEY in errs[-1].getMessage()


def test_a_malformed_directory_is_an_empty_directory_and_an_error(caplog):
    s3 = FakeS3({directory.KEY: {"sites": {}}, OLD_KEY: LEGACY_DOC})   # no `people`
    doc, errs = _errors_after_load(caplog, s3)
    assert doc == directory.empty() and s3.gets == [directory.KEY]
    assert errs and directory.KEY in errs[-1].getMessage()


# ---- report generator -------------------------------------------------------------

rg = pytest.importorskip("lambda_report_generator", reason="requires boto3 (installed in CI)")


def test_THE_a_daily_report_for_deandre_gets_role_and_site_from_the_directory(monkeypatch):
    """By folder, not by a display-name match: the old code looked `Deandre__Alberts`
    up among name variants of a hand-edited file that has never heard of him."""
    s3 = FakeS3({directory.KEY: DIRECTORY_DOC})
    monkeypatch.setattr(rg, "s3_client", s3)
    monkeypatch.setattr(rg, "download_json_from_s3",
                        lambda bucket, key: json.loads(s3.objects[key]) if key in s3.objects else None)
    primary, allsites, roles, sites_info = rg.get_user_site_mapping("b")
    assert roles["Deandre__Alberts"] == "gm"
    assert primary["Deandre__Alberts"] == "id-a"
    assert allsites["Ben_Lin"] == ["id-a", "id-b"] and "Ben_Lin" not in primary
    # Two companies share the slug `main-site`: neither overwrites the other.
    assert sites_info["id-a"]["name"] == "Alpha School"
    assert sites_info["id-b"]["name"] == "Alpha Schooling"
    assert sites_info["id-a"]["slug"] == sites_info["id-b"]["slug"] == "main-site"
    assert rg.site_name_for(sites_info["id-a"]) == "Alpha School"
    # Every directory site id is an Aurora UUID (no `legacy:` ids any more).
    assert sites_info["id-a"]["site_uuid"] == "id-a"
    assert sites_info["id-b"]["site_uuid"] == "id-b"


def test_THE_the_directory_id_wins_over_a_slug_keyed_coordinate_uuid(monkeypatch):
    """site-coords.json is keyed by slug, and slugs are unique per company only. Two
    tenants' `main-site` share one coords entry; its site_id must not become the
    other tenant's site_uuid (programme and weather are read by it)."""
    s3 = FakeS3({directory.KEY: DIRECTORY_DOC,
                 "config/site-coords.json": {"main-site": {"site_id": "id-a",
                                                           "latitude": -43.5, "longitude": 172.6}}})
    monkeypatch.setattr(rg, "s3_client", s3)
    monkeypatch.setattr(rg, "download_json_from_s3",
                        lambda bucket, key: json.loads(s3.objects[key]) if key in s3.objects else None)
    _p, _a, _r, sites_info = rg.get_user_site_mapping("b")
    assert sites_info["id-a"]["site_uuid"] == "id-a"
    assert sites_info["id-b"]["site_uuid"] == "id-b"


def test_the_spaced_display_name_finds_the_folder(monkeypatch):
    s3 = FakeS3({directory.KEY: DIRECTORY_DOC})
    monkeypatch.setattr(rg, "s3_client", s3)
    monkeypatch.setattr(rg, "download_json_from_s3",
                        lambda bucket, key: json.loads(s3.objects[key]) if key in s3.objects else None)
    _p, allsites, roles, _s = rg.get_user_site_mapping("b")
    assert roles.get("Ben Lin") == "pm" and allsites.get("Ben Lin") == ["id-a", "id-b"]
    assert roles.get("Nobody Here", "") == ""


def test_the_weekly_prompt_accepts_the_global_role_vocabulary():
    def has_manager_emphasis(role):
        p = rg.build_weekly_prompt([], "Site", "2026-10-01", "2026-10-07",
                                   scope_label="X", scope_type="user", user_role=role)
        return "supervisory" in p or "manager/PM" in p

    for role in ("site_manager", "pm", "regional_manager", "gm", "admin"):
        assert has_manager_emphasis(role), role
    assert not has_manager_emphasis("worker")
    assert not has_manager_emphasis("")


def test_the_daily_prompt_no_longer_carries_a_device_name_reference():
    import inspect
    assert "name_mapping" not in inspect.signature(rg.build_daily_prompt).parameters


# ---- meeting minutes --------------------------------------------------------------

mm = pytest.importorskip("lambda_meeting_minutes", reason="requires boto3 (installed in CI)")


def _transcript_key(folder):
    return (f"transcripts/{folder}/2026-10-07/x_2026-10-07_09-00-00_sid{'a' * 32}_c0000"
            "_bn4_off0.0_to30.0_srcwav.json")


def _collect(monkeypatch, folder, names):
    key = _transcript_key(folder)
    monkeypatch.setattr(mm, "list_s3_objects",
                        lambda bucket, prefix: [{"key": key}] if key.startswith(prefix) else [])
    monkeypatch.setattr(mm, "load_user_mapping", lambda bucket: names)
    monkeypatch.setattr(mm, "download_json_from_s3", lambda b, k: {
        "results": {"transcripts": [{"transcript": "the slab pour finished"}], "items": []}})
    monkeypatch.setattr(mm.agent_turn_filter, "apply_agent_filter",
                        lambda turns, *a, **k: (turns, None))
    return mm.collect_transcripts("b", "2026-10-07", user_filter=folder)


def test_THE_minutes_speaker_name_comes_from_the_folder(monkeypatch):
    got = _collect(monkeypatch, "Deandre__Alberts", {"Deandre__Alberts": "Deandre Alberts"})
    assert [t["speaker_name"] for t in got] == ["Deandre Alberts"]


def test_an_unknown_folder_keeps_the_device_name_unchanged(monkeypatch):
    got = _collect(monkeypatch, "Somebody_New", {"Deandre__Alberts": "Deandre Alberts"})
    assert got and got[0]["speaker_name"] == got[0]["device"]


def test_a_nameless_person_does_not_override_the_device_name(monkeypatch):
    got = _collect(monkeypatch, "Ben_UCPK_", {})
    assert got and got[0]["speaker_name"] == got[0]["device"]
    assert directory.names_by_folder({"people": {"X": {"name": " "}, "Y": {"name": "Y Y"}}}) == {"Y": "Y Y"}


def test_minutes_names_come_from_the_directory(monkeypatch):
    s3 = FakeS3({directory.KEY: DIRECTORY_DOC})
    monkeypatch.setattr(mm, "s3_client", s3)
    assert mm.load_user_mapping("b") == {"Deandre__Alberts": "Deandre Alberts", "Ben_Lin": "Ben Lin"}


# ---- extract-session --------------------------------------------------------------

es = pytest.importorskip("lambda_extract_session", reason="requires boto3 (installed in CI)")


def test_the_site_match_reads_the_directory_and_is_scoped_to_the_speakers_company(monkeypatch):
    monkeypatch.setattr(es, "s3", lambda: FakeS3({directory.KEY: DIRECTORY_DOC}))
    monkeypatch.setattr(es, "_sites_cache", None)
    # Deandre is company co-1: "Alpha Schooling" belongs to co-2 and is not a candidate.
    assert es._fuzzy_match_site("Alpha Schoo", "Deandre__Alberts") == "Alpha School"
    # A co-2 speaker is matched against co-2's sites only.
    assert es._fuzzy_match_site("Alpha Schooling", "Ben_Lin") == "Alpha Schooling"
    assert es._fuzzy_match_site("Alpha School", "Ben_Lin") == "Alpha Schooling"
    # An unknown speaker: every site, as before.
    # An unknown speaker while the directory carries company ids: NO match.
    assert es._fuzzy_match_site("Alpha Schooling", "nobody") is None
    assert es._fuzzy_match_site("Alpha School", None) is None


def test_THE_the_site_match_is_always_scoped_now_that_the_unscoped_document_is_gone(monkeypatch):
    """Only the frozen-file adapter produced sites with no company id; the published
    directory always stamps one (sites.company_id is NOT NULL). With the adapter gone,
    the unscoped branch would only ever fire on a broken document -- it fails closed."""
    doc = {"version": 2,
           "people": {"Ann_Lee": {"name": "Ann Lee", "company_id": None}},
           "sites": {"id-u": {"slug": "uc-pk", "name": "UC PK", "company_id": None}}}
    monkeypatch.setattr(es, "s3", lambda: FakeS3({directory.KEY: doc, OLD_KEY: LEGACY_DOC}))
    monkeypatch.setattr(es, "_sites_cache", None)
    assert es._fuzzy_match_site("UC PK", "anybody") is None
    assert es._fuzzy_match_site("UC PK", "Ann_Lee") is None


# ---- template ---------------------------------------------------------------------

def _template():
    return open(TEMPLATE, encoding="utf-8").read()


def test_the_template_schedules_the_directory_after_the_coordinates_and_before_reports():
    t = _template()
    block = t[t.index("DirectoryRepublish:"):][:600]
    assert "cron(35 15 * * ? *)" in block
    assert '"task": "republish_directory"' in block
    assert "cron(30 15 * * ? *)" in t[t.index("SiteCoordsRepublish:"):][:500]
    assert "cron(0 16 * * ? *)" in t


def test_org_api_may_read_and_write_exactly_that_one_key():
    t = _template()
    assert t.count("${DataBucketName}/config/directory.json") == 1
    assert "config/directory.json" in t[t.index("- config/site-coords.json"):][:600]   # ListBucket list
    assert "${DataBucketName}/config/*" not in t


def test_the_extract_session_reader_can_read_the_directory():
    t = _template()
    fn = t[t.index("  ExtractSessionFunction:"):][:20000]
    assert "${IngestBucketName}/config/directory.json" in fn


def test_no_env_var_or_grant_is_left_pointing_at_the_mapping_without_a_reader():
    t = _template()
    assert "CONFIG_KEY" not in t
    # M5 removed the directory's fallback, the last reader behind an exact grant; the
    # orchestrator, which read it through a bucket-wide policy, was removed 2026-10-29.
    assert t.count("${IngestBucketName}/config/user_mapping.json") == 0
    assert t.count("${DataBucketName}/config/user_mapping.json") == 0


def test_the_dead_loader_is_gone():
    import lambda_ingest
    assert not hasattr(lambda_ingest, "load_mapping")
    for mod in ("lambda_ingest", "lambda_item_writer", "lambda_embed_report", "lambda_extract_session"):
        assert not hasattr(__import__(mod), "CONFIG_KEY"), mod


# ---- the schedule reaches the republish -------------------------------------------

def test_the_directory_task_is_scheduled_not_routed(monkeypatch):
    import lambda_org_api as api
    called = []

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(api, "get_connection", lambda: Conn())
    monkeypatch.setattr(api, "republish_directory", lambda conn, dry_run=False: called.append(dry_run) or {"ok": 1})
    monkeypatch.setattr(api, "dispatch", lambda conn, event, method, route: {"statusCode": 404})
    assert api.lambda_handler({"task": "republish_directory"}, None) == {"ok": 1}
    api.lambda_handler({"httpMethod": "POST", "path": "/api/org/sites",
                        "body": json.dumps({"task": "republish_directory"})}, None)
    assert called == [False]
