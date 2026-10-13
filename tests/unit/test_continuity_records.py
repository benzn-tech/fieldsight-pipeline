"""
Tests for src/continuity_records.py -- Task 7 (spec 2026-09-30-extractor-declares-item-continuity
D6): every continuity claim in the extraction artifact becomes a decision_records row.

Style mirrors tests/unit/test_decision_records_written_for_rejected_verdicts.py: a FakeConn +
monkeypatch on decision_records.insert. continuity_records issues its own SQL directly (no
repository seam for `_already`/`_stable_id_for`), so FakeConn also models those two query
shapes rather than only opening/closing a connection.
"""
import pytest

cr = pytest.importorskip("continuity_records", reason="requires psycopg (installed in CI)")

_PRIOR_ID = "11111111-1111-1111-1111-111111111111"
_NEW_ID = "22222222-2222-2222-2222-222222222222"


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    """`stable_ids` maps item_id (str) -> stable_id, standing in for a real
    `SELECT stable_id FROM <table> WHERE item_id=%s AND topic_id = ANY(%s)`. `already` is the
    set of (alias, new_item_id) pairs a prior call has already recorded, standing in for the
    decision_records dedupe SELECT. Both are the only two query shapes continuity_records
    issues -- topic_id scoping itself is proved against a real table in the integration test."""

    def __init__(self, stable_ids=None, already=None):
        self.stable_ids = stable_ids or {}
        self.already = already or set()
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "FROM decision_records" in sql:
            _key, _extracted_at, alias, new_item_id = params
            return _FakeCursor([(1,)] if (alias, new_item_id) in self.already else [])
        item_id, _topic_ids = params
        stable = self.stable_ids.get(str(item_id)) if item_id is not None else None
        return _FakeCursor([(stable,)] if stable is not None else [])


def _extraction(**overrides):
    e = dict(
        extracted_at="2026-09-30T10:00:00Z",
        llm_provider="anthropic", llm_model="claude-sonnet-4-6",
        continuity={"question_set": "item_continuity:abc123", "claims": []},
    )
    e.update(overrides)
    return e


def _claim(**overrides):
    c = dict(alias="A1", prior_item_id=_PRIOR_ID, new_item_id=_NEW_ID,
             outcome="accepted", guard=None, list_name="action_items")
    c.update(overrides)
    return c


def _with_claims(*claims, question_set="item_continuity:abc123"):
    return _extraction(continuity={"question_set": question_set, "claims": list(claims)})


# ---------------------------------------------------------------------------
# 1. Accepted claim -> one insert, subject on the RETIRED row, no text anywhere.
# ---------------------------------------------------------------------------

def test_accepted_claim_becomes_one_decision_record(monkeypatch):
    captured = []
    monkeypatch.setattr(cr.decision_records, "insert",
                        lambda conn, **kw: captured.append(kw) or {"id": "dr-1"})
    claim = _claim()
    conn = FakeConn(stable_ids={_PRIOR_ID: "stable-old"})

    stats = cr.record_claims(conn, _with_claims(claim), "extractions/x.json", "co-1", "site-1",
                             ["topic-old"], ["topic-new"])

    assert stats == {"inserted": 1, "skipped_duplicate": 0, "unresolved": 0}
    assert len(captured) == 1
    kw = captured[0]
    assert kw["kind"] == "item_continuity"
    assert kw["subject_type"] == "action_item"
    assert kw["subject_stable_id"] == "stable-old"
    assert kw["object_ref"] == "A1"
    assert kw["auto_outcome"] == "accepted"
    assert set(kw["output"]) == {"prior_item_id", "new_item_id", "outcome", "guard"}
    assert kw["output"] == {"prior_item_id": _PRIOR_ID, "new_item_id": _NEW_ID,
                            "outcome": "accepted", "guard": None}
    assert kw["question_set"] == "item_continuity:abc123"
    assert kw["input_key"] == "extractions/x.json"
    assert kw["input_hash"] == "2026-09-30T10:00:00Z"
    assert kw["provider"] == "anthropic"
    assert kw["model"] == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# 2. Rejected claim (guard="echo") -> subject/subject_type come from the NEW row.
# ---------------------------------------------------------------------------

def test_rejected_claim_subject_is_the_new_rows_kind_and_stable_id(monkeypatch):
    captured = []
    monkeypatch.setattr(cr.decision_records, "insert",
                        lambda conn, **kw: captured.append(kw) or {"id": "dr-1"})
    claim = _claim(outcome="rejected", guard="echo")
    conn = FakeConn(stable_ids={_NEW_ID: "stable-new"})

    stats = cr.record_claims(conn, _with_claims(claim), "extractions/x.json", "co-1", "site-1",
                             ["topic-old"], ["topic-new"])

    assert stats["inserted"] == 1
    kw = captured[0]
    assert kw["auto_outcome"] == "rejected"
    assert kw["output"]["guard"] == "echo"
    assert kw["subject_stable_id"] == "stable-new"
    assert kw["subject_type"] == "action_item"


# ---------------------------------------------------------------------------
# 3. Re-delivery -- same claim already recorded -> skipped, not inserted twice.
# ---------------------------------------------------------------------------

def test_redelivery_of_the_same_claim_is_not_inserted_twice(monkeypatch):
    def _boom(conn, **kw):
        raise AssertionError("insert must not be called for a re-delivered claim")
    monkeypatch.setattr(cr.decision_records, "insert", _boom)
    claim = _claim()
    conn = FakeConn(stable_ids={_PRIOR_ID: "stable-old"},
                    already={("A1", _NEW_ID)})

    stats = cr.record_claims(conn, _with_claims(claim), "extractions/x.json", "co-1", "site-1",
                             ["topic-old"], ["topic-new"])

    assert stats == {"inserted": 0, "skipped_duplicate": 1, "unresolved": 0}


# ---------------------------------------------------------------------------
# 4. Double claim -- two records, same alias, different new_item_id -> two inserts.
# item_continuity's one_to_one guard rejects both halves of a real double claim, so both
# arrive here with outcome="rejected" and resolve via their own new row.
# ---------------------------------------------------------------------------

def test_double_claim_same_alias_different_new_item_id_both_recorded(monkeypatch):
    captured = []
    monkeypatch.setattr(cr.decision_records, "insert",
                        lambda conn, **kw: captured.append(kw) or {"id": "dr-1"})
    id_a, id_b = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    claim_a = _claim(new_item_id=id_a, outcome="rejected", guard="one_to_one")
    claim_b = _claim(new_item_id=id_b, outcome="rejected", guard="one_to_one")
    conn = FakeConn(stable_ids={id_a: "stable-a", id_b: "stable-b"})

    stats = cr.record_claims(conn, _with_claims(claim_a, claim_b), "extractions/x.json",
                             "co-1", "site-1", ["topic-old"], ["topic-new"])

    assert stats["inserted"] == 2
    assert {kw["output"]["new_item_id"] for kw in captured} == {id_a, id_b}
    assert {kw["subject_stable_id"] for kw in captured} == {"stable-a", "stable-b"}


# ---------------------------------------------------------------------------
# 5. No continuity key -> no queries at all.
# ---------------------------------------------------------------------------

def test_no_continuity_key_issues_no_queries(monkeypatch):
    def _boom(conn, **kw):
        raise AssertionError("insert must not be called when there is no continuity block")
    monkeypatch.setattr(cr.decision_records, "insert", _boom)
    conn = FakeConn()
    extraction = {"extracted_at": "2026-09-30T10:00:00Z", "llm_provider": "anthropic"}

    stats = cr.record_claims(conn, extraction, "extractions/x.json", "co-1", "site-1",
                             ["topic-old"], ["topic-new"])

    assert stats == {"inserted": 0, "skipped_duplicate": 0, "unresolved": 0}
    assert conn.executed == []


# ---------------------------------------------------------------------------
# Extra coverage: D9 (stale prior recorded, not prevented) and the unresolved count.
# ---------------------------------------------------------------------------

def test_accepted_claim_whose_prior_row_is_gone_falls_back_to_the_new_row(monkeypatch):
    """The retired row's stable_id cannot be found (a wider pass superseded it again between
    read and write -- D9) -- the claim still gets a record, keyed on the new row instead."""
    captured = []
    monkeypatch.setattr(cr.decision_records, "insert",
                        lambda conn, **kw: captured.append(kw) or {"id": "dr-1"})
    claim = _claim()
    conn = FakeConn(stable_ids={_NEW_ID: "stable-new"})  # no entry for _PRIOR_ID

    stats = cr.record_claims(conn, _with_claims(claim), "extractions/x.json", "co-1", "site-1",
                             ["topic-old"], ["topic-new"])

    assert stats == {"inserted": 1, "skipped_duplicate": 0, "unresolved": 0}
    assert captured[0]["subject_stable_id"] == "stable-new"
    assert captured[0]["auto_outcome"] == "accepted"


def test_claim_resolving_to_neither_pool_is_unresolved_not_inserted(monkeypatch):
    def _boom(conn, **kw):
        raise AssertionError("insert must not be called for an unresolvable claim")
    monkeypatch.setattr(cr.decision_records, "insert", _boom)
    claim = _claim(outcome="rejected", guard="existence")
    conn = FakeConn()  # neither id resolves to a stable_id

    stats = cr.record_claims(conn, _with_claims(claim), "extractions/x.json", "co-1", "site-1",
                             ["topic-old"], ["topic-new"])

    assert stats == {"inserted": 0, "skipped_duplicate": 0, "unresolved": 1}


# ---------------------------------------------------------------------------
# _TABLE must never drift from item_continuity.KINDS (both list every kind the extractor
# can claim continuity for; _TABLE additionally maps each one to the table that carries its
# stable_id, so it can't just BE KINDS, but its key set must always equal KINDS's).
# ---------------------------------------------------------------------------

def test_table_kinds_match_item_continuity_kinds():
    assert set(cr._TABLE) == set(cr.item_continuity.KINDS)
