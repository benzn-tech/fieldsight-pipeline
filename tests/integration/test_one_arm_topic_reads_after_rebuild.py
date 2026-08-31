"""Which topic reads still show a deleted recording the day after the rebuild.

This is a MEASUREMENT written as a test, not a rule invented in advance.

`deleted_predicates.visible_topics_predicate()` carries two arms. The topic arm
matches a topic uuid; the source arm matches `source_s3_key` against a deleted
recording's prefix. `redactions.create_redaction`'s docstring says why both are
needed: `lambda_ingest` re-inserts a superseded day's topics under NEW uuids, so
a topic-keyed tombstone stops matching within a day and only the source arm
survives.

Eight reads in `topics.py` hand-roll the topic arm alone. Whether that is a leak
depends on each one's purpose -- a pipeline probe SHOULD see deleted rows, or it
would re-extract and resurrect them, which is why `tests/unit/test_deleted_read_
paths.py` keeps an EXEMPT list with a reason per entry.

So this file seeds exactly the state that tells them apart -- a topic whose
recording is tombstoned by SOURCE PREFIX and whose uuid no tombstone names --
and records what each read does with it. The unit guard cannot answer this: it
accepts any body containing `scope = 'deleted'`, which the one-arm form has, and
its own comment says "Text is not SQL."

The first run of this file was RED and that was the point: all eight returned
the topic. The list below is the judgement made afterwards, read by read, with
the reason each one is or is not a leak. `get_topic` was the one fixed.

Skipped without TEST_DATABASE_URL. A SKIP IS NOT A PASS -- read the CI run.
"""
import uuid

import pytest

from repositories import companies, redactions, sites, topics, users

pytestmark = pytest.mark.integration

DAY = "2026-08-24"


def _rebuilt_and_source_tombstoned(db):
    """The state the day after a delete: the recording is tombstoned by prefix,
    and the topic carrying that source has a uuid no topic-arm tombstone names.

    Built by tombstoning first and inserting after, which is what the rebuild
    amounts to -- no topic-keyed row is ever written for this uuid.
    """
    tag = uuid.uuid4().hex[:6]
    co = companies.create_company(db, f"Sweep-Co-{tag}")
    site = sites.create_site(db, co["id"], f"Sweep-Site-{tag}")
    user = users.upsert_field_only_user(db, co["id"], f"Sweep_{tag}",
                                        f"Sweep_{tag}", "", "worker")
    source = f"extractions/Sweep_{tag}/{DAY}/sid{'c' * 32}.json"
    redactions.create_recording_tombstone(db, co["id"], source, "removed",
                                          user["id"], "admin")
    t = topics.upsert_topic(db, site["id"], DAY, "Rebuilt after the delete",
                            user_id=user["id"], source_s3_key=source, summary="s")
    return co, site, user, source, t


def test_which_one_arm_reads_still_return_it(db):
    """One assertion per read, collected so CI reports all of them at once
    rather than stopping at the first."""
    co, site, user, source, t = _rebuilt_and_source_tombstoned(db)
    tid, sid, uid, cid = t["id"], site["id"], user["id"], co["id"]
    prefix = source.rsplit("/", 1)[0] + "/"
    base = source.rsplit("/", 1)[1].removesuffix(".json")

    # What each read does with that topic today, and WHY that is the right
    # answer for it. The measurement came first: all eight returned it. The
    # judgement is per read, because "carries both arms" is not automatically
    # correct -- filtering a pipeline read can RESURRECT content, which is
    # exactly what test_deleted_read_paths.EXEMPT already says about
    # `has_topics_for_source`.
    EXPECTED_TO_RETURN_IT = {
        "list_topics_for_source_prefix":
            "the DELETE endpoint's own helper (lambda_org_api ~3866): it is how a "
            "source-prefix delete finds the topics to tombstone. Filter it and the "
            "delete stops finding what it is deleting.",
        "list_extraction_topics_for_day":
            "lambda_ingest's supersession matcher. Hiding a superseded day's topics "
            "from it makes the nightly report fail to match them, and unmatched "
            "extraction topics are re-created -- filtering here resurrects content.",
        "list_contributor_folders_for_site_date":
            "UNJUDGED. Folder names for a site/date (lambda_org_api ~2665). Whether "
            "hiding a deleted contributor's folder is right depends on what the "
            "caller does with the list; not decided tonight, so pinned as-is.",
        "list_extraction_folder_names_for_date":
            "UNJUDGED, same shape (lambda_org_api ~5944).",
        "folders_for_session_base":
            "UNJUDGED, same shape (lambda_org_api ~1595).",
        "list_site_topics":
            "DEAD: no caller anywhere in src/. Left alone rather than deleted -- "
            "another workstream may be mid-flight on it.",
        "list_report_dates":
            "DEAD: no caller anywhere in src/ either; `report_date_counts` carries "
            "both arms and backs the calendar. Same note as above.",
    }

    returns_it = {}

    def check(name, returned):
        returns_it[name] = bool(returned)

    check("get_topic", topics.get_topic(db, tid) is not None)
    check("list_site_topics",
          any(r["id"] == tid for r in topics.list_site_topics(db, sid, DAY)))
    check("list_report_dates",
          str(DAY) in {str(d) for d in topics.list_report_dates(db, [sid], "2026-01-01")})
    check("list_extraction_topics_for_day",
          any(r["id"] == tid
              for r in topics.list_extraction_topics_for_day(db, sid, uid, DAY)))
    check("list_contributor_folders_for_site_date",
          bool(topics.list_contributor_folders_for_site_date(db, sid, DAY)))
    check("list_extraction_folder_names_for_date",
          bool(topics.list_extraction_folder_names_for_date(db, cid, DAY)))
    check("folders_for_session_base",
          bool(topics.folders_for_session_base(db, cid, base)))
    check("list_topics_for_source_prefix",
          any(r["id"] == tid for r in topics.list_topics_for_source_prefix(db, prefix)))

    unexpected = sorted(n for n, r in returns_it.items()
                        if r and n not in EXPECTED_TO_RETURN_IT)
    assert not unexpected, (
        "these reads show a topic whose recording was deleted, once the nightly "
        "rebuild has given it a new uuid: " + ", ".join(unexpected) +
        " -- fix with visible_topics_predicate, or add it to EXPECTED_TO_RETURN_IT "
        "with the reason it must still see deleted rows"
    )

    stopped = sorted(n for n, r in returns_it.items()
                     if not r and n in EXPECTED_TO_RETURN_IT)
    assert not stopped, (
        "these are recorded as needing to SEE deleted rows and no longer do, which "
        "for the two pipeline entries means content can be resurrected: " +
        ", ".join(stopped)
    )
