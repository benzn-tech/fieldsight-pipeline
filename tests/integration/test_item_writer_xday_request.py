"""Integration: hop 1 of cross-day continuity -- the item writer builds a candidate request
and puts it to S3 after the commit (spec 2026-11-09 X1-X5, X10), against a real Postgres.

Same committed-connection harness as test_item_writer_event_links.py: real passes through
write_extraction_items, a fake S3 holding the synthetic artifacts. An EARLIER session on the
same site is written first (flag off), then today's pass. All text is synthetic.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import io
import json
import uuid

import pytest

from db.connection import get_connection
from repositories import companies, memberships, sites, users

pytestmark = pytest.mark.integration

EARLY = "2026-11-02"
EARLIER = "2026-10-30"
DATE = "2026-11-09"

lambda_item_writer = pytest.importorskip(
    "lambda_item_writer", reason="requires psycopg (installed in CI)")
item_history = pytest.importorskip("repositories.item_history")

PREFIX = "continuity_xday_requests/"


class _FakeS3:
    """get_object from `objects`; put_object records (key, body, committed-at-put-time).

    `probe` is called at each request put: it opens a SECOND connection and reports whether
    today's topics are visible there, which they are only once the writer has committed."""

    def __init__(self, objects):
        self.objects = objects
        self.puts = []
        self.probe = None
        self.fail_puts = False

    def get_object(self, Bucket, Key):
        body = self.objects[Key]
        raw = body.encode("utf-8") if isinstance(body, str) else body
        return {"Body": io.BytesIO(raw)}

    def put_object(self, Bucket, Key, Body, **kw):
        if not Key.startswith(PREFIX):
            return {}
        if self.fail_puts:
            raise RuntimeError("synthetic put failure")
        self.puts.append({"key": Key, "body": json.loads(Body),
                          "committed": self.probe() if self.probe else None})
        return {}

    def get_paginator(self, op):
        assert op == "list_objects_v2"
        return _FakePaginator(self.objects)


class _FakePaginator:
    def __init__(self, objects):
        self.objects = objects

    def paginate(self, Bucket, Prefix):
        yield {"Contents": [{"Key": k} for k in self.objects if k.startswith(Prefix)]}


def _artifact(tier, extracted_at, *, actions=(), questions=(), findings=(), incomplete=False):
    art = {
        "schema_version": 1, "tier": tier, "extracted_at": extracted_at,
        "topics": [{
            "topic_title": "Synthetic topic", "category": "progress", "summary": "s",
            "time_range": "10:00 - 10:05", "participants": [], "safety_flags": [],
            "action_items": [{"action": a} for a in actions],
            "findings": [{"observation": f} for f in findings],
            "decisions": [],
            "questions": [{"question": q} for q in questions],
        }],
    }
    if incomplete:
        art["incomplete"] = True
    return art


@pytest.fixture
def world(monkeypatch, migrated_db_url):
    tag = uuid.uuid4().hex[:8]
    seed = get_connection(migrated_db_url, autocommit=True)
    made = {}
    try:
        made["co"] = companies.create_company(seed, f"Xday-Co-{tag}")
        made["site"] = sites.create_site(seed, made["co"]["id"], f"Xday-Site-{tag}")
        folder = f"XdayW-{tag}"
        made["user"] = users.upsert_field_only_user(seed, made["co"]["id"], folder,
                                                    "Fol", "Der", "worker")
        memberships.add_membership(seed, made["user"]["id"], made["site"]["id"], "worker")
        monkeypatch.setenv("LAKE_MANIFEST", "off")
        fake_s3 = _FakeS3({})
        monkeypatch.setattr(lambda_item_writer, "_s3_client", fake_s3)
        monkeypatch.setattr(lambda_item_writer, "get_connection",
                            lambda *a, **k: get_connection(migrated_db_url))
        monkeypatch.setattr(lambda_item_writer.match_request, "emit", lambda *a, **k: None)
        monkeypatch.setattr(lambda_item_writer, "CROSS_DAY_CONTINUITY", "off")
        keys = {}

        def key_for(date, name):
            return keys.setdefault((date, name),
                                   f"extractions/{folder}/{date}/sid{uuid.uuid4().hex}.json")

        def write(date, name, artifact, mode="off"):
            monkeypatch.setattr(lambda_item_writer, "CROSS_DAY_CONTINUITY", mode)
            key = key_for(date, name)
            fake_s3.objects[key] = json.dumps(artifact)
            return lambda_item_writer.write_extraction_items(date, folder, key)

        def committed_today():
            key = keys.get((DATE, "today"))
            with get_connection(migrated_db_url, autocommit=True) as c:
                return c.execute(
                    "SELECT count(*) FROM topics WHERE source_s3_key = %s "
                    "AND superseded_at IS NULL", (key,)).fetchone()[0] > 0

        fake_s3.probe = committed_today
        yield {"seed": seed, "s3": fake_s3, "write": write, "key_for": key_for,
               "folder": folder, "url": migrated_db_url, **made}
    finally:
        if "site" in made:
            seed.execute("DELETE FROM sites WHERE id=%s", (made["site"]["id"],))
        if "user" in made:
            seed.execute("DELETE FROM users WHERE id=%s", (made["user"]["id"],))
        if "co" in made:
            seed.execute("DELETE FROM companies WHERE id=%s", (made["co"]["id"],))
        seed.close()


def _live_stable_id(w, key, table, col, text):
    row = w["seed"].execute(
        f"SELECT c.stable_id::text FROM {table} c JOIN topics t ON t.id = c.topic_id "
        f"WHERE t.source_s3_key = %s AND t.superseded_at IS NULL AND c.{col} = %s",
        (key, text)).fetchone()
    assert row, (key, text)
    return row[0]


def _spy_find_items(monkeypatch):
    calls = []
    real = item_history.find_items

    def spy(conn, **kw):
        calls.append(kw)
        return real(conn, **kw)

    monkeypatch.setattr(item_history, "find_items", spy)
    return calls


def _seed_earlier(w):
    w["write"](EARLY, "early", _artifact(
        "final", "2026-11-02T10:30:00Z", actions=["Order rebar for level two slab"],
        questions=["Who signs off the level two pour"], findings=["Crack in east wall render"]))
    key = w["key_for"](EARLY, "early")
    return {
        "action": _live_stable_id(w, key, "action_items", "text", "Order rebar for level two slab"),
        "question": _live_stable_id(w, key, "topic_questions", "question",
                                    "Who signs off the level two pour"),
        "finding": _live_stable_id(w, key, "findings", "observation", "Crack in east wall render"),
    }


TODAY = dict(actions=["Chase the rebar order for level two slab"],
             questions=["Who signs off the level two pour today"],
             findings=["East wall render crack has widened"])


def test_shadow_builds_and_puts_the_request_after_commit(world, monkeypatch):
    w = world
    early = _seed_earlier(w)
    calls = _spy_find_items(monkeypatch)
    res = w["write"](DATE, "today", _artifact("final", "2026-11-09T11:00:00Z", **TODAY),
                     mode="shadow")
    assert res["skipped"] is False
    key = w["key_for"](DATE, "today")
    assert len(w["s3"].puts) == 1, w["s3"].puts
    put = w["s3"].puts[0]
    assert put["committed"] is True, "the request was put before the pass committed"
    body = put["body"]
    assert put["key"] == f"{PREFIX}{w['site']['id']}/{DATE}/{body['run_hash']}.json"
    assert body["site_id"] == str(w["site"]["id"])
    assert body["company_id"] == str(w["co"]["id"])
    assert body["extraction_key"] == key and body["date"] == DATE
    assert body["trigger"] == "final"
    assert body["extracted_at"] == "2026-11-09T11:00:00Z"

    news = {n["kind"]: n for n in body["new_items"]}
    assert set(news) == {"action_item", "question", "finding"}
    assert news["action_item"]["stable_id"] == _live_stable_id(
        w, key, "action_items", "text", TODAY["actions"][0])
    assert news["action_item"]["text"] == TODAY["actions"][0]
    cands = {c["kind"]: c for c in body["candidates"]}
    assert cands["action_item"]["stable_id"] == early["action"]
    assert cands["action_item"]["alias"] == "A1"
    assert cands["action_item"]["first_date"] == EARLY
    assert cands["question"]["stable_id"] == early["question"]
    assert cands["finding"]["stable_id"] == early["finding"]
    assert body["precap_counts"] == {"action_items": 1, "questions": 1, "findings": 1}

    # every find_items call excludes this session's key (reopen rounds reuse it), carries the
    # 28-day window and X5's statuses, and is scoped to the site's company
    assert calls
    for kw in calls:
        assert list(kw["exclude_source_keys"]) == [key]
        assert str(kw["site_id"]) == str(w["site"]["id"])
        assert str(kw["company_id"]) == str(w["co"]["id"])
        assert str(kw["since"]) == "2026-10-12" and str(kw["until"]) == DATE
        kind = kw["kinds"][0]
        if kind == "action_item":
            assert kw["statuses"] == {"action_item": ["open", "in_progress", "blocked"]}
        elif kind == "question":
            assert kw["statuses"] == {"question": ["open"]}
        else:
            assert kw["statuses"] in (None, {})
    lexical = [kw for kw in calls if kw.get("query")]
    assert len(lexical) == 3 and all(kw["limit"] == 5 for kw in lexical)


def test_on_also_emits_the_request(world):
    w = world
    _seed_earlier(w)
    w["write"](DATE, "today", _artifact("final", "2026-11-09T11:00:00Z", **TODAY), mode="on")
    assert len(w["s3"].puts) == 1


def test_this_sessions_earlier_pass_is_never_a_candidate(world):
    """A live pass of today's key, then the final: the live pass's items are this session's
    own (excluded by key and by stable_id), only the earlier session's item is shown."""
    w = world
    early = _seed_earlier(w)
    w["write"](DATE, "today", _artifact("live", "2026-11-09T10:00:00Z",
                                        actions=["Something only said in the live pass"]))
    w["write"](DATE, "today", _artifact("final", "2026-11-09T11:00:00Z", **TODAY),
               mode="shadow")
    body = w["s3"].puts[-1]["body"]
    assert [c["stable_id"] for c in body["candidates"] if c["kind"] == "action_item"] == [
        early["action"]]


def test_closed_and_out_of_window_items_are_not_candidates(world):
    w = world
    w["write"]("2026-10-01", "old", _artifact(
        "final", "2026-10-01T10:00:00Z", actions=["Order rebar for level two slab"]))
    w["write"](EARLY, "early", _artifact(
        "final", "2026-11-02T10:30:00Z", actions=["Order rebar for level two slab now"]))
    w["seed"].execute(
        "UPDATE action_items SET status = 'done' WHERE topic_id IN "
        "(SELECT id FROM topics WHERE source_s3_key = %s)", (w["key_for"](EARLY, "early"),))
    w["write"](DATE, "today", _artifact("final", "2026-11-09T11:00:00Z",
                                        actions=TODAY["actions"]), mode="shadow")
    assert w["s3"].puts == [], "no live candidate in window -> no request"


def test_a_chain_is_shown_once_as_its_newest_member(world):
    w = world
    w["write"](EARLIER, "earlier", _artifact(
        "final", "2026-10-30T10:00:00Z", actions=["Order rebar for level two slab"]))
    older = _live_stable_id(w, w["key_for"](EARLIER, "earlier"), "action_items", "text",
                            "Order rebar for level two slab")
    w["write"](EARLY, "early", _artifact(
        "final", "2026-11-02T10:30:00Z", actions=["Rebar order for level two slab placed"]))
    newer = _live_stable_id(w, w["key_for"](EARLY, "early"), "action_items", "text",
                            "Rebar order for level two slab placed")
    w["seed"].execute(
        "INSERT INTO event_links (site_id, link_type, src_kind, src_stable_id, dst_kind, "
        "dst_stable_id, method, rule_version) VALUES "
        "(%s, 'continues', 'action_item', %s, 'action_item', %s, 'llm_echo', 'xday_v1')",
        (w["site"]["id"], newer, older))
    w["write"](DATE, "today", _artifact("final", "2026-11-09T11:00:00Z",
                                        actions=TODAY["actions"]), mode="shadow")
    body = w["s3"].puts[-1]["body"]
    assert [c["stable_id"] for c in body["candidates"]] == [newer]
    # pre-cap counts what existed before the cap and the collapse
    assert body["precap_counts"]["action_items"] == 2


def test_a_chain_collapses_through_a_member_that_is_not_a_candidate(world):
    """oldest <- middle <- newest, the middle one closed: the oldest and the newest are still
    one chain, shown once as the newest."""
    w = world
    texts = {"a": "Order rebar for level two slab", "b": "Rebar order for level two slab sent",
             "c": "Rebar order for level two slab confirmed"}
    sids = {}
    for name, d in (("a", "2026-10-20"), ("b", "2026-10-27"), ("c", EARLY)):
        w["write"](d, name, _artifact("final", f"{d}T10:00:00Z", actions=[texts[name]]))
        sids[name] = _live_stable_id(w, w["key_for"](d, name), "action_items", "text",
                                     texts[name])
    w["seed"].execute("UPDATE action_items SET status = 'done' WHERE stable_id = %s",
                      (sids["b"],))
    for src, dst in (("b", "a"), ("c", "b")):
        w["seed"].execute(
            "INSERT INTO event_links (site_id, link_type, src_kind, src_stable_id, dst_kind, "
            "dst_stable_id, method, rule_version) VALUES "
            "(%s, 'continues', 'action_item', %s, 'action_item', %s, 'llm_echo', 'xday_v1')",
            (w["site"]["id"], sids[src], sids[dst]))
    w["write"](DATE, "today", _artifact("final", "2026-11-09T11:00:00Z",
                                        actions=TODAY["actions"]), mode="shadow")
    body = w["s3"].puts[-1]["body"]
    assert [c["stable_id"] for c in body["candidates"]] == [sids["c"]]


@pytest.mark.parametrize("case", ["incomplete", "live_tier", "zero_candidates", "off"])
def test_no_request(world, monkeypatch, case):
    w = world
    if case != "zero_candidates":
        _seed_earlier(w)
    if case == "off":
        def boom(*a, **k):
            raise AssertionError("no cross-day SQL may run with the flag off")
        monkeypatch.setattr(item_history, "find_items", boom)
        monkeypatch.setattr(lambda_item_writer, "build_xday_request", boom)
    tier = "live" if case == "live_tier" else "final"
    res = w["write"](DATE, "today", _artifact(tier, "2026-11-09T11:00:00Z",
                                              incomplete=(case == "incomplete"), **TODAY),
                     mode="off" if case == "off" else "shadow")
    assert res["skipped"] is False
    assert w["s3"].puts == []


def test_no_site_builds_nothing_and_runs_no_sql():
    class _NoConn:
        def execute(self, *a, **k):
            raise AssertionError("no SQL without a site")

        def cursor(self, *a, **k):
            raise AssertionError("no SQL without a site")

    assert lambda_item_writer.build_xday_request(
        _NoConn(), site=None, company_id="c", extraction={"tier": "final"},
        extraction_key="extractions/f/2026-11-09/sidx.json", date=DATE) is None


def test_a_failing_build_never_fails_the_write(world, monkeypatch):
    """The build poisons the transaction with a real SQL error and raises: the savepoint
    takes the error, the pass still commits, nothing is put."""
    w = world
    _seed_earlier(w)

    def poison(conn, **kw):
        conn.execute("SELECT 1/0")

    monkeypatch.setattr(item_history, "find_items", poison)
    res = w["write"](DATE, "today", _artifact("final", "2026-11-09T11:00:00Z", **TODAY),
                     mode="shadow")
    assert res["skipped"] is False
    assert w["s3"].probe() is True, "the pass did not commit"
    assert w["s3"].puts == []


def test_a_failing_put_never_fails_the_write(world):
    w = world
    _seed_earlier(w)
    w["s3"].fail_puts = True
    res = w["write"](DATE, "today", _artifact("final", "2026-11-09T11:00:00Z", **TODAY),
                     mode="shadow")
    assert res["skipped"] is False
    assert w["s3"].probe() is True


def test_nonce_and_trigger_are_the_callers(world):
    """The public builder, as the nightly re-match agent calls it: its nonce gives its own
    run_hash and its trigger rides in the artifact only."""
    w = world
    _seed_earlier(w)
    w["write"](DATE, "today", _artifact("final", "2026-11-09T11:00:00Z", **TODAY))
    key = w["key_for"](DATE, "today")
    extraction = json.loads(w["s3"].objects[key])
    site = {"id": w["site"]["id"], "company_id": w["co"]["id"]}
    with get_connection(w["url"]) as conn:
        plain = lambda_item_writer.build_xday_request(
            conn, site=site, company_id=w["co"]["id"], extraction=extraction,
            extraction_key=key, date=DATE)
        nightly = lambda_item_writer.build_xday_request(
            conn, site=site, company_id=w["co"]["id"], extraction=extraction,
            extraction_key=key, date=DATE, nonce="n1", trigger="nightly")
    assert plain["trigger"] == "final" and nightly["trigger"] == "nightly"
    assert nightly["nonce"] == "n1" and nightly["run_hash"] != plain["run_hash"]
    assert nightly["candidates"] == plain["candidates"]
