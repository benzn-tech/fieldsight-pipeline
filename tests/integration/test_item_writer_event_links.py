"""Integration: lambda_item_writer writes supported_by links after carry-forward and a
derived claim_type on every supported child (Track C Task 7), against a real Postgres.

Same committed-connection harness as test_carry_forward_pass0.py: real passes through
write_extraction_items, a fake S3 holding the synthetic artifact. The `support` objects are
put on the children directly (what item_support attaches on the final tier), so nothing here
depends on EMIT_ITEM_SUPPORT or its thresholds. All text is synthetic.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import io
import json
import logging
import uuid

import pytest

from db.connection import get_connection
from repositories import companies, memberships, sites, users

pytestmark = pytest.mark.integration

DATE = "2026-11-02"

lambda_item_writer = pytest.importorskip(
    "lambda_item_writer", reason="requires psycopg (installed in CI)")
claim_type_store = pytest.importorskip("repositories.claim_type_store")


class _FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        body = self.objects[Key]
        raw = body.encode("utf-8") if isinstance(body, str) else body
        return {"Body": io.BytesIO(raw)}

    def get_paginator(self, op):
        assert op == "list_objects_v2"
        return _FakePaginator(self.objects)


class _FakePaginator:
    def __init__(self, objects):
        self.objects = objects

    def paginate(self, Bucket, Prefix):
        yield {"Contents": [{"Key": k} for k in self.objects if k.startswith(Prefix)]}


STEM = "synthetic-chunk-a"


def _linked(start, score=0.9):
    return {"status": "linked", "source_stem": STEM, "start_sec": start,
            "end_sec": start + 4.0, "speaker_label": "spk_0", "quote_status": None,
            "score": score, "candidate_path": "window", "rule_version": "lexical-test"}


def _artifact(tier, extracted_at, *, action_support=None, finding_support=None,
              decision_support=None, question_support=None, action_text="Synthetic task one",
              extra_actions=()):
    def child(field, text, support):
        c = {field: text}
        if support is not None:
            c["support"] = support
        return c
    actions = [child("action", action_text, action_support)]
    actions += [child("action", text, sup) for text, sup in extra_actions]
    return {
        "schema_version": 1, "tier": tier, "extracted_at": extracted_at,
        "topics": [{
            "topic_title": "Synthetic topic", "category": "progress", "summary": "s",
            "time_range": "10:00 – 10:05", "participants": [], "safety_flags": [],
            "action_items": actions,
            "findings": [child("observation", "Synthetic finding one", finding_support)],
            # A blank decision first: the insert skips it, and pairing must still line up.
            "decisions": [{"decision": ""},
                          child("decision", "Synthetic decision one", decision_support)],
            "questions": [child("question", "Synthetic question one", question_support)],
        }],
    }


@pytest.fixture
def world(monkeypatch, migrated_db_url):
    tag = uuid.uuid4().hex[:8]
    seed = get_connection(migrated_db_url, autocommit=True)
    made = {}
    extra_companies = []
    try:
        made["co"] = companies.create_company(seed, f"Links-W-Co-{tag}")
        made["site"] = sites.create_site(seed, made["co"]["id"], f"Links-W-Site-{tag}")
        folder = f"LinksW-{tag}"
        made["user"] = users.upsert_field_only_user(seed, made["co"]["id"], folder,
                                                    "Fol", "Der", "worker")
        made["other"] = users.upsert_field_only_user(seed, made["co"]["id"], f"{folder}-o",
                                                     "Oth", "Er", "worker")
        memberships.add_membership(seed, made["user"]["id"], made["site"]["id"], "worker")
        session_base = "sid" + uuid.uuid4().hex
        key = f"extractions/{folder}/{DATE}/{session_base}.json"
        fake_s3 = _FakeS3({})
        monkeypatch.setattr(lambda_item_writer, "_s3_client", fake_s3)
        monkeypatch.setattr(lambda_item_writer, "get_connection",
                            lambda *a, **k: get_connection(migrated_db_url))
        monkeypatch.setattr(lambda_item_writer.match_request, "emit", lambda *a, **k: None)

        def write(artifact):
            fake_s3.objects[key] = json.dumps(artifact)
            return lambda_item_writer.write_extraction_items(DATE, folder, key)

        yield {"seed": seed, "key": key, "session_base": session_base, "write": write,
               "folder": folder, "url": migrated_db_url, "cleanup_companies": extra_companies,
               **made}
    finally:
        if "site" in made:
            seed.execute("DELETE FROM sites WHERE id=%s", (made["site"]["id"],))
        for u in ("user", "other"):
            if u in made:
                seed.execute("DELETE FROM users WHERE id=%s", (made[u]["id"],))
        if "co" in made:
            for cid in extra_companies:
                seed.execute("DELETE FROM companies WHERE id=%s", (cid,))
            seed.execute("DELETE FROM companies WHERE id=%s", (made["co"]["id"],))
            assert seed.execute(
                "SELECT (SELECT count(*) FROM sites WHERE id=%s), "
                "(SELECT count(*) FROM event_links WHERE site_id=%s), "
                "(SELECT count(*) FROM speaker_turn_names WHERE company_id=%s)",
                (made["site"]["id"], made["site"]["id"], made["co"]["id"])).fetchone() \
                == (0, 0, 0)
        seed.close()


_TEXT = {"action_items": "text", "findings": "observation",
         "topic_decisions": "decision", "topic_questions": "question"}


def _live(w, table, text):
    """The live row of `table` on this pass's key with `text`: id, stable_id, claims."""
    return w["seed"].execute(
        f"SELECT c.id, c.stable_id, c.claim_type, c.claim_basis FROM {table} c "
        f"JOIN topics t ON t.id = c.topic_id WHERE t.source_s3_key = %s "
        f"AND t.superseded_at IS NULL AND c.{_TEXT[table]} = %s", (w["key"], text)).fetchone()


def _links(w):
    return w["seed"].execute(
        "SELECT src_kind, src_stable_id, src_action_item_id, src_finding_id, src_decision_id, "
        "src_question_id, dst_turn, score, rule_version, source_s3_key, id "
        "FROM event_links WHERE site_id = %s ORDER BY created_at, id",
        (w["site"]["id"],)).fetchall()


def test_linked_children_get_supported_by_rows(world, capsys):
    w = world
    res = w["write"](_artifact(
        "final", "2026-11-02T10:30:00Z", action_support=_linked(10.0),
        finding_support=_linked(20.0), decision_support=_linked(30.0, score=0.85),
        question_support={"status": "ambiguous", "best": 0.8, "second": 0.79,
                          "rule_version": "lexical-test"}))
    assert res == {"skipped": False, "topics": 1}, res
    a = _live(w, "action_items", "Synthetic task one")
    f = _live(w, "findings", "Synthetic finding one")
    d = _live(w, "topic_decisions", "Synthetic decision one")
    q = _live(w, "topic_questions", "Synthetic question one")
    links = {r[0]: r for r in _links(w)}
    assert set(links) == {"action_item", "finding", "decision"}
    for kind, row, fk_index in (("action_item", a, 2), ("finding", f, 3), ("decision", d, 4)):
        link = links[kind]
        assert link[fk_index] == row[0], kind
        assert link[1] == row[1], kind
        assert link[9] == w["key"] and link[8] == "lexical-test"
        assert set(link[6]) == {"source_stem", "start_sec", "end_sec", "speaker_label",
                                "quote_status"}
        assert link[6]["source_stem"] == STEM
        # No names exist at write time: unknown, but the basis records the link.
        assert row[2] is None
        assert row[3] == {"rule_version": "claim_v1", "support_status": "linked",
                          "link_id": str(link[10])}
    assert links["decision"][6]["start_sec"] == 30.0
    assert links["decision"][7] == pytest.approx(0.85)
    assert q[2] is None and q[3] == {"rule_version": "claim_v1", "support_status": "ambiguous"}
    emf = [json.loads(line) for line in capsys.readouterr().out.splitlines()
           if '"SupportLinked"' in line]
    assert len(emf) == 1
    assert (emf[0]["SupportLinked"], emf[0]["SupportAmbiguous"], emf[0]["LinksWritten"],
            emf[0]["ClaimNull"], emf[0]["ItemSupportWriteFailed"]) == (3, 1, 3, 4, 0)


def test_link_records_post_carry_stable_id(world):
    """Two passes of one key, the second carried forward by exact text: the second pass's
    link names the NEW row and carries the stable_id carry-forward gave it."""
    w = world
    w["write"](_artifact("live", "2026-11-02T10:00:00Z", action_support=_linked(10.0)))
    old = _live(w, "action_items", "Synthetic task one")
    w["write"](_artifact("final", "2026-11-02T10:30:00Z", action_support=_linked(10.5)))
    new = _live(w, "action_items", "Synthetic task one")
    assert new[0] != old[0]
    assert new[1] == old[1], "carry-forward must have carried the stable_id"
    rows = [r for r in _links(w) if r[2] == new[0]]
    assert len(rows) == 1
    assert rows[0][1] == old[1]
    assert rows[0][6]["start_sec"] == 10.5


def test_none_close_child_written_inferred_without_link(world):
    w = world
    w["write"](_artifact("final", "2026-11-02T10:30:00Z",
                         finding_support={"status": "none_close", "best": 0.05,
                                          "rule_version": "lexical-test"}))
    f = _live(w, "findings", "Synthetic finding one")
    assert f[2] == "inferred"
    assert f[3] == {"rule_version": "claim_v1", "support_status": "none_close"}
    assert _links(w) == []
    # the children with no support key are untouched
    assert _live(w, "action_items", "Synthetic task one")[2:] == (None, None)


def test_no_support_key_writes_nothing(world, capsys):
    w = world
    w["write"](_artifact("final", "2026-11-02T10:30:00Z"))
    assert _links(w) == []
    for table, text in (("action_items", "Synthetic task one"),
                        ("findings", "Synthetic finding one"),
                        ("topic_decisions", "Synthetic decision one"),
                        ("topic_questions", "Synthetic question one")):
        assert _live(w, table, text)[2:] == (None, None), table
    assert '"SupportLinked"' not in capsys.readouterr().out


def _voiceprint(seed, co_id, user_id):
    return seed.execute(
        "INSERT INTO speaker_voiceprints (company_id, user_id, display_name, status) "
        "VALUES (%s, %s, 'Synthetic Person', 'confirmed') RETURNING id",
        (co_id, user_id)).fetchone()[0]


def _name(seed, co_id, vp, session_base, start, state="confirmed"):
    seed.execute(
        "INSERT INTO speaker_turn_names (company_id, voiceprint_id, session_base, turn_ref, "
        " state, display_name, source) VALUES (%s, %s, %s, %s, %s, 'Synthetic Person', "
        " 'correction')",
        (co_id, vp, session_base, f"{STEM}@{float(start)}", state))


def test_recompute_after_names_turns_null_into_observed_and_stated(world):
    w = world
    seed, co_id = w["seed"], w["co"]["id"]
    w["write"](_artifact(
        "final", "2026-11-02T10:30:00Z", action_support=_linked(10.0),
        finding_support=_linked(20.0), decision_support=_linked(30.0),
        question_support={"status": "none_close", "best": 0.0, "rule_version": "x"}))
    for table, text in (("action_items", "Synthetic task one"),
                        ("findings", "Synthetic finding one"),
                        ("topic_decisions", "Synthetic decision one")):
        assert _live(w, table, text)[2] is None
    # Names land: the recorder on the action's turn (offset within tolerance, not equal),
    # someone else on the finding's, a tentative name on the decision's. The recorder's
    # profile lives in their HOME company (tenancy P5: an external recorder's own print is
    # matched from there), while the turn-name rows belong to the site's company.
    home = companies.create_company(seed, f"Links-W-Home-{uuid.uuid4().hex[:8]}")
    w["cleanup_companies"].append(home["id"])
    rec_vp = _voiceprint(seed, home["id"], w["user"]["id"])
    other_vp = _voiceprint(seed, co_id, w["other"]["id"])
    _name(seed, co_id, rec_vp, w["session_base"], 10.2)
    _name(seed, co_id, other_vp, w["session_base"], 20.0)
    _name(seed, co_id, other_vp, w["session_base"], 30.0, state="tentative")

    conn = get_connection(w["url"])
    try:
        with conn:
            counts = claim_type_store.recompute_session(
                conn, company_id=co_id, site_id=w["site"]["id"],
                session_base=w["session_base"])
    finally:
        conn.close()
    assert counts["rows"] == 4
    assert counts["claim"] == {"observed": 1, "stated": 1, "inferred": 1, "null": 1}
    a = _live(w, "action_items", "Synthetic task one")
    f = _live(w, "findings", "Synthetic finding one")
    d = _live(w, "topic_decisions", "Synthetic decision one")
    q = _live(w, "topic_questions", "Synthetic question one")
    assert a[2] == "observed" and a[3]["named_user_id"] == str(w["user"]["id"])
    assert f[2] == "stated" and f[3]["named_user_id"] == str(w["other"]["id"])
    assert d[2] is None and d[3]["name_state"] == "tentative"
    assert q[2] == "inferred"
    assert "Synthetic Person" not in json.dumps([a[3], f[3], d[3], q[3]])
    # Idempotent: a second run changes nothing.
    conn = get_connection(w["url"])
    try:
        with conn:
            again = claim_type_store.recompute_session(
                conn, company_id=co_id, site_id=w["site"]["id"],
                session_base=w["session_base"])
    finally:
        conn.close()
    assert again["changed"] == 0 and again["rows"] == 4
    # Another company's id is refused, not silently scoped.
    conn = get_connection(w["url"])
    try:
        with pytest.raises(ValueError):
            claim_type_store.recompute_session(
                conn, company_id=uuid.uuid4(), site_id=w["site"]["id"],
                session_base=w["session_base"])
    finally:
        conn.close()


def test_link_write_failure_does_not_fail_the_pass(world, monkeypatch, capsys, caplog):
    """A SQL error mid-step (after one link row was already inserted) rolls back the whole
    step's savepoint; the items still commit."""
    w = world
    real = claim_type_store.event_links.insert_supported_by
    calls = []

    def failing(conn, **kw):
        calls.append(1)
        link_id = real(conn, **kw)
        if len(calls) == 2:
            conn.execute("SELECT 1/0")
        return link_id

    monkeypatch.setattr(claim_type_store.event_links, "insert_supported_by", failing)
    with caplog.at_level(logging.WARNING):
        res = w["write"](_artifact("final", "2026-11-02T10:30:00Z",
                                   action_support=_linked(10.0),
                                   finding_support=_linked(20.0)))
    assert res == {"skipped": False, "topics": 1}, res
    failed = [r for r in caplog.records if "links/claim_type not stored" in r.getMessage()]
    assert len(failed) == 1 and "(DivisionByZero)" in failed[0].getMessage()
    assert failed[0].exc_info is None, "type name only, never a traceback"
    assert len(calls) == 2
    a = _live(w, "action_items", "Synthetic task one")
    f = _live(w, "findings", "Synthetic finding one")
    assert a is not None and f is not None, "items must still be written"
    assert a[2:] == (None, None) and f[2:] == (None, None)
    assert _links(w) == []
    emf = [json.loads(line) for line in capsys.readouterr().out.splitlines()
           if '"SupportLinked"' in line]
    assert len(emf) == 1 and emf[0]["ItemSupportWriteFailed"] == 1
    assert emf[0]["SupportLinked"] == 2 and emf[0]["LinksWritten"] == 0
