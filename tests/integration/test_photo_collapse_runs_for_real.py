"""The collapse of multi-bound photos, executed against Postgres.

The script had only ever been read, never run: its SQL (the multi-bound query,
the per-day count, the company lookup) and the rebind it calls meet a real
database for the first time here -- before anyone points it at production.

The seeded day is the prod shape: one photo bound to two topics from two
sessions, the one it belongs to (14:31-14:38) and one six minutes away.
"""
import pytest

import photo_collapse
from repositories import topics

pytestmark = pytest.mark.integration

FOLDER, DATE = "Ben_Lin", "2026-09-11"
PHOTO = f"users/{FOLDER}/pictures/{DATE}/ben_lin_{DATE}_14-37-31.jpg"


class FakeS3:
    def get_paginator(self, name):
        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": PHOTO}] if PHOTO.startswith(Prefix) else []}
        return P()


def _seed(db):
    cid = db.execute("INSERT INTO companies (name) VALUES ('Collapse Co') RETURNING id").fetchone()[0]
    sid = db.execute("INSERT INTO sites (company_id, name) VALUES (%s, 'S') RETURNING id",
                     (cid,)).fetchone()[0]
    right = topics.upsert_topic(db, sid, DATE, "Right", time_range="14:31 - 14:38",
                                source_s3_key=f"extractions/{FOLDER}/{DATE}/sidaaa.json")["id"]
    wrong = topics.upsert_topic(db, sid, DATE, "Six minutes away", time_range="14:44 - 14:45",
                                source_s3_key=f"extractions/{FOLDER}/{DATE}/sidbbb.json")["id"]
    for t in (right, wrong):
        db.execute("INSERT INTO topic_photos (topic_id, s3_key) VALUES (%s, %s)", (t, PHOTO))
    return right, wrong


def _bound_to(db):
    return sorted(str(r[0]) for r in db.execute(
        "SELECT topic_id FROM topic_photos WHERE s3_key = %s", (PHOTO,)).fetchall())


def test_the_dry_run_counts_and_changes_nothing(db):
    right, wrong = _seed(db)
    out = photo_collapse.run(db, FakeS3(), "bucket", apply=False)
    assert out["mode"] == "DRY RUN"
    assert out["multibound_photos_before"] == 1
    assert out["per_day"] == [{"folder": FOLDER, "day": DATE, "multibound_photos": 1,
                               "rows_before": 2, "rows_after": 1}]
    assert _bound_to(db) == sorted([str(right), str(wrong)]), "nothing written"


def test_apply_keeps_the_containing_topic_and_says_so(db):
    right, _ = _seed(db)
    out = photo_collapse.run(db, FakeS3(), "bucket", apply=True)
    assert out["mode"] == "APPLIED"
    assert out["multibound_photos_after"] == 0
    assert _bound_to(db) == [str(right)], "the six-minutes-away row is the one that goes"
