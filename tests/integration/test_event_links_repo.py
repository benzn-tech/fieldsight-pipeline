"""Integration: repositories.event_links against a real Postgres.

Pins the deletion reach of the evidence chain: a revocable tombstone (topic or recording)
hides links without deleting them and an undelete brings them back; a physical source or
site delete removes the link rows. All text is synthetic. Skipped without
TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import datetime as _dt
import uuid

import pytest

from repositories import companies, event_links, redactions, sites, topics, users

pytestmark = pytest.mark.integration

DAY = _dt.date(2026, 10, 1)


def _turn(stem="stem-a", start=1.0):
    return {"source_stem": stem, "start_sec": start, "end_sec": start + 3.0,
            "speaker_label": "spk_0", "quote_status": "verified"}


class Seed:
    def __init__(self, db):
        self.db = db
        tag = uuid.uuid4().hex[:8]
        self.tag = tag
        self.co = companies.create_company(db, f"Links-Co-{tag}")
        self.site = sites.create_site(db, self.co["id"], f"Links-Site-{tag}")
        self.folder = f"Folder-{tag}"
        self.user = users.upsert_field_only_user(db, self.co["id"], self.folder,
                                                 "Fol", "Der", "worker")

    def key(self, base):
        return f"extractions/{self.folder}/{DAY.isoformat()}/{base}.json"

    def topic(self, base):
        return topics.upsert_topic(self.db, self.site["id"], DAY, f"Topic {base}",
                                   user_id=self.user["id"], source_s3_key=self.key(base),
                                   summary="s")

    def item(self, kind, topic, stable_id=None):
        table = event_links.KIND_TABLE[kind]
        text_col = {"action_item": "text", "finding": "observation",
                    "decision": "decision", "question": "question"}[kind]
        cols, vals = ["topic_id", "site_id", text_col], [topic["id"], self.site["id"],
                                                         f"synthetic {kind}"]
        if stable_id is not None:
            cols.append("stable_id")
            vals.append(stable_id)
        row = self.db.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(vals))}) "
            f"RETURNING id, stable_id", vals).fetchone()
        return {"id": row[0], "stable_id": row[1], "topic": topic}

    def support(self, kind, item, score=0.8, stem="stem-a"):
        return event_links.insert_supported_by(
            self.db, site_id=self.site["id"], src_kind=kind, src_row_id=item["id"],
            src_stable_id=item["stable_id"], source_s3_key=item["topic"]["source_s3_key"],
            dst_turn=_turn(stem), score=score, rule_version="v-test")

    def tombstone(self, topic):
        return redactions.create_redaction(self.db, self.co["id"], topic["id"], "gone",
                                           None, "worker", scope="deleted")

    def links(self, stable_id, **kw):
        return event_links.links_for_stable_id(self.db, site_id=self.site["id"],
                                               stable_id=stable_id, **kw)


def _count(db, link_id):
    return db.execute("SELECT count(*) FROM event_links WHERE id = %s", (link_id,)).fetchone()[0]


def test_insert_supported_by_sets_the_right_fk(db):
    s = Seed(db)
    t = s.topic("sid" + "a" * 32)
    for kind, col in event_links.KIND_SRC_COLUMN.items():
        item = s.item(kind, t)
        link_id = s.support(kind, item)
        row = db.execute(
            "SELECT link_type, src_kind, src_stable_id, src_action_item_id, src_finding_id, "
            "src_decision_id, src_question_id, dst_kind, dst_stable_id, dst_turn, method, "
            "score, rule_version, source_s3_key FROM event_links WHERE id = %s",
            (link_id,)).fetchone()
        fks = dict(zip(("src_action_item_id", "src_finding_id", "src_decision_id",
                        "src_question_id"), row[3:7]))
        assert fks.pop(col) == item["id"], kind
        assert set(fks.values()) == {None}, kind
        assert row[:3] == ("supported_by", kind, item["stable_id"])
        assert row[7:9] == ("turn", None)
        assert row[9] == _turn()
        assert row[10] == "lexical_v1" and row[12] == "v-test"
        assert row[11] == pytest.approx(0.8)
        assert row[13] == t["source_s3_key"]
    with pytest.raises(ValueError):
        s.support("topic", {"id": None, "stable_id": None, "topic": t})


def test_live_supported_by_ignores_superseded_rows(db):
    s = Seed(db)
    t_old, t_new = s.topic("sid" + "a" * 32), s.topic("sid" + "b" * 32)
    old = s.item("action_item", t_old)
    new = s.item("action_item", t_new, stable_id=old["stable_id"])   # carried forward
    lonely = s.item("action_item", t_new)                             # no link
    s.support("action_item", old, stem="stem-old")
    s.support("action_item", new, score=0.4, stem="stem-weak")
    s.support("action_item", new, score=0.9, stem="stem-best")
    db.execute("UPDATE topics SET superseded_at = now() WHERE id = %s", (t_old["id"],))

    ids = [str(old["id"]), str(new["id"]), str(lonely["id"])]
    got = event_links.live_supported_by(db, "action_item", ids)
    assert list(got) == [str(new["id"])]                    # caller's own key type back
    assert got[str(new["id"])]["dst_turn"]["source_stem"] == "stem-best"
    assert got[str(new["id"])]["score"] == pytest.approx(0.9)
    assert event_links.live_supported_by(db, "action_item", []) == {}
    # the same row, keyed as uuid, gets the same answer
    assert list(event_links.live_supported_by(db, "action_item", [new["id"]])) == [new["id"]]
    # a deletion tombstone hides it too
    s.tombstone(t_new)
    assert event_links.live_supported_by(db, "action_item", ids) == {}


def test_redaction_hides_links_and_undelete_restores_them(db):
    s = Seed(db)
    t = s.topic("sid" + "a" * 32)
    item = s.item("finding", t)
    link_id = s.support("finding", item)
    assert [r["id"] for r in s.links(item["stable_id"])] == [link_id]

    red = s.tombstone(t)
    assert s.links(item["stable_id"]) == []
    assert _count(db, link_id) == 1                          # hidden, not deleted
    redactions.revert_redaction(db, red["id"], s.co["id"])
    assert [r["id"] for r in s.links(item["stable_id"])] == [link_id]

    # the recording arm: a tombstone over the source prefix hides it as well
    rec = redactions.create_recording_tombstone(
        db, s.co["id"], t["source_s3_key"].rsplit(".", 1)[0], "gone", None, "worker")
    assert s.links(item["stable_id"]) == []
    redactions.revert_redaction(db, rec["id"], s.co["id"])
    assert [r["id"] for r in s.links(item["stable_id"])] == [link_id]


def _item_link(db, s, link_type, src, src_kind, dst, dst_kind):
    return db.execute(
        "INSERT INTO event_links (site_id, link_type, src_kind, src_stable_id, dst_kind, "
        "dst_stable_id, method, rule_version) VALUES (%s,%s,%s,%s,%s,%s,'rule','v-test') "
        "RETURNING id",
        (s.site["id"], link_type, src_kind, src["stable_id"], dst_kind,
         dst["stable_id"])).fetchone()[0]


def test_item_link_hidden_when_dst_endpoint_deleted(db):
    s = Seed(db)
    t1, t2 = s.topic("sid" + "a" * 32), s.topic("sid" + "b" * 32)
    question = s.item("question", t1)
    decision = s.item("decision", t2)
    link_id = _item_link(db, s, "resolves", decision, "decision", question, "question")

    # both directions find it while both ends are visible
    assert [r["id"] for r in s.links(question["stable_id"])] == [link_id]
    assert [r["id"] for r in s.links(decision["stable_id"])] == [link_id]
    assert s.links(question["stable_id"], link_types=["resolves"])[0]["id"] == link_id
    assert s.links(question["stable_id"], link_types=["continues"]) == []

    red = s.tombstone(t1)                                    # the dst's topic
    assert s.links(question["stable_id"]) == []
    assert s.links(decision["stable_id"]) == []
    redactions.revert_redaction(db, red["id"], s.co["id"])
    assert [r["id"] for r in s.links(decision["stable_id"])] == [link_id]

    s.tombstone(t2)                                          # the src's topic
    assert s.links(question["stable_id"]) == []


def test_an_endpoint_with_one_live_carrier_stays_visible(db):
    """A carried-forward stable_id names two rows; the superseded one must neither hide
    the link nor crash an endpoint check that assumed one row."""
    s = Seed(db)
    t_old, t_new = s.topic("sid" + "a" * 32), s.topic("sid" + "b" * 32)
    old = s.item("action_item", t_old)
    s.item("action_item", t_new, stable_id=old["stable_id"])
    link_id = s.support("action_item", old)
    db.execute("UPDATE topics SET superseded_at = now() WHERE id = %s", (t_old["id"],))
    assert [r["id"] for r in s.links(old["stable_id"])] == [link_id]


def test_link_types_validation(db):
    s = Seed(db)
    with pytest.raises(ValueError):
        s.links(uuid.uuid4(), link_types=[])
    with pytest.raises(ValueError):
        s.links(uuid.uuid4(), link_types=["caused_by"])


def test_physical_source_delete_cascades_links(db):
    s = Seed(db)
    t, keep = s.topic("sid" + "a" * 32), s.topic("sid" + "b" * 32)
    gone = [s.support(k, s.item(k, t)) for k in event_links.KIND_TABLE]
    kept = s.support("action_item", s.item("action_item", keep))
    assert topics.delete_topics_for_source(db, t["source_s3_key"]) == 1
    assert [_count(db, i) for i in gone] == [0, 0, 0, 0]
    assert _count(db, kept) == 1


def test_site_delete_cascades_links(db):
    s = Seed(db)
    t = s.topic("sid" + "a" * 32)
    a = s.item("action_item", t)
    q = s.item("question", t)
    sup = s.support("action_item", a)
    item_link = _item_link(db, s, "continues", a, "action_item", q, "question")
    db.execute("DELETE FROM sites WHERE id = %s", (s.site["id"],))
    assert _count(db, sup) == 0 and _count(db, item_link) == 0
