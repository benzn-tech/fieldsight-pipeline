"""Integration: agent-created topics reach the day view and therefore Tasks (action packs
spec §14 D2), against a real Postgres.

`/timeline` reads a person's day by recorder S3 prefix, and an action pack's topic is
keyed `agent://action_pack/<id>`, so it matched nothing. It is now unioned in by id: the
agent topics the person applied on that date, on sites in the reader's reach, then
clipped by site exactly like every other row (no bypass of the kind a merged meeting
gets). The ordinary day -- no agent topic -- must render byte-identically.
The `db` fixture rolls back after each test. Skipped without TEST_DATABASE_URL.
"""
import json
import uuid

import pytest

import lambda_org_api as api
from repositories import companies, memberships, redactions, sites, topics, users

pytestmark = pytest.mark.integration

DATE = "2026-11-01"
OTHER_DATE = "2026-11-02"
VERBATIM = {"executive_summary": "WHOLE DAY PROSE", "topics": []}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(api, "GRADED_ROLES", True)
    monkeypatch.setattr(api, "_get_lake_json", lambda key: dict(VERBATIM)
                        if key.endswith("daily_report.json") else None)
    monkeypatch.setattr(api, "_merged_keys_for", lambda conn, uid, date: [])


def _user(db, co, role, tag, who):
    sub = f"sub-{who}-{tag}"
    row = users.upsert_user(db, sub, f"{who}-{tag}@x.nz", company_id=co["id"],
                            first_name=who.title(), last_name="Test", global_role=role)
    users.set_folder_name(db, sub, f"{who.title()}_Test_{tag}")
    return users.get_by_id(db, row["id"])


def _world(db):
    tag = uuid.uuid4().hex[:6]
    co_a = companies.create_company(db, f"AgA-{tag}")
    co_b = companies.create_company(db, f"AgB-{tag}")
    site_a = sites.create_site(db, co_a["id"], "Yard A")
    site_b = sites.create_site(db, co_b["id"], "Tower B")
    eve = _user(db, co_a, "worker", tag, "eve")
    admin_a = _user(db, co_a, "admin", tag, "adma")
    admin_b = _user(db, co_b, "admin", tag, "admb")
    memberships.add_membership(db, eve["id"], site_a["id"], "worker")
    memberships.add_external_membership(db, eve["id"], site_b["id"], "worker")
    return dict(tag=tag, site_a=site_a, site_b=site_b, eve=eve, admin_a=admin_a,
                admin_b=admin_b, folder=eve["folder_name"])


def _regular(db, w, site, title, sid):
    key = f"extractions/{w['folder']}/{DATE}/sid{sid}.json"
    return topics.upsert_topic(db, site["id"], DATE, title, user_id=w["eve"]["id"],
                               source_s3_key=key, time_range="09:00 - 09:30",
                               action_items=[{"text": f"{title} task"}])


def _agent(db, w, site, title, *, pack=None, date=DATE, user_id=None):
    """What Task 7's apply writes: one synthetic topic per (pack, site), plus its todo."""
    key = f"agent://action_pack/{pack or uuid.uuid4()}"
    tid = db.execute(
        "INSERT INTO topics (site_id, user_id, source_s3_key, report_date, title, source) "
        "VALUES (%s, %s, %s, %s, %s, 'agent') RETURNING id",
        (site["id"], user_id or w["eve"]["id"], key, date, title)).fetchone()[0]
    db.execute("INSERT INTO action_items (topic_id, site_id, text, status) "
               "VALUES (%s, %s, %s, 'open')", (tid, site["id"], f"{title} task"))
    return tid


def _timeline(db, caller, user=None):
    ev = {"queryStringParameters": {"date": DATE, **({"user": user} if user else {})}}
    res = api.get_timeline_compat(db, caller, ev)
    return res["statusCode"], json.loads(res["body"])


def _titles(body):
    return sorted(t.get("topic_title") or "" for t in body.get("topics") or [])


def _sources(body):
    return {t["topic_title"]: (t["source"], [a["source"] for a in t["action_items"]])
            for t in body.get("topics") or []}


def test_agent_topic_appears_in_the_persons_day_beside_extracted_ones(db):
    w = _world(db)
    _regular(db, w, w["site_a"], "Yard A pour", "a" * 32)
    _agent(db, w, w["site_a"], "Pack: chase steel")
    code, body = _timeline(db, w["eve"])
    assert code == 200, body
    assert _titles(body) == ["Pack: chase steel", "Yard A pour"]
    assert _sources(body) == {"Pack: chase steel": ("agent", ["agent"]),
                              "Yard A pour": ("ai", ["ai"])}
    agent = next(t for t in body["topics"] if t["source"] == "agent")
    assert agent["session_kind"] != "report"
    # The extraction arm served, so the day keeps its prose.
    assert body.get("executive_summary") == "WHOLE DAY PROSE"


def test_a_day_with_only_agent_topics_renders_them_without_prose(db):
    w = _world(db)
    _agent(db, w, w["site_a"], "Pack: chase steel")
    code, body = _timeline(db, w["eve"])
    assert code == 200, body
    assert _titles(body) == ["Pack: chase steel"]
    assert body["topics"][0]["action_items"][0]["source"] == "agent"
    assert body.get("executive_summary") is None          # agent topics carry no S3 prose
    assert body["user"] == w["folder"]


def test_reach_on_the_site_decides_who_sees_the_agent_topic(db):
    w = _world(db)
    _regular(db, w, w["site_a"], "Yard A pour", "a" * 32)
    _regular(db, w, w["site_b"], "Tower B steel", "b" * 32)
    _agent(db, w, w["site_a"], "Pack on A")
    _agent(db, w, w["site_b"], "Pack on B")
    # Company A's admin reaches site A only.
    code, body = _timeline(db, w["admin_a"], w["folder"])
    assert code == 200 and _titles(body) == ["Pack on A", "Yard A pour"]
    # Company B's admin reaches site B only: never the site-A agent topic.
    code, body = _timeline(db, w["admin_b"], w["folder"])
    assert code == 200 and _titles(body) == ["Pack on B", "Tower B steel"]
    # The person sees both.
    code, body = _timeline(db, w["eve"])
    assert code == 200 and _titles(body) == ["Pack on A", "Pack on B",
                                             "Tower B steel", "Yard A pour"]


def test_an_agent_only_day_off_the_readers_sites_is_still_404(db):
    w = _world(db)
    _agent(db, w, w["site_a"], "Pack on A")
    code, _ = _timeline(db, w["admin_b"], w["folder"])
    assert code == 404


def test_other_days_other_people_and_retired_topics_stay_out(db):
    w = _world(db)
    _regular(db, w, w["site_a"], "Yard A pour", "a" * 32)
    pack = uuid.uuid4()
    _agent(db, w, w["site_a"], "Pack today", pack=pack)
    # Same pack, another apply date: shares the key, must not be pulled in by it.
    _agent(db, w, w["site_a"], "Pack tomorrow", pack=pack, date=OTHER_DATE)
    # Another person's agent topic on the same site and day.
    _agent(db, w, w["site_a"], "Admin's pack", user_id=w["admin_a"]["id"])
    gone = _agent(db, w, w["site_a"], "Retired pack")
    db.execute("UPDATE topics SET superseded_at = now() WHERE id = %s", (gone,))
    code, body = _timeline(db, w["eve"])
    assert code == 200 and _titles(body) == ["Pack today", "Yard A pour"]


def test_ordinary_day_is_byte_identical_without_agent_topics(db, monkeypatch):
    """Flag off: no agent topics exist, and the payload is exactly what the read
    produced before agent topics were unioned in (the lookup removed entirely), with
    the same repository call -- no extra kwarg."""
    w = _world(db)
    _regular(db, w, w["site_a"], "Yard A pour", "a" * 32)
    _regular(db, w, w["site_b"], "Tower B steel", "b" * 32)
    calls = []
    real = topics.list_topics_for_source_prefix

    def spy(conn, prefix, **kw):
        calls.append(kw)
        return real(conn, prefix, **kw)

    monkeypatch.setattr(topics, "list_topics_for_source_prefix", spy)
    cases = [(w["eve"], None), (w["admin_a"], w["folder"]), (w["admin_b"], w["folder"])]
    with_lookup = [_timeline(db, c, u) for c, u in cases]
    assert calls and all(kw == {} for kw in calls)
    monkeypatch.setattr(api, "_agent_topic_ids_for", lambda *a, **k: [])
    without_lookup = [_timeline(db, c, u) for c, u in cases]
    assert [json.dumps(b, sort_keys=True, default=str) for b in with_lookup] == \
           [json.dumps(b, sort_keys=True, default=str) for b in without_lookup]
    assert [c for c, _ in with_lookup] == [200, 200, 200]


def test_agent_lookup_failure_degrades_to_the_ordinary_day(db, monkeypatch):
    w = _world(db)
    _regular(db, w, w["site_a"], "Yard A pour", "a" * 32)
    _agent(db, w, w["site_a"], "Pack on A")

    def boom(*a, **k):
        db.execute("SELECT * FROM no_such_table_agent_lookup")

    monkeypatch.setattr(topics, "agent_topic_ids_for_day", boom)
    code, body = _timeline(db, w["eve"])
    assert code == 200 and _titles(body) == ["Yard A pour"]   # transaction still usable


def test_deleted_recorder_rows_do_not_bring_the_prose_back_with_an_agent_topic(db):
    """All of the person's own rows deleted, one agent topic left: the agent topic
    shows, the pre-deletion doc's prose does not ride along with it."""
    w = _world(db)
    t = _regular(db, w, w["site_a"], "Yard A pour", "a" * 32)
    redactions.create_redaction(db, w["site_a"]["company_id"], t["id"], "test", None,
                                "admin", scope="deleted")
    _agent(db, w, w["site_a"], "Pack on A")
    code, body = _timeline(db, w["eve"])
    assert code == 200 and _titles(body) == ["Pack on A"]
    assert "WHOLE DAY PROSE" not in json.dumps(body)


def test_graded_reader_never_sees_an_agent_topic_off_their_sites(db):
    """A pm of site A (graded SITE tier) reading eve's day: cross-user clip. The site-B
    agent topic is applied by eve on the same day and must stay out."""
    w = _world(db)
    pm = _user(db, {"id": w["site_a"]["company_id"]}, "pm", w["tag"], "pam")
    memberships.add_membership(db, pm["id"], w["site_a"]["id"], "pm")
    _regular(db, w, w["site_a"], "Yard A pour", "a" * 32)
    _agent(db, w, w["site_a"], "Pack on A")
    _agent(db, w, w["site_b"], "Pack on B")
    code, body = _timeline(db, dict(pm), w["folder"])
    assert code == 200, body
    assert _titles(body) == ["Pack on A", "Yard A pour"]
    assert "WHOLE DAY PROSE" not in json.dumps(body)


def test_agent_topic_on_an_inactive_companys_site_is_not_shown(db, monkeypatch):
    """Company offboarding (spec 2026-11-01 §5.2): eve's own day loses the site of a
    suspended company -- her agent topic there included."""
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    w = _world(db)
    _regular(db, w, w["site_a"], "Yard A pour", "a" * 32)
    _agent(db, w, w["site_a"], "Pack on A")
    _agent(db, w, w["site_b"], "Pack on B")
    code, body = _timeline(db, dict(w["eve"]))
    assert code == 200 and _titles(body) == ["Pack on A", "Pack on B", "Yard A pour"]
    db.execute("UPDATE companies SET status = 'suspended' WHERE id = %s",
               (w["site_b"]["company_id"],))
    code, body = _timeline(db, dict(w["eve"]))
    assert code == 200 and _titles(body) == ["Pack on A", "Yard A pour"]


def test_no_agent_topic_means_one_probe_and_no_reach(db, monkeypatch):
    """The cost on every ordinary /timeline read: the probe only. Reach and the id
    lookup run only on a hit."""
    w = _world(db)
    _regular(db, w, w["site_a"], "Yard A pour", "a" * 32)
    probes, lookups = [], []
    real_probe = topics.agent_topic_author_for_folder_day
    monkeypatch.setattr(topics, "agent_topic_author_for_folder_day",
                        lambda *a: probes.append(a) or real_probe(*a))
    monkeypatch.setattr(topics, "agent_topic_ids_for_day",
                        lambda *a: lookups.append(a) or [])
    reach_calls = []
    real_reach = api._agent_topic_ids_for

    def spy(conn, caller, user, date, reach):
        return real_reach(conn, caller, user, date,
                          lambda: reach_calls.append(1) or reach())

    monkeypatch.setattr(api, "_agent_topic_ids_for", spy)
    code, _ = _timeline(db, w["eve"])
    assert code == 200
    assert len(probes) == 1 and lookups == [] and reach_calls == []
    _agent(db, w, w["site_a"], "Pack on A")
    _timeline(db, w["eve"])
    assert len(lookups) == 1 and reach_calls == [1]


def test_a_failing_probe_leaves_the_transaction_usable(db, monkeypatch):
    w = _world(db)
    _regular(db, w, w["site_a"], "Yard A pour", "a" * 32)
    monkeypatch.setattr(topics, "agent_topic_author_for_folder_day",
                        lambda conn, *a: conn.execute("SELECT * FROM no_such_probe_table"))
    code, body = _timeline(db, w["eve"])
    assert code == 200 and _titles(body) == ["Yard A pour"]


def test_the_probe_index_exists(db):
    """Migration 0093's partial index the probe reads: agent topics only."""
    row = db.execute("SELECT indexdef FROM pg_indexes "
                     "WHERE indexname = 'idx_topics_agent_user_date'").fetchone()
    assert row is not None
    assert "(user_id, report_date)" in row[0] and "'agent'" in row[0]
