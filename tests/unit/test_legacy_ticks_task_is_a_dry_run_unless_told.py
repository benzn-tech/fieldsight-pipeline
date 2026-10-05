"""backfill_legacy_ticks: dry run unless `apply` is exactly true; rows must be a list."""
import lambda_org_api as api


class Conn:
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _wire(monkeypatch):
    calls = []
    monkeypatch.setattr(api, "get_connection", lambda: Conn())
    import legacy_ticks_backfill
    monkeypatch.setattr(legacy_ticks_backfill, "run",
                        lambda conn, rows, apply: calls.append(apply) or {"ok": 1})
    return calls


def test_only_a_literal_true_writes(monkeypatch):
    calls = _wire(monkeypatch)
    for v in ("true", 1, "yes", None):
        api.lambda_handler({"task": "backfill_legacy_ticks", "rows": [], "apply": v}, None)
    api.lambda_handler({"task": "backfill_legacy_ticks", "rows": [], "apply": True}, None)
    assert calls == [False, False, False, False, True]


def test_rows_must_be_a_list(monkeypatch):
    calls = _wire(monkeypatch)
    for rows in (None, "x", {"a": 1}):
        assert "error" in api.lambda_handler(
            {"task": "backfill_legacy_ticks", "rows": rows, "apply": True}, None)
    assert calls == []
