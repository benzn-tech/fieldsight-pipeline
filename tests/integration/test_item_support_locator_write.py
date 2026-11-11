"""Integration: the item writer stores locator supports (locator spec 2026-11-10 §4.3, §4.4)
against a real Postgres.

Same committed-connection harness and id-based cleanup as test_supersede_two_passes.py /
test_item_writer_event_links.py: real passes through write_extraction_items, a fake S3
holding the synthetic artifact. The locator `support` objects are put on the children
directly (what item_support attaches on the locator path), so nothing here depends on
EMIT_ITEM_LOCATOR or EMIT_ITEM_SUPPORT. All text is synthetic.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import io
import json
import uuid

import pytest

from db.connection import get_connection
from repositories import companies, memberships, sites, users

pytestmark = pytest.mark.integration

DATE = "2026-11-10"
STEM = "synthetic-chunk-loc"

lambda_item_writer = pytest.importorskip(
    "lambda_item_writer", reason="requires psycopg (installed in CI)")
claim_type_store = pytest.importorskip("repositories.claim_type_store")
import claim_type  # noqa: E402
import item_locator  # noqa: E402


class _FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key].encode("utf-8"))}

    def get_paginator(self, op):
        assert op == "list_objects_v2"
        return _FakePaginator(self.objects)


class _FakePaginator:
    def __init__(self, objects):
        self.objects = objects

    def paginate(self, Bucket, Prefix):
        yield {"Contents": [{"Key": k} for k in self.objects if k.startswith(Prefix)]}


def _locator_linked(start):
    return {"status": "linked", "source_stem": STEM, "start_sec": start,
            "end_sec": start + 3.0, "speaker_label": "spk_1", "quote_status": None,
            "method": item_locator.RULE_VERSION, "rule_version": item_locator.RULE_VERSION,
            "score": None, "range_checked": True}


def _refusal(status):
    return {"status": status, "method": item_locator.RULE_VERSION,
            "rule_version": item_locator.RULE_VERSION}


def _artifact():
    return {
        "schema_version": 1, "tier": "final", "extracted_at": "2026-11-10T10:30:00Z",
        "topics": [{
            "topic_title": "Synthetic topic", "category": "progress", "summary": "s",
            "time_range": "10:00 – 10:05", "participants": [], "safety_flags": [],
            "action_items": [{"action": "Synthetic locator task",
                              "support": _locator_linked(12.0)}],
            "findings": [{"observation": "Synthetic locator finding",
                          "support": _refusal("no_locator")}],
            "decisions": [], "questions": [],
        }],
    }


@pytest.fixture
def world(monkeypatch, migrated_db_url):
    tag = uuid.uuid4().hex[:8]
    seed = get_connection(migrated_db_url, autocommit=True)
    made = {}
    try:
        made["co"] = companies.create_company(seed, f"Loc-W-Co-{tag}")
        made["site"] = sites.create_site(seed, made["co"]["id"], f"Loc-W-Site-{tag}")
        folder = f"LocW-{tag}"
        made["user"] = users.upsert_field_only_user(seed, made["co"]["id"], folder,
                                                    "Fol", "Der", "worker")
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
               "url": migrated_db_url, **made}
    finally:
        if "site" in made:
            seed.execute("DELETE FROM sites WHERE id=%s", (made["site"]["id"],))
        if "user" in made:
            seed.execute("DELETE FROM users WHERE id=%s", (made["user"]["id"],))
        if "co" in made:
            seed.execute("DELETE FROM companies WHERE id=%s", (made["co"]["id"],))
            assert seed.execute(
                "SELECT (SELECT count(*) FROM sites WHERE id=%s), "
                "(SELECT count(*) FROM event_links WHERE site_id=%s), "
                "(SELECT count(*) FROM speaker_turn_names WHERE company_id=%s)",
                (made["site"]["id"], made["site"]["id"], made["co"]["id"])).fetchone() \
                == (0, 0, 0)
        seed.close()


def _row(w, table, col, text):
    return w["seed"].execute(
        f"SELECT c.id, c.claim_type, c.claim_basis FROM {table} c "
        f"JOIN topics t ON t.id = c.topic_id WHERE t.source_s3_key = %s "
        f"AND t.superseded_at IS NULL AND c.{col} = %s", (w["key"], text)).fetchone()


def _links(w):
    return w["seed"].execute(
        "SELECT id, src_kind, src_action_item_id, method, rule_version, score, dst_turn "
        "FROM event_links WHERE site_id = %s ORDER BY created_at, id",
        (w["site"]["id"],)).fetchall()


def test_locator_supports_are_stored(world, capsys, monkeypatch):
    # The shipped constant, pinned so a later measured flip shows up here, not silently.
    assert claim_type.INFERRED_FROM_NO_LOCATOR is False
    w = world
    res = w["write"](_artifact())
    assert res == {"skipped": False, "topics": 1}, res

    a = _row(w, "action_items", "text", "Synthetic locator task")
    f = _row(w, "findings", "observation", "Synthetic locator finding")
    links = _links(w)
    assert len(links) == 1
    link_id, src_kind, src_action, method, rule_version, score, dst_turn = links[0]
    assert (src_kind, src_action) == ("action_item", a[0])
    assert method == "locator_v2" and rule_version == "locator_v2"
    assert score is None
    assert set(dst_turn) == set(claim_type_store.DST_TURN_KEYS)
    assert dst_turn["source_stem"] == STEM and dst_turn["start_sec"] == 12.0
    assert dst_turn["end_sec"] == 15.0

    # No names, flag false: both NULL; the bases record v2 and the status.
    assert a[1] is None and a[2] == {"rule_version": "claim_v2", "support_status": "linked",
                                     "link_id": str(link_id)}
    assert f[1] is None and f[2] == {"rule_version": "claim_v2",
                                     "support_status": "no_locator"}

    emf = [json.loads(line) for line in capsys.readouterr().out.splitlines()
           if '"SupportLinked"' in line]
    assert len(emf) == 1
    assert (emf[0]["SupportLinked"], emf[0]["SupportNoLocator"], emf[0]["LinksWritten"],
            emf[0]["ClaimNull"], emf[0]["ItemSupportWriteFailed"]) == (1, 1, 1, 2, 0)

    # A name lands on the linked turn: the voiceprint recompute (which rebuilds the support
    # from the stored link) derives the locator link as linked -> observed.
    seed, co_id = w["seed"], w["co"]["id"]
    vp = seed.execute(
        "INSERT INTO speaker_voiceprints (company_id, user_id, display_name, status) "
        "VALUES (%s, %s, 'Synthetic Person', 'confirmed') RETURNING id",
        (co_id, w["user"]["id"])).fetchone()[0]
    seed.execute(
        "INSERT INTO speaker_turn_names (company_id, voiceprint_id, session_base, turn_ref, "
        " state, display_name, source) VALUES (%s, %s, %s, %s, 'confirmed', "
        " 'Synthetic Person', 'correction')",
        (co_id, vp, w["session_base"], f"{STEM}@12.0"))
    conn = get_connection(w["url"])
    try:
        with conn:
            counts = claim_type_store.recompute_session(
                conn, company_id=co_id, site_id=w["site"]["id"],
                session_base=w["session_base"])
    finally:
        conn.close()
    assert counts["rows"] == 1 and counts["claim"]["observed"] == 1
    a = _row(w, "action_items", "text", "Synthetic locator task")
    assert a[1] == "observed" and a[2]["rule_version"] == "claim_v2"
    # The no_locator row is not a recompute row and stays NULL.
    assert _row(w, "findings", "observation", "Synthetic locator finding")[1] is None

    # Deleting the item row cascades its link (slice 1 FK).
    seed.execute("DELETE FROM action_items WHERE id = %s", (a[0],))
    assert _links(w) == []


def test_locator_v2_tie_break_support_writes_and_reads_back(world):
    """A locator_v2 support that the same-second tie-break produced (a 0.2 s fragment beside
    the real line, both stamped 10:00:12) is stored, and the link reads back the long turn's
    keys under `locator_v2` -- not the fragment's."""
    from datetime import datetime

    def turn(start, end, speaker):
        at = datetime(2026, 11, 10, 10, 0, 12)
        return {"text": "synthetic", "speaker": speaker, "source_filename": f"{STEM}.json",
                "start_sec": start, "end_sec": end, "abs_start": at,
                "abs_start_str": "10:00:12"}

    turns = [turn(12.0, 12.2, "spk_2"), turn(12.4, 16.0, "spk_1")]
    topic = {"topic_title": "Synthetic topic", "time_range": "10:00 – 10:05"}
    support = item_locator.resolve({"said_at": "10:00:12"}, topic, turns, {0, 1}, DATE)
    assert support["status"] == "linked" and support["rule_version"] == "locator_v2"

    art = _artifact()
    art["topics"][0]["action_items"][0]["support"] = support
    assert world["write"](art) == {"skipped": False, "topics": 1}

    links = _links(world)
    assert len(links) == 1
    _id, _kind, _src, method, rule_version, score, dst_turn = links[0]
    assert (method, rule_version, score) == ("locator_v2", "locator_v2", None)
    assert dst_turn == {"source_stem": STEM, "start_sec": 12.4, "end_sec": 16.0,
                        "speaker_label": "spk_1", "quote_status": None}
