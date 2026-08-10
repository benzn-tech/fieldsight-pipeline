"""
Lambda: fieldsight-item-writer v1.0 — realtime extraction ingestion (Phase 4b).

In-VPC (psycopg direct to Aurora; mirrors lambda_ingest's VPC/PG pattern).
Reads one `extractions/{user_folder}/{date}/{session_base}.json` written by
lambda_extract_session (the session-extraction JSON contract -- see that
module's docstring and docs/superpowers/plans/2026-07-07-phase-4b-realtime.md
"Global Constraints"), resolves site/user via the SAME identity bridge as
Phase 4a's nightly ingest, scope-deletes the prior write for that extraction
key, then re-inserts topics (with action_items/safety_observations children).

The identity bridge, topic-child-shape mapping, and the "no seeded company"
guard are REUSED from lambda_ingest by import -- never copied:
  lambda_ingest.resolve_site / resolve_user / _map_action_items / _map_safety
  and the same RuntimeError message on a missing companies row.

Site resolution note: the extraction JSON has no 'site' field (unlike a
daily_report.json, which may carry report['site']) -- declared_site is only
ever stored for record in the extraction JSON, it is NOT consumed for site
attribution here. resolve_site is always called with an empty report dict,
which falls straight through to the user_mapping.json primary_site slug
bridge. A double miss (report has no site AND the mapping bridge also
misses) skips the extraction, zero writes -- exactly like lambda_ingest's
report-level site-bridge miss.

G5b: recordings.site_for_media (the app-tagged site, keyed on the
recording's own session_base) is now consulted FIRST and, when present,
overrides the membership resolver above -- resolve_site is only the
fallback when there is no matching tag.

Idempotency: keyed on source_s3_key = the extraction's own S3 key (delete
then re-insert) -- same source-key idempotency Phase 4a topics/chunks use,
so re-processing the same extraction (e.g. a re-triggered S3 event, or a
later session segment landing and re-writing the same extractions/ key)
never duplicates rows.

Entry point (event shape):
  - S3 event: {"Records": [{"s3": {"object": {
        "key": "extractions/<User_Folder>/<date>/<session_base>.json"}}}]}
    S3 event notifications encode spaces as '+' and other special chars as
    %XX -- the key is ALWAYS unquote_plus'd before use.

Environment Variables:
    S3_BUCKET     - S3 bucket name (the data lake -- IngestBucketName)
    CONFIG_KEY    - S3 key for user/site mapping (default: config/user_mapping.json,
                    read indirectly via lambda_ingest.load_mapping's own env var)
    COMPANY_NAME  - default: FieldSight (mirrors lambda_ingest's default)
    PG*/DATABASE_URL - read by db.connection.get_connection()
"""
import json
import logging
import os
from urllib.parse import unquote_plus

import boto3

import lambda_ingest
import keyframe_request
import match_request
from db.connection import get_connection
from keyframe_selection import keyframe_seconds
from photo_binding import PHOTOS_PER_TOPIC_CAP  # noqa: F401  (re-export)
from photo_binding import list_pictures as _pb_list_pictures
from repositories import users as users_repo
from photo_binding import photos_for_topics as _photos_for_topics
import thread_match
from repositories import (companies, findings, meeting_session, recordings,
                          session_group, sites, threads, topics)
# The extraction-key shape lives in session_scope now (the read side needs the
# SAME parse to derive session_id from topics.source_s3_key -- see that
# module). Re-exported under the historical private names so existing callers
# and tests keep working; same extraction pattern photo_binding/
# keyframe_selection already followed.
from session_scope import EXTRACTION_KEY_RE  # noqa: F401  (re-export)
from session_scope import device_session_id as _device_session_id
from session_scope import parse_extraction_key as _parse_extraction_key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
CONFIG_KEY = os.environ.get("CONFIG_KEY", "config/user_mapping.json")
COMPANY_NAME = os.environ.get("COMPANY_NAME", "FieldSight")

# video-keyframe plan: ship the pipeline change inert -- only when
# EnableKeyframes flips this env true does item-writer emit keyframe_requests/.
EMIT_KEYFRAME_REQUESTS = os.environ.get("EMIT_KEYFRAME_REQUESTS", "false").lower() == "true"
# Propose which earlier subject a new topic is a restatement of. Off by
# default so the write path ships inert: this only ever writes rows to
# topic_thread_suggestions, which nothing reads yet.
SUGGEST_THREADS = os.environ.get("SUGGEST_THREADS", "false").lower() == "true"

EXTRACTIONS_PREFIX = "extractions/"

_s3_client = None


def s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _site_from_meeting_session(conn, company_id, session_base):
    """The site the recorder picked when OPENING a chunk session
    (meeting_session.site_id, set by POST /sessions/{id}/open), as a
    sites.get_site()-shaped row -- else None. This is how a chunk session
    attributes to a site: it uploads its ~1-min chunks straight to the raw-media
    prefix (no `recordings` row, so recordings.site_for_media misses), and its
    only explicit site tag lives on meeting_session. Returns None for a legacy
    whole-file base (no device session). Company-scoped: session_open already
    rejected a cross-tenant site, and we re-check the resolved site's company here
    so a stale/rogue row can never attribute across tenants (multi-tenant
    invariant, mirrors recordings.site_for_media)."""
    device_sid = _device_session_id(session_base)
    if not device_sid:
        return None
    row = meeting_session.get(conn, device_sid)
    if not row or not row.get("site_id"):
        return None
    site = sites.get_site(conn, row["site_id"])
    if site is None or site["company_id"] != company_id:
        return None
    return site


def _group_id_from_base(session_base):
    """The group id inside a MERGED artifact's base (`grp{32hex}`), else None."""
    if not session_base or not session_base.startswith("grp"):
        return None
    return session_base[3:] or None


def _site_from_group_lead(conn, company_id, session_base):
    """A merged artifact's site, taken from the LEAD's session row.

    Needed because the merged key deliberately is NOT a `sid` base (that one
    collides with the lead's own final pass and would be overwritten), so every
    existing rung of the ladder misses it: recordings.site_for_media matches on
    the media filename, _site_from_meeting_session's device_session_id only
    recognises `sid`, and an admin/gm lead has no recordings row for the day.
    Without this rung a merge ends in "identity bridge miss ... zero writes" --
    silently discarded AFTER the members' topics were deleted.

    Company-scoped exactly as _site_from_meeting_session is, so a stale or rogue
    row can never attribute across tenants."""
    gid = _group_id_from_base(session_base)
    if not gid:
        return None
    row = meeting_session.get(conn, gid)
    if not row or not row.get("site_id"):
        return None
    site = sites.get_site(conn, row["site_id"])
    if site is None or str(site["company_id"]) != str(company_id):
        return None
    return site


ENABLE_GROUP_MERGE = os.environ.get("ENABLE_GROUP_MERGE", "false").lower() == "true"
GROUP_MERGE_CAP = int(os.environ.get("GROUP_MERGE_CAP", "2"))


def _group_for_session(conn, session_base):
    """The group-merge state row for a `sid` base's group, or None."""
    sid = _device_session_id(session_base)
    if not sid:
        return None
    row = meeting_session.get(conn, sid)
    if not row:
        return None
    gid = row.get("group_id") or sid      # a lead carries no group_id of its own
    return session_group.get(conn, gid)


def _read_merged_artifact(key):
    """The merged extraction the group published, or None if unreadable."""
    if not key:
        return None
    import boto3
    try:
        obj = boto3.client("s3").get_object(Bucket=S3_BUCKET, Key=key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except Exception:
        logger.warning("could not read merged artifact %s", key)
        return None


def _group_supersedes_solo(conn, session_base, extraction):
    """Should this SOLO extraction be written, given its group's merge state?

    Returns "write" or "suppress".

    Once the group has merged, writing a member's own topics reintroduces
    exactly the duplicate the merge removed -- and this is not an edge case: the
    sweep requests each member's final pass BEFORE the merge runs, so a
    lead-solo final routinely lands afterwards.

    A member that brings genuinely NEW transcripts is different. It is not
    dropped; it re-arms the group so the next standing scan merges again and
    everyone gets an updated record. Capped, because a device drip-feeding
    chunks would otherwise re-merge and re-email all day -- and past the cap the
    content is WRITTEN rather than lost, so only its inclusion in the merged
    record is given up, never the content itself."""
    if _group_id_from_base(session_base):
        return "write"                     # the merged artifact itself
    row = _group_for_session(conn, session_base)
    if not row or not row.get("merged_at"):
        return "write"                     # no group, or not merged yet
    merged = _read_merged_artifact(row.get("merged_key"))
    if not _brings_new_content(extraction, merged):
        logger.info("group %s: %s adds nothing the merge did not see -- suppressed",
                    row["group_id"], session_base)
        return "suppress"
    if (row.get("merge_count") or 0) >= GROUP_MERGE_CAP:
        logger.warning(
            "group %s: merge cap reached (%d); writing %s as solo topics instead "
            "of re-merging -- its content is kept, its place in the merged "
            "record is not", row["group_id"], row.get("merge_count"), session_base)
        return "write"
    if session_group.rearm(conn, row["group_id"]):
        logger.info("group %s: %s brought new content -- re-armed for another merge",
                    row["group_id"], session_base)
    return "suppress"


def _delete_member_topics(conn, artifact, delete=None):
    """Remove each member's solo topics so the merged set is the only record.

    A zero rowcount is logged loudly. The delete is keyed on source_s3_key and
    delete_topics_for_source returns a count rather than raising, so a key that
    differs by one character (a date derived in UTC instead of NZ, say) removes
    nothing and leaves exactly the duplicate this whole feature exists to
    eliminate -- with no error anywhere to notice."""
    delete = delete or topics.delete_topics_for_source
    for key in artifact.get("mergedMembers") or []:
        n = delete(conn, key)
        if not n:
            logger.warning(
                "group %s: %s removed 0 topics -- that member's solo items will "
                "now duplicate the merged record", artifact.get("groupId"), key)


def _brings_new_content(solo, merged):
    """Does this solo extraction hold a transcript the merge did not see?

    COVERAGE, not timing. "Anything written after the merge" would fire on the
    lead's own final pass -- which the sweep requested BEFORE the merge ran --
    so every group would re-merge and re-email once in the completely ordinary
    case, and the cap would be spent before a genuinely late device arrived.

    An unreadable merged artifact counts as covering nothing: erring towards a
    wasted re-merge (costs an email) rather than towards dropping a late
    device's content (costs the content)."""
    if not isinstance(merged, dict):
        return True
    return not set((solo or {}).get("source_transcripts") or []).issubset(
        set(merged.get("source_transcripts") or []))


def _enqueue_updated_emails(artifact, put=None):
    """One finalize request per member, all quoting ONE summary.

    The summary rides in the artifact rather than being rebuilt per member:
    lambda_session_finalize re-derives its own from that member's SOLO
    transcripts, so N members would otherwise receive N different bodies -- the
    opposite of "every member gets identical content" -- at the cost of N LLM
    calls for one meeting.

    Keyed `-updated` so the worker's result cannot be mistaken by the finalize
    sweep's reconcile for that member's solo outcome. A member can be counted
    settled by quietness while still `finalizing`, so the two would otherwise
    race on session_finalize_results/{sessionId}.json."""
    put = put or _put_finalize_request
    todos = _todos_from_topics(artifact)
    for sid in artifact.get("memberSessions") or []:
        put(f"session_finalize_requests/{sid}-updated.json",
            {"kind": "updated", "sessionId": sid,
             "groupId": artifact.get("groupId"),
             "summary": artifact.get("summary"),
             "openTodos": todos})


def _evidence_payload(topic):
    """The citations plus the topic's rolled-up status, as one jsonb object.

    An object rather than the bare array the extraction produces, because the
    column has to distinguish three states and an array can only carry two:

      NULL                        never measured (pre-feature, or flag off)
      {"status": "absent", ...}   measured; the model cited nothing
      {"status": "verified", ...} measured; here is what it cited

    Returning None for an unmeasured topic is what keeps historical rows from
    reading as uncited.
    """
    quotes = topic.get("evidence")
    status = topic.get("evidence_status")
    if quotes is None and status is None:
        return None
    return {"status": status, "quotes": quotes or []}


def _todos_from_topics(artifact):
    """The merged record's action items, in the shape the email renderer wants.

    _clean_todos expects {text, responsible, due} dicts, not strings — a list of
    strings raises AttributeError and takes the whole email with it. The solo
    path gets this shape from the rolling summary; a merged artifact has no
    rolling summary at all, so it is built from the merged topics' action_items
    here."""
    out = []
    for topic in artifact.get("topics") or []:
        for item in topic.get("action_items") or []:
            if not isinstance(item, dict):
                continue
            text = (item.get("action") or item.get("text") or "").strip()
            if text:
                out.append({"text": text,
                            "responsible": item.get("responsible") or None,
                            "due": item.get("deadline") or item.get("due") or None})
    return out


def _put_finalize_request(key, body):
    import boto3
    boto3.client("s3").put_object(
        Bucket=S3_BUCKET, Key=key,
        Body=json.dumps(body, ensure_ascii=False),
        ContentType="application/json")


# ----------------------------------------------------------
# Task 3 (authority-flip plan) -- time-correlated photo attach.
#
# P2 (2026-07-23 prod-media-binding plan): the matcher and the pictures
# lister now live in photo_binding, shared with lambda_ingest's report path
# (P4) -- a direct import of THIS module from lambda_ingest would be
# circular, since this module imports lambda_ingest for the identity
# bridge. The rule also changed there: strict containment against the
# topic's time_range stranded every prod photo by 1-2 minutes
# (topic_photos: 0 rows across all of prod history). 2026-07-24 correction:
# binding is bounded-tolerance (inside the window, or within
# PHOTO_TOLERANCE_MIN=2 min of an edge; beyond that, no binding at all --
# the never-orphan fallback was removed) -- see photo_binding's docstring.
# The aliases below keep the historical private names importable for
# existing callers and tests.
# ----------------------------------------------------------

def _list_pictures(prefix):
    """Pictures listing bound to THIS module's S3 client + bucket (the
    shared lister is client-parameterized so lambda_ingest can reuse it)."""
    return _pb_list_pictures(s3(), S3_BUCKET, prefix)


# ----------------------------------------------------------
# Per-extraction write (commit-per-extraction: one `with get_connection()` here)
# ----------------------------------------------------------
def _suggest_threads(conn, site_id, date, written):
    """Propose, for each topic just written, which earlier subject it is a
    restatement of.

    PROPOSES. Nothing here sets topics.thread_id -- that only happens when a
    human confirms, because a wrong link silently closes or escalates the
    wrong work and nobody finds out.

    Runs inside the caller's transaction and inside the VPC, which is why the
    matcher is lexical: this lambda has no outbound network (CLAUDE.md
    BUG-36), and string maths over rows already in the database needs none.

    Never fatal, and the SAVEPOINT is what makes that true rather than
    aspirational: catching a database error in Python does NOT un-abort the
    transaction it happened in -- Postgres refuses every later statement, so
    the commit fails and the topics and findings this transaction just wrote
    are lost. A bare try/except here would have silently traded the day's
    real content for an optional suggestion. `conn.transaction()` nested
    inside the caller's transaction issues a SAVEPOINT, so a failure unwinds
    only this pass."""
    try:
        with conn.transaction():
            return _suggest_threads_inner(conn, site_id, date, written)
    except Exception:
        logger.exception("thread suggestion pass failed; topics were written")
        return 0


def _suggest_threads_inner(conn, site_id, date, written):
    corpus = threads.candidate_corpus(conn, site_id, date,
                                      thread_match.MAX_GAP_DAYS)
    if not corpus:
        # Log the silence. The first prod run of this wrote 8 topics and
        # emitted NOTHING, because the early return sat above the only log
        # line -- leaving "the flag is off", "there was nothing to compare
        # against" and "it threw and was swallowed" indistinguishable from
        # the outside. Three states, one empty log, and the only way to tell
        # them apart was to query the database by hand.
        logger.info("thread suggestions: no candidates within %dd for site=%s %s",
                    thread_match.MAX_GAP_DAYS, site_id, date)
        return 0
    made = 0
    for t in written:
        new_topic = {
            "id": t["topic_id"], "report_date": date, "site_id": site_id,
            "title": t.get("title"), "summary": t.get("summary"),
            "open_items": t.get("open_items") or 0,
        }
        # The new topic joins the corpus for IDF only: a word's weight
        # should account for the document being scored, and on a small
        # site's corpus leaving it out visibly skews the rarity of its
        # own vocabulary. find_candidates skips it as a candidate.
        hits = thread_match.find_candidates(new_topic, list(corpus) + [new_topic])
        if not hits:
            continue
        best = hits[0]
        # Join the parent's thread if it has one; otherwise anchor a new
        # thread on the parent itself. Exactly one of these, which the
        # table's CHECK enforces.
        if best.get("thread_id"):
            row = threads.upsert_suggestion(
                conn, t["topic_id"], thread_id=best["thread_id"],
                score=best["match_score"], gap_days=best["gap_days"])
        else:
            row = threads.upsert_suggestion(
                conn, t["topic_id"], parent_topic_id=best["id"],
                score=best["match_score"], gap_days=best["gap_days"])
        if row is not None:
            made += 1
    logger.info("thread suggestions: %d proposed over %d candidates",
                made, len(corpus))
    return made


# The model has no name for the person holding the recorder -- the transcript
# never says it -- so it writes "Speaker". That is fine as a placeholder and
# useless in a report: a task cannot be assigned to "Speaker".
#
# We DO know who it is: the recording belongs to an account. The resolution is
# gated on the session having exactly ONE voice, because that is the only case
# where "the speaker" is unambiguous. With two or more, mapping it to the
# account holder would be a guess, and a guess printed as a name reads as a
# fact -- the same failure that made mentioned people into participants.
_SELF_REFERENTIAL = {
    "speaker", "the speaker", "speaker 1", "spk_0",
    "me", "myself", "self", "i", "the recorder", "narrator", "the narrator",
}


def _display_name(user_row, fallback):
    """First+last, or the folder name when the row has neither.

    Built with an explicit filter+strip rather than a concatenation: a NULL
    last_name once produced "Ben_UCPK " with a trailing space, which then
    became a folder that did not exist (see the display-name trailing-space
    incident).
    """
    if not user_row:
        return fallback
    parts = [(user_row.get("first_name") or "").strip(),
             (user_row.get("last_name") or "").strip()]
    return " ".join(p for p in parts if p) or fallback


def _resolve_self_responsible(action_items, name):
    """Replace a self-referential `responsible` with the recorder's name.

    Returns how many were resolved, for the log — a silent rewrite of a
    user-facing field is not something to do without saying so.
    """
    resolved = 0
    for item in action_items or []:
        if not isinstance(item, dict):
            continue
        value = (item.get("responsible") or "").strip()
        if value and value.lower() in _SELF_REFERENTIAL:
            item["responsible"] = name
            resolved += 1
    return resolved


def write_extraction_items(date, user_folder, extraction_key):
    raw = s3().get_object(Bucket=S3_BUCKET, Key=extraction_key)["Body"].read()
    extraction = json.loads(raw.decode("utf-8"))

    with get_connection() as conn:
        # I-3: serialize concurrent writers on this extraction key. Delete-
        # then-insert is not concurrency-safe on its own (two overlapping
        # invocations for the same key could interleave their delete/insert
        # pairs), and upsert_topic is INSERT-only (no ON CONFLICT dedup) --
        # an xact-scoped advisory lock keyed on the extraction key forces
        # concurrent writers for the SAME key to run one at a time.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (extraction_key,))

        # I-4: Fargate next-evening catch-up downloads can produce a session
        # extraction that lands AFTER that day's nightly report has already
        # been ingested. Without this guard a late-landing extraction would
        # re-insert topics with no future supersession ever coming --
        # permanently-dangling live rows alongside the authoritative report.
        # Post authority-flip (Task 7, spec §6): once AUTHORITY_FLIP defers
        # for a day, lambda_ingest stops writing report topics for it, so
        # report topics only exist for zero-extraction fallback days; this
        # guard keeps that rare day duplicate-free.
        report_source_key = f"reports/{date}/{user_folder}/daily_report.json"
        report_already_ingested = conn.execute(
            "SELECT 1 FROM topics WHERE source_s3_key=%s LIMIT 1",
            (report_source_key,),
        ).fetchone()
        if report_already_ingested is not None:
            reason = "nightly report already ingested — late session extraction superseded"
            logger.info("%s: %s", extraction_key, reason)
            return {"skipped": True, "reason": reason}

        company = lambda_ingest.resolve_company(conn, user_folder)
        if company is None:
            # Same guard + message as lambda_ingest.ingest_report (Fable
            # minor 6): an unseeded org DB would otherwise surface as an
            # opaque 'NoneType' subscript error on every extraction.
            raise RuntimeError(
                f"org company {COMPANY_NAME!r} not found — run the org seed "
                "(fieldsight-*-org-seed) before ingesting")

        # Site attribution, in priority order:
        #   1. recordings.site_for_media -- G5b: the app stamps the in-app project
        #      pick onto recordings.site_id for a WHOLE-FILE upload. Authoritative
        #      over membership, and the only way an admin recording (resolve_site
        #      returns None for ALL scope) attributes.
        #   2. meeting_session.site_id -- a CHUNK session uploads its ~1-min chunks
        #      straight to the raw-media prefix (no recordings row -> #1 misses), so
        #      its explicit site pick lives on meeting_session via POST /sessions/
        #      {id}/open. Without this, every chunk-session recording identity-bridge
        #      missed and never reached the web timeline.
        #   3. recordings.site_for_day -- the day's app-tagged site (majority of
        #      that user's recordings). Covers the gap #1 and #2 both leave for a
        #      CHUNK session recorded OFFLINE: #1's LIKE pattern wants the file to
        #      BE `{session_base}.ext`, but a chunk file is
        #      `{user}_{ts}_sid{id}_c{NNNN}.wav`, so it never matches; and #2 needs
        #      meeting_session.site_id, which is NULL whenever the device's
        #      POST /sessions/{id}/open could not reach the server at record time
        #      (the session then gets opened by chunk-stream inference, which
        #      carries no site). The recordings rows still carry the correct
        #      site_id all along -- CLAUDE.md BUG-41's rule is that the app's
        #      recordings.site_id is the authority, so this ranks ABOVE membership.
        #   3b. meeting_session.site_id of the group LEAD -- a MERGED artifact's
        #      base is `grp{gid}`, not `sid{...}`, so #2 misses it by
        #      construction (device_session_id only recognises `sid`), #1's LIKE
        #      never matches, and an admin/gm lead has no recordings row. Every
        #      rung would miss and the merge would end in "zero writes" AFTER
        #      the members' topics were already deleted.
        #   4. resolve_site -- legacy recorder-membership resolver. Last, and it
        #      deliberately returns None for admin/gm (ALL scope, no single home
        #      site), which is why an offline gm recording used to fall all the way
        #      through to "identity bridge miss ... zero writes" and never reach
        #      the web timeline even though every upload had succeeded.
        # All three explicit tags are company-scoped; fall through only on no match.
        session_base = _parse_extraction_key(extraction_key)[2]

        # A member whose group has already merged must not re-publish its own
        # topics: that reintroduces the duplicate the merge removed. Checked
        # BEFORE any site work — the answer does not depend on it, and doing it
        # first keeps a suppressed pass cheap.
        if ENABLE_GROUP_MERGE and _group_supersedes_solo(
                conn, session_base, extraction) == "suppress":
            return {"skipped": True, "reason": "superseded by the group merge"}

        site = recordings.site_for_media(conn, company["id"], user_folder, date, session_base) \
            or _site_from_meeting_session(conn, company["id"], session_base) \
            or _site_from_group_lead(conn, company["id"], session_base) \
            or recordings.site_for_day(conn, company["id"], user_folder, date) \
            or lambda_ingest.resolve_site(conn, company["id"], {}, user_folder)
        if site is None:
            reason = (f"identity bridge miss: user_folder={user_folder!r} -- "
                      f"skipping extraction, zero writes")
            logger.warning("%s: %s", extraction_key, reason)
            return {"skipped": True, "reason": reason}

        user_id = lambda_ingest.resolve_user(conn, company["id"], user_folder)

        # Only when the ASR heard exactly one voice. Absent (older artifacts) is
        # treated as "unknown", not as one -- an unknown count must not license
        # putting a name on someone else's words.
        if extraction.get("speaker_count") == 1:
            recorder = _display_name(
                users_repo.get_by_folder_name(conn, company["id"], user_folder), user_folder)
            resolved = sum(_resolve_self_responsible(t.get("action_items"), recorder)
                           for t in extraction.get("topics", []))
            if resolved:
                logger.info("%s: resolved %d self-referential responsible -> %r",
                            extraction_key, resolved, recorder)

        # Source-key idempotency (Phase 4a pattern): clear this extraction's
        # prior rows before re-inserting.
        topics.delete_topics_for_source(conn, extraction_key)

        # A MERGED artifact additionally supersedes each member's own topics.
        # BEFORE the writes below, never after: this key's own rows were just
        # cleared, and deleting afterwards would take the merged set with them
        # if a member key ever equalled this one.
        # ...but only when the merge actually produced something. An artifact
        # with no topics would otherwise delete every member's record and write
        # nothing in its place: on the website the meeting simply empties. The
        # S3 extractions survive, so it is recoverable by hand, but nobody would
        # know to look -- merge_result stays NULL (it is gated on topics_n
        # below), so the group reads as still-in-flight rather than as damage.
        if extraction.get("tier") == "group" and extraction.get("topics"):
            _delete_member_topics(conn, extraction)

        # Task 3 (authority-flip plan) -- list the pictures prefix ONCE per
        # invocation (paginator, outside the per-topic loop below), then
        # pure-match photos to topics by time_range before the loop uses it.
        pictures_prefix = f"users/{user_folder}/pictures/{date}/"
        photo_objects = _list_pictures(pictures_prefix)
        extraction_topics = extraction.get("topics", [])
        photos_by_topic = _photos_for_topics(photo_objects, extraction_topics)

        topics_n = 0
        collected_topics = []
        keyframe_topics = []  # video-keyframe plan: {topic_id, time_range} of gate-passers
        for i, t in enumerate(extraction_topics):
            mapped_action_items = lambda_ingest._map_action_items(t.get("action_items"), date)
            matched_photos = photos_by_topic.get(i, [])
            # Sanitize work_class/work_confidence before the upsert (Fable
            # review #7): the columns carry CHECK constraints (work_class IN
            # ('work','non_work'); work_confidence is real) so a raw bad LLM
            # value (e.g. "personal", or a non-numeric confidence) would
            # raise inside this transaction and abort the whole session's
            # topics/findings write. Invalid -> NULL (legacy/unclassified,
            # which enforcement treats as work).
            _wc = t.get("work_class")
            _wc = _wc if _wc in ("work", "non_work") else None
            try:
                _wconf = float(t["work_confidence"]) if t.get("work_confidence") is not None else None
            except (TypeError, ValueError):
                _wconf = None
            row = topics.upsert_topic(
                conn, site["id"], date, t.get("topic_title", ""),
                user_id=user_id, source_s3_key=extraction_key,
                category=t.get("category"), summary=t.get("summary"),
                action_items=mapped_action_items,
                # Phase F Task 23 (D8 retirement, spec §8): no `safety=` kwarg
                # here anymore -- findings.insert_findings below is now the
                # ONLY Aurora write for this topic's safety data, so
                # upsert_topic's own safety_observations INSERT loop never
                # fires. t['safety_flags'] (still derived by lambda_extract_
                # session._derive_safety_flags) is intentionally left
                # untouched in the extraction JSON -- chunking.py and
                # lambda_ask_agent.py still read it for RAG embedding text;
                # only this Aurora dual-write is stopped. safety_observations
                # the TABLE stays in place, unread by this writer, for
                # rollback.
                time_range=t.get("time_range"), participants=t.get("participants"),
                work_class=_wc, work_confidence=_wconf, is_mixed=(t.get("is_mixed") is True),
                evidence=_evidence_payload(t),
                # video-keyframe plan (Task 4): re-bound synthetic keyframes
                # (filename carries the '_kf_' marker) keep an "Auto keyframe"
                # caption so the UI can still distinguish them after an
                # item-writer re-run; real photos stay caption-less (None).
                photos=[{"s3_key": p["key"],
                         "caption_text": "Auto keyframe" if "_kf_" in p["filename"] else None}
                        for p in matched_photos],
            )
            # Task 2 (programme-impact-link plan) -- persist this topic's
            # rich extraction findings in the SAME transaction as the topic
            # upsert (inherits the I-3 advisory lock + I-4 supersession
            # guard already established above). Legacy extraction JSON with
            # no 'findings' key (pre-#46 extractions still in S3, and the
            # report/ingest path which never has findings) -> t.get(...) or
            # [] -> insert_findings returns [] -> zero rows, zero crash.
            finding_rows = findings.insert_findings(
                conn, row["id"], site["id"], t.get("findings") or [])

            # Snapshot for the match_requests/ artifact (Task 4) -- the
            # non-VPC MatcherFunction reads this, never Aurora directly, so
            # every field it needs (the durable topic id + the same
            # title/summary/action-item text just written) is captured here.
            # The durable finding uuids are what the impact matcher (Task 4)
            # will match against and the suggestion-writer (Task 3) will
            # UPDATE by.
            collected_topics.append({
                "topic_id": str(row["id"]),
                "title": t.get("topic_title", ""),
                "summary": t.get("summary"),
                "user_id": str(user_id) if user_id is not None else None,
                # Freshly extracted items are all 'open' (upsert_topic defaults
                # the column), and thread eligibility is "does this subject
                # still carry outstanding work" -- so the count is the length.
                "open_items": len(mapped_action_items),
                "action_items": [{"text": a["text"]} for a in mapped_action_items],
                "findings": [{
                    "finding_id": str(f["id"]),
                    "observation": f["observation"],
                    "domain": f["domain"],
                    "severity": f["severity"],
                    "entity_name": f["entity_name"],
                    "entity_trade": f["entity_trade"],
                } for f in finding_rows],
            })
            # video-keyframe plan (Task 2): collect the durable id + time_range
            # of every topic whose window passes the >=2-minute gate (i.e.
            # keyframe_seconds yields at least one frame). The KeyframeFunction
            # recomputes the exact frame instants itself from time_range.
            if keyframe_seconds(t.get("time_range")):
                keyframe_topics.append({"topic_id": str(row["id"]),
                                        "time_range": t.get("time_range")})
            topics_n += 1

        if collected_topics:
            if SUGGEST_THREADS:
                _suggest_threads(conn, site["id"], date, collected_topics)
            else:
                # Say that it is off. An env-gated feature that logs nothing
                # when disabled is indistinguishable from one that is broken,
                # and the first question anyone asks is "did it even run".
                logger.info("thread suggestions: disabled (SUGGEST_THREADS)")

        # INSIDE the connection block, deliberately. psycopg3's `with conn:`
        # CLOSES the connection on exit (db/connection.py says so), so this ran
        # outside it and raised on every single successful merge -- swallowed by
        # the except below and mis-logged as an email failure. merge_result
        # stayed NULL with merged_at set, which is exactly the signature the
        # stuck-group recovery looks for: every successful merge would have been
        # re-merged and re-emailed.
        if ENABLE_GROUP_MERGE and extraction.get("tier") == "group" and topics_n:
            session_group.mark_result(conn, extraction["groupId"], "merged")

    logger.info("item-writer wrote extraction=%s topics=%d", extraction_key, topics_n)

    # The updated email, AFTER the connection block commits: the merged topics
    # must be durable before every member is told to look at them. Same ordering
    # rule as the matcher artifact below, for the same reason.
    #
    # Enqueued here rather than by the sweep because the email has to contain
    # the merged record, and only the step that LANDS the result knows it
    # landed. This lambda is in-VPC and cannot invoke another (BUG-36), so the
    # request rides the same S3 channel as everything else crossing that line.
    if ENABLE_GROUP_MERGE and extraction.get("tier") == "group" and topics_n:
        try:
            _enqueue_updated_emails(extraction)
        except Exception:
            # The merged record is already durable; failing to announce it must
            # not undo it. A missing email is recoverable by hand, a rolled-back
            # merge is not.
            logger.exception("group %s: merged topics written but the updated "
                             "emails could not be enqueued", extraction.get("groupId"))

    # AFTER the connection block commits -- the topics referenced in the
    # artifact must be durable before the matcher can act on them. Only
    # emit when something was actually written (mirrors the zero-write
    # skip above); an empty extraction's zero topics never reaches here
    # anyway since collected_topics would be empty.
    if collected_topics:
        match_request.emit(s3(), S3_BUCKET, site["id"], date, extraction_key, collected_topics)

    # video-keyframe plan (Task 2): post-commit, like match_request above --
    # the KeyframeFunction reads these durable topic ids. Env-gated so the
    # pipeline change ships inert. Video availability is resolved by the
    # keyframe fn itself (vad-metadata coverage) -- audio-only days no-op there.
    if EMIT_KEYFRAME_REQUESTS and keyframe_topics:
        keyframe_request.emit(s3(), S3_BUCKET, user_folder, date, session_base,
                              extraction_key, keyframe_topics)

    return {"skipped": False, "topics": topics_n}


# ----------------------------------------------------------
# Entry point — S3 event
# ----------------------------------------------------------
def lambda_handler(event, context):
    event = event or {}
    results = []
    for record in event.get("Records", []):
        key = unquote_plus(record["s3"]["object"]["key"])
        parsed = _parse_extraction_key(key)
        if parsed is None:
            logger.warning("skipping non-extraction S3 key: %s", key)
            continue
        user_folder, date, _session_base = parsed
        results.append(write_extraction_items(date, user_folder, key))
    return {"results": results}
