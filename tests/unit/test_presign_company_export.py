"""scripts/presign_company_export.py -- the operator's handover links (spec 7). No AWS here:
the S3 client and the boto3 session are stubs."""
import datetime

import pytest

import scripts.lake_ops_common as common
import scripts.presign_company_export as pe

CO = "11111111-2222-3333-4444-555555555555"
EX = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PREFIX = f"exports/{CO}/{EX}/"
NOW = datetime.datetime(2026, 11, 1, 0, 0, tzinfo=datetime.timezone.utc)
TEST_BUCKET = common.BUCKETS[common.TEST_DATABASE]
PROD_BUCKET = common.BUCKETS[common.PROD_DATABASE]


class FakeS3:
    def __init__(self, pages, region="ap-southeast-2"):
        self.pages = pages
        self.region = region
        self.list_calls = []
        self.presigned = []

    def list_objects_v2(self, **kw):
        self.list_calls.append(kw)
        return self.pages[len(self.list_calls) - 1]

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.presigned.append((op, Params, ExpiresIn))
        return f"https://signed.example/{Params['Key']}?exp={ExpiresIn}"

    def get_bucket_location(self, Bucket):
        return {"LocationConstraint": self.region}


def _obj(key, size=10):
    return {"Key": key, "Size": size}


def _two_pages():
    return [
        {"Contents": [_obj(PREFIX + "part-001.zip", 1000), _obj(PREFIX + "manifest.json", 50)],
         "IsTruncated": True, "NextContinuationToken": "t2"},
        {"Contents": [_obj(PREFIX + "records/sites.jsonl", 7), _obj(PREFIX, 0)],
         "IsTruncated": False},
    ]


def test_presigns_every_object_under_the_prefix_across_pages():
    s3 = FakeS3(_two_pages())
    links = pe.presign_export(s3, TEST_BUCKET, CO, EX, 3)
    assert [l["key"] for l in links] == [PREFIX + "part-001.zip", PREFIX + "manifest.json",
                                        PREFIX + "records/sites.jsonl"]   # directory marker skipped
    assert [l["size"] for l in links] == [1000, 50, 7]
    assert all(l["url"].startswith("https://signed.example/") for l in links)
    assert s3.list_calls == [{"Bucket": TEST_BUCKET, "Prefix": PREFIX},
                             {"Bucket": TEST_BUCKET, "Prefix": PREFIX, "ContinuationToken": "t2"}]
    assert {(op, p["Bucket"], e) for op, p, e in s3.presigned} == {
        ("get_object", TEST_BUCKET, 3 * 86400)}


def test_a_key_outside_the_prefix_is_refused_and_nothing_is_signed():
    s3 = FakeS3([{"Contents": [_obj(PREFIX + "part-001.zip"),
                               _obj(f"exports/{CO}/{EX}-other/part-001.zip")]}])
    with pytest.raises(pe.PresignError, match="outside"):
        pe.presign_export(s3, TEST_BUCKET, CO, EX, 1)
    assert s3.presigned == []


def test_a_key_with_dot_dot_is_refused():
    s3 = FakeS3([{"Contents": [_obj(PREFIX + "../../other-co/x.zip")]}])
    with pytest.raises(pe.PresignError, match="outside"):
        pe.presign_export(s3, TEST_BUCKET, CO, EX, 1)


def test_empty_prefix_is_an_error_not_an_empty_success():
    with pytest.raises(pe.PresignError, match="no objects"):
        pe.presign_export(FakeS3([{"IsTruncated": False}]), TEST_BUCKET, CO, EX, 1)


@pytest.mark.parametrize("days", [0, -1, 8, 30])
def test_days_out_of_bounds_refused(days):
    s3 = FakeS3(_two_pages())
    with pytest.raises(pe.PresignError, match="1..7"):
        pe.presign_export(s3, TEST_BUCKET, CO, EX, days)
    assert s3.list_calls == []


@pytest.mark.parametrize("days", [1, 7])
def test_days_bounds_accepted(days):
    s3 = FakeS3(_two_pages())
    pe.presign_export(s3, TEST_BUCKET, CO, EX, days)
    assert s3.presigned[0][2] == days * 86400


@pytest.mark.parametrize("bad", ["", "not-a-uuid", "../x", CO + "/x"])
def test_bad_uuids_refused(bad):
    with pytest.raises(pe.PresignError, match="UUID"):
        pe.presign_export(FakeS3(_two_pages()), TEST_BUCKET, bad, EX, 1)
    with pytest.raises(pe.PresignError, match="UUID"):
        pe.presign_export(FakeS3(_two_pages()), TEST_BUCKET, CO, bad, 1)


def test_uppercase_uuid_is_canonicalised_to_the_written_prefix():
    s3 = FakeS3(_two_pages())
    pe.presign_export(s3, TEST_BUCKET, CO.upper(), EX.upper(), 1)
    assert s3.list_calls[0]["Prefix"] == PREFIX


@pytest.mark.parametrize("bucket,is_prod", [
    (TEST_BUCKET, False), (PROD_BUCKET, True), ("some-unknown-bucket", True)])
def test_is_prod_bucket_fails_closed(bucket, is_prod):
    assert pe.is_prod_bucket(bucket) is is_prod


class FakeCreds:
    def __init__(self, token):
        self.token = token

    def get_frozen_credentials(self):
        return self


class FakeSession:
    def __init__(self, s3, token=None, creds=True):
        self.s3 = s3
        self.token = token
        self.creds = creds
        self.clients = []

    def get_credentials(self):
        return FakeCreds(self.token) if self.creds else None

    def client(self, name, **kw):
        self.clients.append((name, kw))
        return self.s3


def _run(argv, s3, capsys, token=None, creds=True):
    sessions = []

    def factory(profile):
        sessions.append(profile)
        return FakeSession(s3, token=token, creds=creds)
    rc = pe.main(argv, session_factory=factory, now=NOW)
    out = capsys.readouterr()
    return rc, out.out, out.err, sessions


def test_prod_refused_without_allow_prod(capsys):
    s3 = FakeS3(_two_pages())
    for argv in (["--database", "fieldsight"], ["--bucket", PROD_BUCKET],
                 ["--bucket", "some-unknown-bucket"]):
        rc, out, err, sessions = _run(["--company", CO, "--export-id", EX, "--days", "2",
                                       "--profile", "p"] + argv, s3, capsys)
        assert rc == 2
        assert "--allow-prod" in err
        assert sessions == []          # no AWS session opened at all
    assert s3.list_calls == []


def test_prod_with_allow_prod_runs(capsys):
    s3 = FakeS3(_two_pages())
    rc, out, err, _ = _run(["--company", CO, "--export-id", EX, "--days", "2", "--profile", "p",
                            "--database", "fieldsight", "--allow-prod"], s3, capsys)
    assert rc == 0
    assert s3.list_calls[0]["Bucket"] == PROD_BUCKET


def test_output_lists_key_size_url_and_expiry(capsys):
    s3 = FakeS3(_two_pages())
    rc, out, err, sessions = _run(["--company", CO, "--export-id", EX, "--days", "7",
                                   "--profile", "ops"], s3, capsys)
    assert rc == 0
    assert sessions == ["ops"]
    for key, size in ((PREFIX + "part-001.zip", 1000), (PREFIX + "manifest.json", 50),
                      (PREFIX + "records/sites.jsonl", 7)):
        line = next(l for l in out.splitlines() if key in l)
        assert str(size) in line
        assert f"https://signed.example/{key}?exp={7 * 86400}" in line
    assert "2026-11-08T00:00:00+00:00" in out
    assert "temporary" not in err


def test_client_is_s3v4_in_the_bucket_region(capsys):
    s3 = FakeS3(_two_pages(), region="us-west-2")
    made = []

    def factory(profile):
        sess = FakeSession(s3)
        made.append(sess)
        return sess
    assert pe.main(["--company", CO, "--export-id", EX, "--days", "1", "--profile", "p"],
                   session_factory=factory, now=NOW) == 0
    signing = [kw for name, kw in made[0].clients if kw.get("region_name") == "us-west-2"]
    assert signing and signing[-1]["config"].signature_version == "s3v4"


def test_temporary_credentials_print_a_warning(capsys):
    rc, out, err, _ = _run(["--company", CO, "--export-id", EX, "--days", "7", "--profile", "p"],
                           FakeS3(_two_pages()), capsys, token="session-token")
    assert rc == 0
    assert "temporary" in err


def test_no_credentials_is_an_error(capsys):
    rc, out, err, _ = _run(["--company", CO, "--export-id", EX, "--days", "1", "--profile", "p"],
                           FakeS3(_two_pages()), capsys, creds=False)
    assert rc == 1
    assert "credentials" in err


def test_days_out_of_range_exits_nonzero(capsys):
    rc, out, err, sessions = _run(["--company", CO, "--export-id", EX, "--days", "8",
                                   "--profile", "p"], FakeS3(_two_pages()), capsys)
    assert rc == 1
    assert "1..7" in err


def test_script_is_not_imported_by_any_lambda():
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[2] / "src"
    assert not [p for p in src.rglob("*.py") if "presign_company_export" in p.read_text(
        encoding="utf-8", errors="ignore")]
