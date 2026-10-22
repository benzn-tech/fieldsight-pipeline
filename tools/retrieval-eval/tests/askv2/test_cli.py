import json
from pathlib import Path

import pytest

import run_ask_v2

GOLD = str(Path(__file__).resolve().parents[2] / "gold" / "v1.1.jsonl")


def test_full_run_needs_yes_and_caps():
    with pytest.raises(SystemExit, match="--yes"):
        run_ask_v2.parse_args(["--gold", GOLD, "--label", "x", "--family", "report"])
    with pytest.raises(SystemExit, match="--max-judge-calls"):
        run_ask_v2.parse_args(["--gold", GOLD, "--label", "x", "--family", "report", "--yes"])
    a = run_ask_v2.parse_args(["--gold", GOLD, "--label", "x", "--family", "report", "--yes",
                               "--max-judge-calls", "400", "--max-tokens", "4000000"])
    assert a.scope_site is True and a.repeat == 2 and a.database == "fieldsight"
    assert a.prefix == "fieldsight-prod" and a.src_ref == "origin/main"


def test_pilot_and_preflight_need_no_yes():
    a = run_ask_v2.parse_args(["--gold", GOLD, "--label", "p", "--family", "session", "--pilot",
                               "--database", "fieldsight_test", "--no-scope-site"])
    assert a.pilot and a.prefix == "fieldsight-test" and a.scope_site is False
    run_ask_v2.parse_args(["--gold", GOLD, "--label", "p", "--family", "report",
                           "--preflight-only"])


def test_family_is_required():
    with pytest.raises(SystemExit):
        run_ask_v2.parse_args(["--gold", GOLD, "--label", "x", "--pilot"])


def test_pilot_questions_are_two_answerable_and_one_control():
    items = run_ask_v2.select_items(GOLD, pilot=True)
    assert [i["id"] for i in items] == ["v1-01", "v1-03", "v1-20"]
    assert [i["expect"] for i in items] == ["answerable", "answerable", "unanswerable"]
    assert len(run_ask_v2.select_items(GOLD, pilot=False)) == 45


def test_result_name_carries_family_and_now():
    name = run_ask_v2.result_name("20261023T010203Z", "pilot", "report", "2026-10-23T14:00:00+13:00")
    assert name == "20261023T010203Z-askv2-pilot-report-now20261023.json"


def test_diff_mode_reads_two_files_offline(tmp_path, capsys):
    rec = {"fingerprint": {"model_chain": ["m"]}, "runs": [{"turns": []}]}
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(rec)), b.write_text(json.dumps(rec))
    run_ask_v2.main(["--diff", str(a), str(b)])
    assert json.loads(capsys.readouterr().out) == {"seen": {}, "cited": {}}


# --- step 0 end to end, offline -------------------------------------------------

def test_preflight_only_runs_step0_with_fakes_and_stops(monkeypatch, capsys, tmp_path):
    import boto3

    from askv2 import harness
    from fakes import FakeRdsData, col, result, s
    from test_env import FakeLambdaApi
    from test_preflight import FakeIam, FakeSts, USER, COMPANY, SITE, ROW

    class Rds(FakeRdsData):
        def execute_statement(self, **kw):
            if kw["sql"].startswith("CREATE TEMP TABLE"):
                self.calls.append(("execute_statement", kw))
                raise RuntimeError("ERROR: read-only transaction; SQLState: 25006")
            return super().execute_statement(**kw)

    rds = Rds([
        (r"information_schema\.columns", result(
            [col("table_name", "name"), col("column_name", "name")],
            [[s("topics"), s("superseded_at")], [s("memberships"), s("external")],
             [s("redactions"), s("target_key")], [s("redactions"), s("reverted_at")],
             [s("project_documents"), s("id")], [s("project_doc_chunks"), s("id")]])),
        (r"FROM users WHERE id=", result([col("cognito_sub", "text"), col("global_role", "text")],
                                         [[s("sub-ben"), s("gm")]])),
        (r"WHERE users\.cognito_sub=", result(
            [col("id", "uuid"), col("company_id", "uuid"), col("global_role", "text"),
             col("folder_name", "text")], [[s(USER), s(COMPANY), s("gm"), s("Ben")]])),
        (r"FROM sites WHERE company_id=", result([col("id", "uuid")], [[s(SITE)]])),
        (r"lexical_hit|vec", result([col("id", "text"), col("distance", "float8")], ROW)),
    ])
    lam = FakeLambdaApi()
    lam.by_name = {k.replace("-prod-", "-test-"): v for k, v in lam.by_name.items()}
    clients = {"iam": FakeIam(), "sts": FakeSts(), "lambda": lam, "rds-data": rds}

    class Session:
        region_name = "ap-southeast-2"

        def __init__(self, **kw):
            pass

        def client(self, name, **kw):
            return clients[name]

    monkeypatch.setattr(boto3.session, "Session", Session)
    monkeypatch.setattr(harness, "freeze_session", lambda session: session)
    monkeypatch.setattr(harness, "prepare_imports", lambda ref, family, env: ("src", "sha"))
    monkeypatch.setattr(harness, "arm_boto_guard", lambda allowed: harness._Guard(lambda: None))
    run_ask_v2.main(["--gold", GOLD, "--label", "pf", "--family", "report",
                     "--database", "fieldsight_test", "--preflight-only", "--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert "read-only probe: refused with 25006" in out
    assert "schema preflight: ok" in out and "binding parity" in out
    assert "--preflight-only: stopping before any question" in out
    assert "sub-ben" not in out and "fake-judge-key" not in out and "fake-ds-key" not in out
    assert clients["lambda"].asked == ["fieldsight-test-ask-voice-worker",
                                       "fieldsight-test-rag-search"]
    assert list(tmp_path.iterdir()) == []          # step 0 writes no result file
    methods = [m for m, _ in rds.calls]
    assert methods.count("begin_transaction") == methods.count("rollback_transaction")


def test_keep_rows_must_not_point_into_results():
    base = ["--gold", GOLD, "--label", "p", "--family", "report", "--pilot"]
    results = Path(run_ask_v2.HERE) / "results" / "rows"
    with pytest.raises(SystemExit, match="--keep-rows must not point into results/"):
        run_ask_v2.parse_args(base + ["--keep-rows", str(results)])
    assert run_ask_v2.parse_args(base + ["--keep-rows", "/tmp/x"]).keep_rows == "/tmp/x"
