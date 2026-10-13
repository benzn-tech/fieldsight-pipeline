"""index_backfill event: count (dry run, the default) or emit index requests."""
import pytest

mod = pytest.importorskip("lambda_ingest", reason="requires psycopg (installed in CI)")

ROWS = [
    {"source_s3_key": "extractions/Ben_X/2026-07-06/Benl1_2026-07-06_10-00-00.json",
     "site_id": "s1", "user_id": "u1", "report_date": "2026-07-06"},
    {"source_s3_key": "extractions/Ben_X/2026-07-06/grpabc.json",
     "site_id": "s1", "user_id": "u1", "report_date": "2026-07-06"},
]


class FakeConn:
    def __init__(self):
        self.sql = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self, row_factory=None):
        conn = self

        class C:
            def execute(self, sql, params=None):
                conn.sql.append((sql, params))
                return self

            def fetchall(self):
                return ROWS

            def fetchone(self):
                return {"company_id": "c1"}
        return C()


@pytest.fixture
def wired(monkeypatch):
    conn = FakeConn()
    emitted = []
    monkeypatch.setattr(mod, "get_connection", lambda: conn)
    monkeypatch.setattr(mod, "s3", lambda: "S3")
    monkeypatch.setattr(mod.index_request, "build",
                        lambda c, key, **kw: {"extraction_key": key, **kw})
    monkeypatch.setattr(mod.index_request, "emit",
                        lambda s3, bucket, req: emitted.append(req) or "k")
    return conn, emitted


def test_dry_run_is_default_and_emits_nothing(wired):
    conn, emitted = wired
    out = mod.lambda_handler({"index_backfill": {}}, None)
    assert out["keys"] == 2 and out["emitted"] == 0 and out["dry_run"] is True
    assert emitted == []


def test_real_run_emits_per_solo_key_with_company_from_site(wired):
    conn, emitted = wired
    out = mod.lambda_handler({"index_backfill": {"dry_run": False}}, None)
    assert out["emitted"] == 1 and out["skipped_groups"] == 1
    assert emitted[0]["owner_company_id"] == "c1"
    assert emitted[0]["session_base"] == "Benl1_2026-07-06_10-00-00"


def test_limit_is_capped_and_filters_bind(wired):
    conn, _ = wired
    mod.lambda_handler({"index_backfill": {"limit": 10**6, "user_folder": "Ben_X",
                                           "date_from": "2026-07-01"}}, None)
    sql, params = conn.sql[0]
    assert "superseded_at IS NULL" in sql
    assert params[-1] == 2000 and "2026-07-01" in params
    assert any(str(p).startswith("extractions/Ben") for p in params)
