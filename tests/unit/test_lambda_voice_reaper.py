import pytest

rp = pytest.importorskip("lambda_voice_reaper", reason="requires psycopg import path")


class _FakeConn:
    def __enter__(self): return self
    def __exit__(self, *a): return False


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(rp, "get_connection", lambda *a, **k: _FakeConn())
    calls = {}
    monkeypatch.setattr(rp.ws_connections, "delete_connections",
                        lambda c, ids: calls.update(del_ids=ids) or len(ids))
    monkeypatch.setattr(rp.ws_connections, "delete_stale",
                        lambda c, cutoff: calls.update(stale_cutoff=cutoff) or 3)
    monkeypatch.setattr(rp.voice_messages, "prune_older_than",
                        lambda c, cutoff: calls.update(prune_cutoff=cutoff) or 5)
    return monkeypatch, calls


def test_targeted_delete(wired):
    mp, calls = wired
    res = rp.lambda_handler({"connectionIds": ["a", "b"]}, None)
    assert res == {"deleted": 2} and calls["del_ids"] == ["a", "b"]
    assert "stale_cutoff" not in calls   # targeted mode never sweeps


def test_sweep_mode(wired):
    mp, calls = wired
    res = rp.lambda_handler({"sweep": True}, None)
    assert res == {"swept_connections": 3, "pruned_messages": 5}
    assert calls["stale_cutoff"] is not None and calls["prune_cutoff"] is not None


def test_empty_targeted(wired):
    mp, calls = wired
    res = rp.lambda_handler({"connectionIds": []}, None)
    assert res == {"deleted": 0}
