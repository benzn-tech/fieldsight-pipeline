import inspect

import pytest
from psycopg.rows import dict_row

from askv2 import adapter
from askv2.adapter import AdapterLog, DataApiConnection
from askv2.render import EvalAdapterError
from fakes import FakeRdsData, col, result, s

KW = dict(cluster="arn:cluster", secret="arn:secret", database="fieldsight_test")
U = "4326dbc7-14a2-4251-8381-9d0067595a85"


def conn_for(client, log=None):
    return DataApiConnection(client, log=log or AdapterLog(), **KW)


def test_source_has_no_path_to_commit():
    assert "commit_transaction" not in inspect.getsource(adapter)


def test_begin_then_read_only_then_show():
    client = FakeRdsData()
    conn = conn_for(client)
    assert [m for m, _ in client.calls] == ["begin_transaction", "execute_statement",
                                            "execute_statement"]
    assert client.statements() == ["SET TRANSACTION READ ONLY", "SHOW transaction_read_only"]
    assert client.calls[0][1] == {"resourceArn": "arn:cluster", "secretArn": "arn:secret",
                                  "database": "fieldsight_test"}
    assert not conn.closed


def test_show_off_aborts_with_rollback():
    client = FakeRdsData(read_only="off")
    with pytest.raises(EvalAdapterError, match="read-only"):
        conn_for(client)
    assert client.calls[-1][0] == "rollback_transaction"


def test_every_statement_carries_the_transaction_and_metadata():
    client = FakeRdsData([(r"FROM users", result([col("id", "uuid")], [[s(U)]]))])
    conn = conn_for(client)
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT id FROM users WHERE cognito_sub=%s", ("sub-1",)).fetchone()
    assert str(row["id"]) == U
    for m, kw in client.calls[1:]:
        assert kw["transactionId"] == "tx-1" and kw["includeResultMetadata"] is True
    assert client.statements()[-1] == "SELECT id FROM users WHERE cognito_sub='sub-1'"


def test_conn_execute_returns_tuples():
    client = FakeRdsData([(r"max\(", result([col("max", "date")], [[s("2026-09-10")]]))])
    row = conn_for(client).execute("SELECT max(t.report_date) FROM topics t WHERE x < %s",
                                   ["2026-10-01"]).fetchone()
    assert row[0].isoformat() == "2026-09-10"


def test_cursor_surface():
    client = FakeRdsData([(r"FROM t", result([col("a", "int4")], [[{"longValue": 1}],
                                                                   [{"longValue": 2}]]))])
    conn = conn_for(client)
    with conn.cursor() as cur:
        cur.execute("SELECT a FROM t")
        assert cur.rowcount == 2
        assert list(cur) == [(1,), (2,)]
    assert conn.cursor().execute("SELECT a FROM t").fetchall() == [(1,), (2,)]
    assert conn.cursor().execute("SELECT a FROM nothing").fetchone() is None


def test_unsupported_row_factory_is_loud():
    with pytest.raises(EvalAdapterError):
        conn_for(FakeRdsData()).cursor(row_factory=lambda cur: None)


def test_savepoints_release_in_order():
    client = FakeRdsData()
    conn = conn_for(client)
    with conn.transaction():
        with conn.transaction():
            conn.execute("SELECT 1")
    assert client.statements()[2:] == ["SAVEPOINT eval_1", "SAVEPOINT eval_2", "SELECT 1",
                                       "RELEASE SAVEPOINT eval_2", "RELEASE SAVEPOINT eval_1"]


def test_error_inside_transaction_rolls_back_to_savepoint_and_reraises():
    client = FakeRdsData()
    log = AdapterLog()
    conn = conn_for(client, log)
    client.fail = RuntimeError("ERROR: column x does not exist")
    with pytest.raises(RuntimeError):
        with conn.transaction():
            conn.execute("SELECT x FROM t")
    assert client.statements()[-2:] == ["SELECT x FROM t", "ROLLBACK TO SAVEPOINT eval_1"]
    assert len(log.errors) == 1 and "RuntimeError" in log.errors[0]


def test_close_rolls_back_once():
    client = FakeRdsData()
    conn = conn_for(client)
    conn.close()
    conn.close()
    assert [m for m, _ in client.calls].count("rollback_transaction") == 1
    assert client.calls[-1][1]["transactionId"] == "tx-1"
    assert conn.closed


def test_templates_are_checked_and_internals_are_not():
    client = FakeRdsData()
    conn = conn_for(client)
    with pytest.raises(EvalAdapterError):
        conn.execute("SET TRANSACTION READ WRITE")
    with pytest.raises(EvalAdapterError):
        conn.execute("SELECT 1 FOR UPDATE")
    # Neither was sent.
    assert client.statements() == ["SET TRANSACTION READ ONLY", "SHOW transaction_read_only"]


def test_keyboard_interrupt_leaves_nothing_open():
    client = FakeRdsData()
    conn_for(client)
    client.fail = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        conn_for(client).execute("SELECT 1")
    adapter.rollback_open()
    rolled = {kw["transactionId"] for m, kw in client.calls if m == "rollback_transaction"}
    assert rolled == {"tx-1", "tx-2"}
    adapter.rollback_open()                  # idempotent
    assert [m for m, _ in client.calls].count("rollback_transaction") == 2


def test_statement_log_and_sql_stage_capture():
    client = FakeRdsData([(r"chunk_text", result(
        [col("id", "uuid"), col("report_date", "date"), col("chunk_text", "text")],
        [[s(U), s("2026-09-02"), s("we poured the slab")]]))])
    log = AdapterLog()
    conn = conn_for(client, log)
    conn.cursor(row_factory=dict_row).execute("SELECT id, report_date, chunk_text FROM c").fetchall()
    conn.execute("SELECT 1 FROM t")
    assert len(log.statements) == 2
    st = log.statements[0]
    assert st["head"].startswith("SELECT id, report_date") and st["rows"] == 1
    assert len(st["sha256"]) == 64 and st["ms"] >= 0 and st["bytes"] > 0
    assert log.sql_rows == [{"id": U, "report_date": "2026-09-02",
                             "chunk_text": "we poured the slab"}]
    assert log.largest_response >= st["bytes"]
