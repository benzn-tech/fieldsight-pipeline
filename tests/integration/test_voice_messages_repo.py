from datetime import datetime, timedelta, timezone

import pytest
from repositories import companies, users, sites, voice_messages

pytestmark = pytest.mark.integration


def _seed(db):
    co = companies.create_company(db, "Acme")
    u = users.upsert_user(db, "sub-v", "v@acme.com", company_id=co["id"])
    s = sites.create_site(db, co["id"], "Wharf")
    return co, u, s


def test_insert_and_list_since(db):
    co, u, s = _seed(db)
    m1 = voice_messages.insert_message(db, co["id"], s["id"], u["id"], "voice/a.wav", duration_s=1.5)
    assert m1["s3_key"] == "voice/a.wav" and float(m1["duration_s"]) == 1.5
    m2 = voice_messages.insert_message(db, co["id"], s["id"], u["id"], "voice/b.wav")
    # The db fixture runs each test in ONE transaction, so now() (hence the
    # default created_at) is identical for both inserts. Real sendVoice calls are
    # separate transactions with distinct created_at; simulate that so the strict
    # `created_at > since` backfill filter and the ordering are actually exercised.
    db.execute("UPDATE voice_messages SET created_at = created_at + interval '1 second' WHERE id=%s", (m2["id"],))
    all_msgs = voice_messages.list_since(db, co["id"], s["id"], "1970-01-01T00:00:00Z")
    assert [m["s3_key"] for m in all_msgs] == ["voice/a.wav", "voice/b.wav"]
    after = voice_messages.list_since(db, co["id"], s["id"], m1["created_at"])
    assert [m["s3_key"] for m in after] == ["voice/b.wav"]


def test_list_since_company_and_site_scoped(db):
    co, u, s = _seed(db)
    other = companies.create_company(db, "Other")
    voice_messages.insert_message(db, co["id"], s["id"], u["id"], "voice/a.wav")
    assert voice_messages.list_since(db, other["id"], s["id"], "1970-01-01T00:00:00Z") == []


def test_prune_older_than(db):
    co, u, s = _seed(db)
    m = voice_messages.insert_message(db, co["id"], s["id"], u["id"], "voice/old.wav")
    db.execute("UPDATE voice_messages SET created_at = now() - interval '40 days' WHERE id=%s", (m["id"],))
    voice_messages.insert_message(db, co["id"], s["id"], u["id"], "voice/new.wav")
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    assert voice_messages.prune_older_than(db, cutoff) == 1
    remaining = voice_messages.list_since(db, co["id"], s["id"], "1970-01-01T00:00:00Z")
    assert [m["s3_key"] for m in remaining] == ["voice/new.wav"]
