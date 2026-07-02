import pytest

lm = pytest.importorskip("lambda_migrate", reason="requires psycopg (installed in CI)")


def test_handler_returns_applied_list(monkeypatch):
    calls = {}

    def fake_get_connection(dsn=None, autocommit=False):
        calls["autocommit"] = autocommit
        class FakeConn:
            def close(self): pass
        return FakeConn()

    def fake_apply(conn, migrations_dir):
        calls["dir"] = migrations_dir
        return ["0001_extensions.sql", "0002_core_relational.sql"]

    monkeypatch.setattr(lm, "get_connection", fake_get_connection)
    monkeypatch.setattr(lm, "apply_migrations", fake_apply)

    out = lm.lambda_handler({}, None)
    assert out == {"applied": ["0001_extensions.sql", "0002_core_relational.sql"]}
    assert calls["autocommit"] is True
    assert calls["dir"].endswith("migrations")
