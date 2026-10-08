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

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "..", "src", "template.yaml")

DIRECTORY_DOC = {
    "version": 1, "published_at": "2026-10-08T00:00:00+00:00",
    "people": {
        "Deandre__Alberts": {"name": "Deandre Alberts", "role": "gm", "company_id": "co-1",
                             "primary_site": "alpha-school", "sites": ["alpha-school"]},
        "Ben_Lin": {"name": "Ben Lin", "role": "pm", "company_id": "co-2",
                    "primary_site": None, "sites": ["a", "b"]},
    },
    "sites": {
        "alpha-school": {"name": "Alpha School", "location": "Chch", "client": "MoE",
                         "company_id": "co-1", "site_id": "uuid-a"},
        "other-tenant": {"name": "Alpha Schooling", "company_id": "co-2"},
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
    s3 = FakeS3({directory.KEY: DIRECTORY_DOC, directory.LEGACY_KEY: LEGACY_DOC})
    doc = directory.load(s3, "b")
    assert doc["people"]["Deandre__Alberts"]["role"] == "gm"
    assert s3.gets == [directory.KEY]


def test_load_adapts_the_old_file_and_warns_once(caplog):
    s3 = FakeS3({directory.LEGACY_KEY: LEGACY_DOC})
    with caplog.at_level(logging.WARNING):
        doc = directory.load(s3, "b")
        directory.load(s3, "b")      # cached: no second read, no second warning
    ben = doc["people"]["Ben_Lin"]
    assert ben["name"] == "Ben Lin" and ben["role"] == "site_manager"
    assert ben["primary_site"] == "uc-pk" and ben["sites"] == ["uc-pk"]
    assert doc["sites"]["uc-pk"]["name"] == "UC PK"
    assert "Benl1" not in doc["people"]          # device ids are not carried over
    warnings = [r for r in caplog.records if "falling back to user_mapping.json" in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING


def test_THE_both_missing_is_an_error_not_a_silent_empty(caplog):
    with caplog.at_level(logging.ERROR):
        doc = directory.load(FakeS3(), "b")
    assert doc["people"] == {} and doc["sites"] == {}
    errs = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errs and "neither" in errs[0].getMessage()


def test_a_403_on_a_missing_key_reads_as_absent_not_as_a_crash():
    class Denied(FakeS3):
        def get_object(self, Bucket, Key):
            if Key == directory.KEY:
                raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")
            return super().get_object(Bucket, Key)

    doc = directory.load(Denied({directory.LEGACY_KEY: LEGACY_DOC}), "b")
    assert "Ben_Lin" in doc["people"]


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
    assert primary["Deandre__Alberts"] == "alpha-school"
    assert allsites["Ben_Lin"] == ["a", "b"] and "Ben_Lin" not in primary
    assert sites_info["alpha-school"]["name"] == "Alpha School"
    assert rg.site_name_for(sites_info["alpha-school"]) == "Alpha School"


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
    assert es._fuzzy_match_site("Alpha Schooling", "nobody") == "Alpha Schooling"


# ---- orchestrator -----------------------------------------------------------------

orch = pytest.importorskip("lambda_orchestrator", reason="requires boto3 (installed in CI)")


@pytest.fixture
def orch_s3(monkeypatch):
    monkeypatch.setattr(orch, "_user_mapping", None)

    def make(objects):
        monkeypatch.setattr(orch, "s3_client", FakeS3(objects))
    yield make
    orch._user_mapping = None


def test_THE_the_orchestrator_refuses_to_file_under_a_device_folder(orch_s3):
    orch_s3({})                                   # no mapping at all
    with pytest.raises(orch.MappingUnavailable):
        orch.get_display_name("Benl1", "b")
    info = {"type": "audio", "user_name": "Benl1", "time": "2026-10-07 09:00:00"}
    with pytest.raises(orch.MappingUnavailable):
        orch.generate_s3_key(info, "b")


def test_an_empty_mapping_is_unavailable_too(orch_s3):
    orch_s3({"config/user_mapping.json": {"mapping": {}}})
    with pytest.raises(orch.MappingUnavailable):
        orch.load_user_mapping("b")


def test_a_device_nobody_owns_is_skipped_not_filed_under_its_id(orch_s3):
    orch_s3({"config/user_mapping.json": LEGACY_DOC})
    with pytest.raises(orch.UnmappedDevice):
        orch.get_display_name("Stranger9", "b")
    stats = {"total_found": 0, "already_exists": 0}
    orch.process_file({"type": "audio", "user_name": "Stranger9",
                       "time": "2026-10-07 09:00:00", "download_url": "u"},
                      stats, {"s3_bucket": "b"})
    assert stats["unmapped_device"] == 1
    assert orch.get_display_name("Benl1", "b") == "Ben Lin"


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
    # Only extract-session (via the directory's fallback) still has an exact grant.
    assert t.count("${IngestBucketName}/config/user_mapping.json") == 1


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
    monkeypatch.setattr(api, "republish_directory", lambda conn: called.append(1) or {"ok": 1})
    monkeypatch.setattr(api, "dispatch", lambda conn, event, method, route: {"statusCode": 404})
    assert api.lambda_handler({"task": "republish_directory"}, None) == {"ok": 1}
    api.lambda_handler({"httpMethod": "POST", "path": "/api/org/sites",
                        "body": json.dumps({"task": "republish_directory"})}, None)
    assert called == [1]
