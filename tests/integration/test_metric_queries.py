"""The metric queries, against a real PostgreSQL.

A fake connection records SQL without parsing it, and everything that matters
here is something only a database can answer: that the session fold is a fold,
that a date range built on the s3_key segment is the clock the rest of the
timeline uses, and that a deleted recording stops being counted.

Skipped without TEST_DATABASE_URL. A SKIP IS NOT A PASS — read the CI run.
"""
import pytest

from repositories import companies, recordings, sites, users

pytestmark = pytest.mark.integration


def _seed(db, folder):
    """`upsert_field_only_user` is the helper that takes a folder_name — there is
    no `create_user`, and `upsert_user` keys on a cognito_sub these rows do not
    have. Folder names are globally unique (migration 0012), so every test uses
    its own."""
    co = companies.create_company(db, "Acme-" + folder)
    site = sites.create_site(db, co["id"], "S-" + folder)
    u = users.upsert_field_only_user(db, co["id"], folder, folder, "", "worker")
    return co["id"], site["id"], u["id"]


def _chunk(db, co, site, uid, folder, date, sid, idx, dur=30, kind="audio"):
    """`insert_pending` is the writer; there is no `recordings.create`."""
    return recordings.insert_pending(
        db, co, uid, site, kind,
        f"users/{folder}/{kind}/{date}/dev_{date}_10-00-00_sid{sid}_c{idx:04d}.wav",
        f"{sid}-{idx}", f"{date}T10:00:00Z", duration_s=dur)


def test_a_session_is_one_session_however_many_chunks_it_arrived_in(db):
    """The whole reason this is not COUNT(*). Measured on prod: 2823 rows are 287
    sessions, a fold of 9.8x. Counting rows tells a person they made nearly ten
    times the recordings they made."""
    co, site, uid = _seed(db, "fold")
    for i in range(21):
        _chunk(db, co, site, uid, "fold", "2026-08-27", "a" * 32, i)

    out = recordings.range_stats(db, co, "2026-08-27", "2026-08-27", [site])

    assert out["sessions"] == 1
    assert out["duration_s"] == 21 * 30


def test_a_pre_chunk_recording_is_still_one_session(db):
    """The fold value is the key itself when there is no sid, so legacy one-row
    recordings are unchanged — the fold is the identity for them."""
    co, site, uid = _seed(db, "legacy")
    recordings.insert_pending(
        db, co, uid, site, "audio",
        "users/legacy/audio/2026-08-27/dev_2026-08-27_10-00-00.wav",
        "L-1", "2026-08-27T10:00:00Z", duration_s=600)

    assert recordings.range_stats(db, co, "2026-08-27", "2026-08-27", [site])["sessions"] == 1


def test_the_range_is_matched_on_the_key_segment_not_started_at(db):
    """`started_at` is UTC and "yesterday" is the device's local day, so an
    evening recording moves to the next day — BUG-37's family. The key segment is
    the clock the topics and the timeline are on, and the one
    `query_slots.time_range` produces."""
    co, site, uid = _seed(db, "clock")
    recordings.insert_pending(
        db, co, uid, site, "audio",
        "users/clock/audio/2026-08-27/dev_x_sid" + "b" * 32 + "_c0001.wav",
        "c-1",
        "2026-08-27T23:30:00Z",     # 11:30am the NEXT day in UTC+12
        duration_s=60)

    assert recordings.range_stats(db, co, "2026-08-27", "2026-08-27", [site])["sessions"] == 1
    assert recordings.range_stats(db, co, "2026-08-28", "2026-08-28", [site])["sessions"] == 0


def test_a_range_spans_days(db):
    co, site, uid = _seed(db, "range")
    _chunk(db, co, site, uid, "range", "2026-08-24", "1" * 32, 1)
    _chunk(db, co, site, uid, "range", "2026-08-26", "2" * 32, 1)
    _chunk(db, co, site, uid, "range", "2026-08-30", "3" * 32, 1)

    out = recordings.range_stats(db, co, "2026-08-24", "2026-08-27", [site])
    assert out["sessions"] == 2


def test_a_session_with_no_duration_at_all_is_counted_and_named(db):
    """Measured: 1 of 287 sessions on prod can produce no duration. Dropping it
    from the total understates the answer silently."""
    co, site, uid = _seed(db, "nodur")
    recordings.insert_pending(
        db, co, uid, site, "audio",
        "users/nodur/audio/2026-08-27/dev_sid" + "c" * 32 + "_c0001.wav",
        "n-1", "2026-08-27T10:00:00Z", ended_at=None, duration_s=None)

    out = recordings.range_stats(db, co, "2026-08-27", "2026-08-27", [site])
    assert out["sessions"] == 1
    assert out["duration_s"] == 0
    assert out["unmeasured"] == 1


def test_the_span_is_the_fallback_when_duration_s_is_absent(db):
    """`day_stats` sums `duration_s` only — 97.9% of sessions on prod. The span
    fallback lives in `duration_for_media` and is what takes it to 99.7%. Without
    it here, 5 sessions in 287 contribute zero and the total is quietly short."""
    co, site, uid = _seed(db, "span")
    recordings.insert_pending(
        db, co, uid, site, "audio",
        "users/span/audio/2026-08-27/dev_sid" + "d" * 32 + "_c0001.wav",
        "s-1", "2026-08-27T10:00:00Z",
        ended_at="2026-08-27T10:02:00Z", duration_s=None)

    out = recordings.range_stats(db, co, "2026-08-27", "2026-08-27", [site])
    assert out["duration_s"] == 120
    assert out["unmeasured"] == 0


def test_photos_are_their_own_number_and_do_not_join_the_session_fold(db):
    """Photos are rows in the same table with `kind='photo'`. Mixing them into
    the session count is how the fold ratio was first miscomputed — 304 of the
    3127 rows on prod are photos, and a session count is not over them."""
    co, site, uid = _seed(db, "pix")
    _chunk(db, co, site, uid, "pix", "2026-08-27", "e" * 32, 1)
    for i in range(3):
        recordings.insert_pending(
            db, co, uid, site, "photo",
            f"users/pix/pictures/2026-08-27/IMG_{i}.jpg",
            f"p-{i}", "2026-08-27T10:00:00Z")

    out = recordings.range_stats(db, co, "2026-08-27", "2026-08-27", [site])
    assert out["sessions"] == 1
    assert out["photos"] == 3
    assert out["duration_s"] == 30


def test_a_deleted_session_stops_being_counted(db):
    """The assertion that proves the key-space translation rather than the
    intention. Tombstones live in the `extractions/` space and `recordings` has
    no `source_s3_key` at all, so the predicate used everywhere else matches
    nothing here — and a count that includes deleted recordings is a way to
    observe what was deleted."""
    co, site, uid = _seed(db, "del")
    _chunk(db, co, site, uid, "del", "2026-08-27", "f" * 32, 1)
    _chunk(db, co, site, uid, "del", "2026-08-27", "0" * 32, 1)

    before = recordings.range_stats(db, co, "2026-08-27", "2026-08-27", [site])
    after = recordings.range_stats(db, co, "2026-08-27", "2026-08-27", [site],
                                   deleted_bases={"sid" + "f" * 32})

    assert before["sessions"] == 2
    assert after["sessions"] == 1, "a deleted recording is still being counted"


def test_either_spelling_of_the_deleted_base_works(db):
    """The mirror carries whatever `sessionBase` the delete endpoint had; this
    key is always `sid{hex}`. Two spellings of a session are equal as sessions
    and not as strings."""
    co, site, uid = _seed(db, "spell")
    _chunk(db, co, site, uid, "spell", "2026-08-27", "9" * 32, 1)

    bare = recordings.range_stats(db, co, "2026-08-27", "2026-08-27", [site],
                                  deleted_bases={"9" * 32})
    assert bare["sessions"] == 0


def test_another_companys_recordings_are_never_counted(db):
    """Both the company pin and the site filter are asserted, because either one
    alone looks like it works: a site belongs to exactly one company here, so
    dropping the company_id predicate would still pass a site-scoped test."""
    co_a, site_a, uid_a = _seed(db, "tenanta")
    co_b, site_b, uid_b = _seed(db, "tenantb")
    _chunk(db, co_a, site_a, uid_a, "tenanta", "2026-08-27", "1" * 32, 1)
    _chunk(db, co_b, site_b, uid_b, "tenantb", "2026-08-27", "2" * 32, 1)

    out = recordings.range_stats(db, co_a, "2026-08-27", "2026-08-27",
                                 [site_a, site_b])
    assert out["sessions"] == 1, "a site id reached across tenants"


def test_an_empty_site_list_counts_nothing_rather_than_everything(db):
    """`= ANY('{}')` matches no rows, which is the correct deny-by-default. An
    implementation that skipped the filter when the set is empty would count the
    whole company -- the "empty list means no filter" bug this repo has already
    shipped once, where a failed identity resolution showed MORE, not less."""
    co, site, uid = _seed(db, "empty")
    _chunk(db, co, site, uid, "empty", "2026-08-27", "8" * 32, 1)

    assert recordings.range_stats(db, co, "2026-08-27", "2026-08-27", [])["sessions"] == 0


def test_an_author_filter_narrows_within_the_sites_the_caller_can_reach(db):
    """`author_ids` is `visible_scope`'s per-author allow-set, and `None` is what
    it returns for an ALL- or SITE-scoped caller: no filter. An EMPTY set is a
    filter that matches nobody -- the two must not collapse into each other."""
    co, site, uid = _seed(db, "authfilter")
    other = users.upsert_field_only_user(db, co, "authfilter2", "authfilter2", "", "worker")
    _chunk(db, co, site, uid, "authfilter", "2026-08-27", "3" * 32, 1)
    recordings.insert_pending(
        db, co, other["id"], site, "audio",
        "users/authfilter2/audio/2026-08-27/dev_sid" + "4" * 32 + "_c0001.wav",
        "o-1", "2026-08-27T10:00:00Z", duration_s=30)

    assert recordings.range_stats(db, co, "2026-08-27", "2026-08-27",
                                  [site])["sessions"] == 2
    assert recordings.range_stats(db, co, "2026-08-27", "2026-08-27", [site],
                                  author_ids=[uid])["sessions"] == 1
    assert recordings.range_stats(db, co, "2026-08-27", "2026-08-27", [site],
                                  author_ids=[])["sessions"] == 0


def test_a_recording_with_no_site_is_excluded_but_named(db):
    """Measured live: 87 of 3127 rows have a NULL `site_id`, and on one real day
    that is 28 sessions against 0 in-scope ones. Excluding them is right -- a row
    belonging to no site cannot be shown to someone whose reach IS a set of
    sites. Excluding them silently is not: without `unattributed` that day
    answers "you recorded nothing"."""
    co, site, uid = _seed(db, "nosite")
    _chunk(db, co, site, uid, "nosite", "2026-08-27", "5" * 32, 1)
    recordings.insert_pending(
        db, co, uid, None, "audio",
        "users/nosite/audio/2026-08-27/dev_sid" + "6" * 32 + "_c0001.wav",
        "ns-1", "2026-08-27T10:00:00Z", duration_s=30)

    out = recordings.range_stats(db, co, "2026-08-27", "2026-08-27", [site])
    assert out["sessions"] == 1
    assert out["unattributed"] == 1, "a site-less recording vanished without a word"


def test_a_folder_name_is_not_part_of_the_interface(db):
    """The parameter this function used to take. A folder name arriving from a
    request is an ACL bypass wearing a parameter -- the caller names whose
    recordings to count -- and `recordings.user_id` is NOT NULL (0 of 3127 live)
    and is the same currency `visible_scope` already speaks."""
    import inspect
    params = inspect.signature(recordings.range_stats).parameters
    assert "folders" not in params
    assert "site_ids" in params and "author_ids" in params


from repositories import findings, topics


def test_a_report_sourced_topic_is_counted_through_the_fallback(db):
    """Measured on the live database: 139 topics carry findings and no
    safety_observations, 15 carry safety_observations and no findings, and ZERO
    carry both. The two paths are disjoint, so a findings-only count does not
    under-report by a margin -- it reports nothing at all for those 15."""
    co, site, uid = _seed(db, "fallback")
    t_live = topics.upsert_topic(db, site, "2026-08-27", "Live", user_id=uid,
                                 source_s3_key="extractions/f/2026-08-27/sidx.json")
    findings.insert_findings(db, t_live["id"], site,
                             [{"observation": "loose board", "domain": "safety"}])
    topics.upsert_topic(db, site, "2026-08-27", "Report", user_id=uid,
                        source_s3_key="reports/2026-08-27/f/daily_report.json",
                        safety=[{"observation": "no handrail"}])

    out = findings.count_by_domain(db, co, "safety", "2026-08-27", "2026-08-27")

    assert out["count"] == 2, "the report-path topic was not counted"
    assert out["from_fallback"] == 1


def test_a_topic_with_findings_does_not_double_count_its_legacy_rows(db):
    """The shipped read falls back ONLY when a topic has zero findings IN THIS
    DOMAIN -- `_findings_as_safety_rows(t_findings) or safety_by_topic[...]` in
    topics.py, where the left side is already filtered to domain == 'safety'.
    A topic carrying both must not count twice."""
    co, site, uid = _seed(db, "both")
    t = topics.upsert_topic(db, site, "2026-08-27", "Both", user_id=uid,
                            source_s3_key="extractions/b/2026-08-27/sidy.json",
                            safety=[{"observation": "legacy row"}])
    findings.insert_findings(db, t["id"], site,
                             [{"observation": "current row", "domain": "safety"}])

    out = findings.count_by_domain(db, co, "safety", "2026-08-27", "2026-08-27")
    assert out["count"] == 1
    assert out["from_fallback"] == 0


def test_a_progress_finding_is_not_a_safety_finding(db):
    """The live domain values are exactly progress / quality / safety -- 117 /
    47 / 25. `progress` is the majority, so a query that forgot the domain
    filter would answer a safety question with mostly progress items."""
    co, site, uid = _seed(db, "domains")
    t = topics.upsert_topic(db, site, "2026-08-27", "Mixed", user_id=uid,
                            source_s3_key="extractions/m/2026-08-27/sidm.json")
    findings.insert_findings(db, t["id"], site, [
        {"observation": "slab poured", "domain": "progress"},
        {"observation": "chipped tile", "domain": "quality"},
        {"observation": "no handrail", "domain": "safety"},
    ])

    assert findings.count_by_domain(db, co, "safety", "2026-08-27", "2026-08-27")["count"] == 1
    assert findings.count_by_domain(db, co, "quality", "2026-08-27", "2026-08-27")["count"] == 1


def test_a_quality_question_never_falls_back(db):
    """There is no legacy quality table. The fallback arm is safety-only, and a
    quality count that reused it would report safety rows as quality ones."""
    co, site, uid = _seed(db, "qualnofb")
    topics.upsert_topic(db, site, "2026-08-27", "Legacy", user_id=uid,
                        source_s3_key="reports/2026-08-27/q/daily_report.json",
                        safety=[{"observation": "no handrail"}])

    out = findings.count_by_domain(db, co, "quality", "2026-08-27", "2026-08-27")
    assert out["count"] == 0
    assert out["from_fallback"] == 0


def test_the_tenant_comes_through_site_id_not_through_users(db):
    """`topics.site_id` and `sites.company_id` are both NOT NULL -- one hop that
    loses nothing. Reaching the tenant through `topics.user_id -> users` instead
    drops every NULL-author row from EVERY caller's count, including an
    ALL-scoped admin's, because `topics.user_id` IS nullable."""
    co, site, uid = _seed(db, "tenant")
    t = topics.upsert_topic(db, site, "2026-08-27", "Unattributed", user_id=None,
                            source_s3_key="extractions/t/2026-08-27/sidz.json")
    findings.insert_findings(db, t["id"], site,
                             [{"observation": "x", "domain": "safety"}])

    out = findings.count_by_domain(db, co, "safety", "2026-08-27", "2026-08-27")
    assert out["count"] == 1, "a NULL-author finding vanished from an unscoped count"
    # NOT `null_author == 1` here, which is what this test asserted when it was
    # written and what CI caught: with no author filter the row is IN the count,
    # so naming it implies the number is short when it is complete. The naming
    # behaviour belongs to a scope that genuinely cannot see the row --
    # test_null_author_is_silent_when_every_author_is_in_scope covers both sides.
    assert out["null_author"] == 0


def test_a_self_scoped_caller_is_told_what_they_cannot_see(db):
    """A per-author scope cannot see NULL-author topics by construction. It must
    say so rather than quietly answer a smaller number."""
    co, site, uid = _seed(db, "selfscope")
    mine = topics.upsert_topic(db, site, "2026-08-27", "Mine", user_id=uid,
                               source_s3_key="extractions/s/2026-08-27/sid1.json")
    findings.insert_findings(db, mine["id"], site, [{"observation": "a", "domain": "safety"}])
    orphan = topics.upsert_topic(db, site, "2026-08-27", "Orphan", user_id=None,
                                 source_s3_key="extractions/s/2026-08-27/sid2.json")
    findings.insert_findings(db, orphan["id"], site, [{"observation": "b", "domain": "safety"}])

    out = findings.count_by_domain(db, co, "safety", "2026-08-27", "2026-08-27",
                                   author_ids=[uid])
    assert out["count"] == 1
    assert out["null_author"] == 1


def test_another_companys_findings_are_never_counted(db):
    co_a, site_a, uid_a = _seed(db, "findtena")
    co_b, site_b, uid_b = _seed(db, "findtenb")
    for site, uid in ((site_a, uid_a), (site_b, uid_b)):
        t = topics.upsert_topic(db, site, "2026-08-27", "T", user_id=uid,
                                source_s3_key="extractions/" + str(site) + "/2026-08-27/s.json")
        findings.insert_findings(db, t["id"], site, [{"observation": "x", "domain": "safety"}])

    assert findings.count_by_domain(db, co_a, "safety", "2026-08-27", "2026-08-27")["count"] == 1


def test_a_deleted_topics_findings_are_not_counted(db):
    """`company_id` and `reason` are NOT NULL on `redactions` with no defaults --
    a tombstone that omits them raises rather than hides anything, and a raise
    inside a test that expects zero reads as "the predicate works"."""
    co, site, uid = _seed(db, "deltopic")
    t = topics.upsert_topic(db, site, "2026-08-27", "Gone", user_id=uid,
                            source_s3_key="extractions/d/2026-08-27/sid9.json")
    findings.insert_findings(db, t["id"], site, [{"observation": "x", "domain": "safety"}])
    assert findings.count_by_domain(db, co, "safety", "2026-08-27", "2026-08-27")["count"] == 1

    db.execute("INSERT INTO redactions (company_id, target_type, target_id, reason, scope) "
               "VALUES (%s, 'topic', %s, 'test', 'deleted')", (co, t["id"]))

    assert findings.count_by_domain(db, co, "safety", "2026-08-27", "2026-08-27")["count"] == 0


def test_a_deleted_recording_hides_the_topics_the_pipeline_rebuilds(db):
    """The source arm, which the topic arm cannot cover: tomorrow's re-ingest
    gives the day new topic uuids that no topic-keyed tombstone names, and they
    still carry the deleted recording's `source_s3_key`. A count carrying only
    the topic arm passes every test above and leaks overnight."""
    co, site, uid = _seed(db, "delsource")
    db.execute("INSERT INTO redactions (company_id, target_type, target_id, reason, scope, "
               "target_key) VALUES (%s, 'recording', gen_random_uuid(), 'test', 'deleted', %s)",
               (co, "extractions/delsource/2026-08-27/sidaaa"))
    t = topics.upsert_topic(db, site, "2026-08-27", "Rebuilt", user_id=uid,
                            source_s3_key="extractions/delsource/2026-08-27/sidaaa.json")
    findings.insert_findings(db, t["id"], site, [{"observation": "x", "domain": "safety"}])

    assert findings.count_by_domain(db, co, "safety", "2026-08-27", "2026-08-27")["count"] == 0


def test_the_range_is_inclusive_at_both_ends(db):
    co, site, uid = _seed(db, "findrange")
    for d in ("2026-08-24", "2026-08-26", "2026-08-30"):
        t = topics.upsert_topic(db, site, d, "T", user_id=uid,
                                source_s3_key="extractions/r/" + d + "/s.json")
        findings.insert_findings(db, t["id"], site, [{"observation": "x", "domain": "safety"}])

    assert findings.count_by_domain(db, co, "safety", "2026-08-24", "2026-08-26")["count"] == 2


# ============================================================
# Found by an adversarial review of the deployed feature
# ============================================================

def test_only_the_rows_on_a_reachable_site_are_summed(db):
    """A session's site test is per ROW. `in_scope` asks whether ANY row is on a
    site the caller can reach, and the sums then count only those rows. Summing
    the whole fold once one row qualified reports seconds recorded on a site the
    ACL hides everywhere else in the product.

    No session spans two sites in either live database today; multi-device merge
    groups by session id, which is exactly how one would arrive.
    """
    co, site_a, uid = _seed(db, "twosite")
    site_b = sites.create_site(db, co, "S-twosite-b")["id"]
    sid = "7" * 32
    for i, s in ((1, site_a), (2, site_b), (3, site_b)):
        recordings.insert_pending(
            db, co, uid, s, "audio",
            f"users/twosite/audio/2026-08-27/dev_sid{sid}_c{i:04d}.wav",
            f"ts-{i}", "2026-08-27T10:00:00Z", duration_s=100)

    out = recordings.range_stats(db, co, "2026-08-27", "2026-08-27", [site_a])

    assert out["sessions"] == 1, "the session is reachable through its site-A chunk"
    assert out["duration_s"] == 100, "seconds from a site the caller cannot reach"


def test_a_chunked_session_with_no_duration_spans_the_whole_session(db):
    """`MAX(ended_at - started_at)` is the span of ONE ~30s chunk, so a
    nine-minute session whose rows carry no `duration_s` reported 30 seconds
    while `unmeasured` stayed 0 -- short by 94%, with nothing flagging it. Only
    the one-row legacy case was covered, where the two are equal."""
    co, site, uid = _seed(db, "spanchunks")
    sid = "b" * 32
    for i in range(18):
        recordings.insert_pending(
            db, co, uid, site, "audio",
            f"users/spanchunks/audio/2026-08-27/dev_sid{sid}_c{i:04d}.wav",
            f"sc-{i}", f"2026-08-27T10:{i:02d}:00Z",
            ended_at=f"2026-08-27T10:{i:02d}:30Z", duration_s=None)

    out = recordings.range_stats(db, co, "2026-08-27", "2026-08-27", [site])

    assert out["sessions"] == 1
    # 10:00:00 to 10:17:30 -- the session, not the longest chunk in it.
    assert out["duration_s"] == 1050, "the span was one chunk, not the session"
    assert out["unmeasured"] == 0


def test_a_photo_belonging_to_a_deleted_session_is_still_counted(db):
    """A DOCUMENTED GAP, pinned so nobody reads its absence as coverage.

    A photo key carries no session id, so its fold is the whole key and no
    `sid{hex}` base can equal it. The filter that used to sit on the photo count
    excluded nothing, ever, while reading as a guard. Linking the two is not
    possible from these keys: the tombstone names
    `extractions/{folder}/{date}/sid{hex}` and the photo is
    `users/{folder}/pictures/{date}/IMG_x.jpg`, sharing only a folder and a day.

    If this test ever goes red because a photo IS excluded, that is the gap being
    closed -- update it, do not restore the no-op.
    """
    co, site, uid = _seed(db, "delphoto")
    _chunk(db, co, site, uid, "delphoto", "2026-08-27", "c" * 32, 1)
    recordings.insert_pending(
        db, co, uid, site, "photo",
        "users/delphoto/pictures/2026-08-27/IMG_1.jpg",
        "dp-1", "2026-08-27T10:00:00Z")

    out = recordings.range_stats(db, co, "2026-08-27", "2026-08-27", [site],
                                 deleted_bases={"sid" + "c" * 32})

    assert out["sessions"] == 0, "the audio session IS excluded"
    assert out["photos"] == 1, "the photo is not, and this is known"


def test_null_author_is_silent_when_every_author_is_in_scope(db):
    """`null_author` is what a per-author scope cannot see. With no author filter
    those rows are IN the count, and printing "2 items sit on notes with no
    recorded author" beside a complete number implies it is short when it is
    not."""
    co, site, uid = _seed(db, "nullauth")
    t = topics.upsert_topic(db, site, "2026-08-27", "Orphan", user_id=None,
                            source_s3_key="extractions/na/2026-08-27/sid1.json")
    findings.insert_findings(db, t["id"], site, [{"observation": "x", "domain": "safety"}])

    unfiltered = findings.count_by_domain(db, co, "safety", "2026-08-27", "2026-08-27")
    assert unfiltered["count"] == 1
    assert unfiltered["null_author"] == 0, "a complete number was given a caveat"

    filtered = findings.count_by_domain(db, co, "safety", "2026-08-27", "2026-08-27",
                                        author_ids=[uid])
    assert filtered["count"] == 0
    assert filtered["null_author"] == 1, "a short number lost its reason"


def test_a_cross_company_caller_counts_the_sites_they_reach(db):
    """A `platform_admin` reaches every site in every company, and their OWN
    company usually owns none of them -- on TEST the platform company owns zero
    sites while all the data is in another.

    `company_id` pinned to the caller's own company then contradicted the site
    set and matched nothing: a platform_admin reaching 5 sites that recorded all
    day was told "There are notes on 2026-08-13, but no recording data was
    registered for it." `has_topics_in_range` scopes by site alone, which is why
    the topic half of that sentence was right and the count was zero.

    `site_ids` IS the ACL, so it is the scope. For every ordinary role it already
    comes from memberships or list_company_sites and sits inside the caller's own
    company, so this is not a widening.
    """
    co_data, site, uid = _seed(db, "xcodata")
    co_admin = companies.create_company(db, "Acme-xcoadmin")["id"]
    _chunk(db, co_data, site, uid, "xcodata", "2026-08-27", "a" * 32, 1)

    out = recordings.range_stats(db, co_admin, "2026-08-27", "2026-08-27", [site])
    assert out["sessions"] == 1, "a cross-company caller saw none of the site they reach"
    assert out["duration_s"] == 30


def test_another_companys_site_less_recordings_stay_out_of_unattributed(db):
    """The company pin survives on the site-less arm, and it has to. A NULL-site
    row is reachable by no site set at all, so without it a cross-company caller
    would fold another company's unattributed recordings into their own note."""
    co_a, site_a, uid_a = _seed(db, "xcoa")
    co_b, site_b, uid_b = _seed(db, "xcob")
    recordings.insert_pending(
        db, co_b, uid_b, None, "audio",
        "users/xcob/audio/2026-08-27/dev_sid" + "b" * 32 + "_c0001.wav",
        "xb-1", "2026-08-27T10:00:00Z", duration_s=30)

    out = recordings.range_stats(db, co_a, "2026-08-27", "2026-08-27", [site_a, site_b])
    assert out["unattributed"] == 0, "another company's site-less recording was named"


def test_findings_follow_the_same_rule(db):
    co_data, site, uid = _seed(db, "xcofind")
    co_admin = companies.create_company(db, "Acme-xcofindadmin")["id"]
    t = topics.upsert_topic(db, site, "2026-08-27", "T", user_id=uid,
                            source_s3_key="extractions/x/2026-08-27/s.json")
    findings.insert_findings(db, t["id"], site, [{"observation": "x", "domain": "safety"}])

    assert findings.count_by_domain(db, co_admin, "safety", "2026-08-27", "2026-08-27",
                                    site_ids=[site])["count"] == 1


def test_without_a_site_set_the_company_is_still_the_scope(db):
    """The fallback the CASE keeps. `count_by_domain` is callable with no
    site_ids, and dropping the company pin outright would have made that call
    count every company."""
    co_a, site_a, uid_a = _seed(db, "nositea")
    co_b, site_b, uid_b = _seed(db, "nositeb")
    for site in (site_a, site_b):
        t = topics.upsert_topic(db, site, "2026-08-27", "T", user_id=None,
                                source_s3_key=f"extractions/{site}/2026-08-27/s.json")
        findings.insert_findings(db, t["id"], site, [{"observation": "x", "domain": "safety"}])

    out = findings.count_by_domain(db, co_a, "safety", "2026-08-27", "2026-08-27")
    assert out["count"] == 1, "with no site set, the company pin is what scopes it"
