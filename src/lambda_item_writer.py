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

Row growth (Track B Task 3): re-extraction no longer DELETEs a source key's prior topics --
it marks them `superseded_at` and leaves them in the table (see repositories.topics.
supersede_topics_for_source). A session typically gets 2-4 passes (live, one or more
mid-session live updates, final -- occasionally a group merge on top), so its topics rows
now persist at roughly 3x the row count a single-pass session used to leave behind. At
today's volume (hundreds of topics per site-month) that is nothing; migration 0073's
`idx_topics_live_source` partial index (`WHERE superseded_at IS NULL`) keeps every live read
this task touched at its pre-Task-3 cost regardless of how many superseded passes pile up
underneath.

Environment Variables:
    S3_BUCKET     - S3 bucket name (the data lake -- IngestBucketName)
    CONFIG_KEY    - S3 key for user/site mapping (default: config/user_mapping.json,
                    read indirectly via lambda_ingest.load_mapping's own env var)
    COMPANY_NAME  - default: FieldSight (mirrors lambda_ingest's default)
    PG*/DATABASE_URL - read by db.connection.get_connection()
"""
import json
import todo_collapse
import logging
import os
from urllib.parse import unquote_plus

import boto3

import carry_forward
import lambda_ingest
import keyframe_request
import match_request
# NOT `match_request` above, which is the PROGRAMME matcher's request builder. Two unrelated
# matchers, and the names are one word apart — spelled out here because importing the wrong
# one would type-check, run, and write an artifact nothing consumes.
import speaker_match_request
from db.connection import get_connection
from keyframe_selection import keyframe_seconds
from photo_binding import PHOTOS_PER_TOPIC_CAP  # noqa: F401  (re-export)
from photo_binding import list_pictures as _pb_list_pictures
from repositories import users as users_repo
from photo_binding import photos_for_topics as _photos_for_topics  # noqa: F401  (re-export)
import photo_rebind
import thread_match
from repositories import location_markers
from repositories import (action_items, companies, decision_records, findings, meeting_session,
                          recordings, redactions,
                          session_group, sites, threads, topic_decisions, topic_questions,
                          topics)
from repositories import speaker_intro_suggestions
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

#: Ask for an anonymous speaker re-bind at the end of a session. Off by default, like every
#: other switch in this feature: it costs ~100 s of ONNX on a shared concurrency slot, and a
#: session with no groups reads exactly as it read before this existed.
REBIND_SPEAKERS = os.environ.get("REBIND_SPEAKERS", "false").lower() == "true"

# Whether a finalized session is matched against the company's stored voiceprints without
# anyone asking. False is the deployed default and the rollback.
#
# Separate from REBIND_SPEAKERS because the two do different things: the re-bind is
# anonymous (it groups spk_0/spk_1 within one session and names nobody) and this one puts a
# person's name on a passage. Separate from SPEAKER_IDENTITY_MODE because it is a COST
# change as well — every finalized session pays for ONNX over its turns — and folding it
# into the mode would make "stop paying for this" and "turn naming off for everyone" the
# same action.
MATCH_ON_FINALIZE = os.environ.get("MATCH_ON_FINALIZE", "false").lower() == "true"

# Read here only to refuse when naming is switched off entirely; this lambda does no naming
# itself. `off` is the feature's rollback, and a producer that kept queueing work under it
# would leave that switch meaning nothing.
SPEAKER_IDENTITY_MODE = os.environ.get("SPEAKER_IDENTITY_MODE", "off").lower()
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


def _supersede_member_topics(conn, artifact, run, supersede=None) -> list[dict]:
    """Retire each member's solo topics (Track B Task 3: supersede, not delete) so the
    merged set is the only LIVE record.

    A zero-row supersede is logged loudly. The call is keyed on source_s3_key and
    supersede_topics_for_source returns the rows it retired rather than raising, so a key
    that differs by one character (a date derived in UTC instead of NZ, say) retires nothing
    and leaves exactly the duplicate this whole feature exists to eliminate -- with no error
    anywhere to notice.

    Returns every retired row across every member, flattened -- write_extraction_items'
    `retired_topics` accumulator is documented as covering "every supersede call below", and
    this is one of them; Task 4 needs a member's retired rows the same way it needs the
    idempotent-clear's."""
    supersede = supersede or topics.supersede_topics_for_source
    all_retired = []
    for key in artifact.get("mergedMembers") or []:
        retired = supersede(conn, key, run)
        all_retired.extend(retired)
        if not retired:
            logger.warning(
                "group %s: %s superseded 0 topics -- that member's solo items will "
                "now duplicate the merged record", artifact.get("groupId"), key)
    return all_retired


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


def _resolve_member_contexts(conn, artifact):
    """Each member session's {recipient, date, timeRange, siteName}, keyed by sid.

    MUST run inside the connection block: the caller's `with get_connection()`
    closes the connection on exit (the comment above the enqueue call is the
    scar from finding that out), and the enqueue itself has to happen after the
    commit so the merged topics are durable before anyone is told to look.
    Resolving here and enqueueing there satisfies both.

    Reuses finalize-claim's own resolver rather than re-deriving it. The date is
    an NZ-local conversion of a UTC timestamp, and a second implementation of
    that is how BUG-37 keeps coming back -- a UTC date names a folder that does
    not exist and the email quietly says "No summary". Imported lazily so the
    module graph stays acyclic at load time.
    """
    from lambda_finalize_claim import _resolve_context
    out = {}
    for sid in artifact.get("memberSessions") or []:
        try:
            row = meeting_session.get(conn, sid)
            if row:
                out[sid] = _resolve_context(conn, row) or {}
        except Exception:
            # One unresolvable member must not cost the others their email.
            logger.exception("updated-email: could not resolve context for member %s", sid)
    return out


def _enqueue_updated_emails(artifact, contexts, put=None):
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
    summary = artifact.get("summary") or _summary_from_topics(artifact)
    for sid in artifact.get("memberSessions") or []:
        ctx = (contexts or {}).get(sid) or {}
        recipient = (ctx.get("recipient") or "").strip()
        if not recipient:
            # This used to be written anyway, with no recipient at all, and the
            # worker skipped it BEFORE writing a result -- so the member got no
            # email and nothing anywhere recorded that. Refusing to write the
            # request, loudly, is the same outcome with a trace.
            logger.warning("updated-email: no recipient for member %s of group %s "
                           "-- not enqueued", sid, artifact.get("groupId"))
            continue
        put(f"session_finalize_requests/{sid}-updated.json",
            {"kind": "updated", "sessionId": sid,
             "groupId": artifact.get("groupId"),
             "recipient": recipient,
             # The email renders Site and Date in its header; without these it
             # is delivered with blanks, which is worse than not delivered.
             "date": ctx.get("date"),
             "timeRange": ctx.get("timeRange"),
             "siteName": ctx.get("siteName"),
             # An explicit summary wins if the extraction ever starts writing
             # one; today it never does, so this is the fallback that keeps the
             # email from quoting nothing. Computed once, outside the loop, so
             # every member genuinely quotes the SAME account of the meeting —
             # which is the one thing the merged record promises.
             "summary": summary,
             "openTodos": todos})


def _final_email_context(conn, session_base, extraction, date):
    """What the recorder's confirmation email needs, or None if it must not be
    sent from here.

    None means the sweep's backstop owns this session -- which is the honest
    answer for every case below, and each says which one it was, because a
    silent skip here is indistinguishable from a trigger that never fired."""
    sid = _device_session_id(session_base)
    if not sid:
        return None                      # a whole-file base, not a device session
    row = meeting_session.get(conn, sid)
    status = (row or {}).get("status")
    if status != "finalizing":
        logger.info("session %s: final extraction written, status is %s -- no email "
                    "from here", sid, status or "unknown")
        return None
    from lambda_finalize_claim import _resolve_context
    ctx = _resolve_context(conn, row) or {}
    if not (ctx.get("recipient") or "").strip():
        # The claim step already marked this session failed and said whose
        # email is missing; repeating it here would only double the noise.
        return None
    return {"kind": "final", "sessionId": sid,
            "recipient": ctx["recipient"], "date": ctx.get("date") or date,
            "timeRange": ctx.get("timeRange"), "siteName": ctx.get("siteName"),
            # handoff-sync plan §2.2: the worker needs the folder to poll
            # session_brief/<folder>/<date>/sid<sessionId>/latest.json. Not
            # carried before this -- the only recipient of `ctx["folder"]` used
            # to be the email itself, which never needed it.
            "folder": ctx.get("folder"),
            # Zero rows is a real answer, not a reason to withhold the email:
            # most sessions produce none, and the renderer says so explicitly.
            # Withholding would send them down the backstop, which quotes the
            # OTHER summariser -- the disagreement this whole change removes.
            "openTodos": _final_email_rows(extraction, date) + _topic_rows(extraction)}


def _enqueue_final_email(ctx, put=None):
    """One confirmation email per session, whoever gets there first."""
    put = put or _put_finalize_request
    return put(f"session_finalize_requests/{ctx['sessionId']}.json", ctx,
               only_if_absent=True)


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


def _summary_from_topics(artifact):
    """What the merged meeting was about, for the email.

    The same gap `_todos_from_topics` covers, one field along: the solo path
    takes its summary from the rolling summary, and a merged artifact has no
    rolling summary — nor any top-level `summary` of its own. Verified against a
    real group extraction on the test stack, 2026-08-11: the artifact carries
    topics, memberSessions, source_transcripts and nothing resembling a summary,
    so reading one sent every member an email that quoted nothing.

    Built from the merged topics rather than by asking the model again. The
    merge is already a paid thinking call, and a second one to summarise what it
    just produced would be a second thing that can fail after the record is
    durable.

    Returns "" when the merge produced no topics. An empty merge is guarded
    earlier — it must not delete the members' own records — and if one reaches
    here the email should say nothing rather than invent something.
    """
    parts = []
    for topic in artifact.get("topics") or []:
        title = (topic.get("topic_title") or topic.get("title") or "").strip()
        body = (topic.get("summary") or "").strip()
        if title and body:
            parts.append(f"{title}: {body}")
        elif title or body:
            parts.append(title or body)
    return "\n".join(parts)


def _final_email_rows(artifact, date):
    """The session's action items in the shape the email renderer wants, with
    the due date RESOLVED.

    `_todos_from_topics` sends the spoken text ("Tomorrow 08:00"); Aurora stores
    the date `lambda_ingest._map_action_items` resolves from it. The email and
    the record would disagree on the one cell a reader acts on, so this reuses
    that same mapping -- the spoken text remains, as the fallback for a deadline
    nothing could resolve.

    No longer carries a `topic_range` (handoff-sync plan §8 originally added
    it, §7.1 as first written): that field existed only so
    `lambda_session_finalize` could decide, by TIME, whether a brief task
    already covered a row's topic. §7.1 is now decided by TEXT instead (see
    `lambda_session_finalize._is_represented`), which reads a row's own
    `text` -- already present here -- so nothing downstream reads this field
    any more."""
    out = []
    for topic in artifact.get("topics") or []:
        for item in lambda_ingest._map_action_items(topic.get("action_items"), date):
            text = (item.get("text") or "").strip()
            if text:
                out.append({"text": text,
                            "responsible": item.get("responsible") or None,
                            "due": item.get("deadline") or item.get("deadline_text") or None})
    return todo_collapse.collapse_if_enabled(out)


#: How much of a topic's own summary rides in its row. Long enough for the
#: point, short enough that the table still scans -- the row is context, not the
#: report, and the report is one click away.
TOPIC_ROW_MAX_CHARS = 180


def _topic_rows(artifact):
    """Topics that produced NO action item, as rows of their own.

    A session where nobody promised anything is the ordinary case (11 of 16
    measured on prod), and such a session used to be emailed as a header and one
    line saying nothing was captured -- while the recording plainly had content.
    The topic says what was discussed; the owner and due date are N/A because
    there is no task here, which the renderer shows differently from an action
    nobody has picked up yet.

    Only topics with no action items: one that produced tasks is already in the
    table through them, and listing it twice would pad the very table this is
    trying to make worth reading.

    No longer carries a `topic_range` -- same reason as `_final_email_rows`
    above: `lambda_session_finalize` now decides §7.1 (does the brief already
    cover this topic) from this row's own `text`, not from a time window."""
    out = []
    for topic in artifact.get("topics") or []:
        if topic.get("action_items"):
            continue
        title = (topic.get("topic_title") or topic.get("title") or "").strip()
        summary = " ".join((topic.get("summary") or "").split())
        # The first sentence, not the whole summary: the rest repeats it at
        # length, which is what pushed the old prose below the fold.
        first = summary.split(". ")[0].strip()
        if first and first != summary and not first.endswith("."):
            first += "."
        text = " — ".join(part for part in (title, first) if part)
        if len(text) > TOPIC_ROW_MAX_CHARS:
            text = text[:TOPIC_ROW_MAX_CHARS - 1].rstrip() + "…"
        if text:
            out.append({"text": text, "responsible": None, "due": None,
                        "kind": "topic"})
    return out


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
    # The surface that is not in the database at all. A merged group artifact
    # carries every member session's topics, so one commitment said in three of
    # them is listed three times in the stop-recording email — and a
    # database-side collapse does nothing about it, because this list is built
    # from the S3 artifact and never touches Aurora.
    #
    # These rows carry no `status`; a freshly extracted commitment is open by
    # definition, which is what todo_collapse._status says and why it says it.
    # The `mention_count` / `collapsed_ids` the collapse adds are ignored by the
    # renderer, which reads text/responsible/due — harmless, and there if the
    # email ever wants to say "raised three times".
    return todo_collapse.collapse_if_enabled(out)


def _request_rebind(company_id, session_base, artifact, put=None):
    """Ask the embedder to group this session's per-call speaker labels. Returns True if asked.

    Written from HERE because of who can do what. `lambda_extract_session` has the turns and
    no database; this function has the database, the company id and the deletion tombstones,
    and no way to read `transcripts/`. So the turns ride on the extraction artifact and the
    request is written on this side.

    It goes to `voiceprint_requests/`, whose S3 notification is ALREADY hand-wired — the part
    BUG-33 makes expensive. One new IAM prefix, no new trigger.

    Best effort. A re-bind that does not happen leaves the transcript reading exactly as it
    reads today; a re-bind that takes the item write down with it would trade a working
    feature for a cosmetic one.
    """
    turns = artifact.get("speaker_turns") or []
    if not turns:
        return False
    # The pre-check, and it logs what it skipped on: most sessions are one person across one
    # call, where the re-bind changes nothing and would still cost ~100 s of ONNX and a
    # concurrency slot the naming path wants. A skip that says nothing is indistinguishable
    # from a producer that never ran.
    pairs = {(t.get("source_filename"), t.get("speaker_label")) for t in turns
             if t.get("source_filename") and t.get("speaker_label")}
    calls = {src for src, _ in pairs}
    if len(pairs) < 2 or len(calls) < 2:
        logger.info("rebind: not requested for %s — %d label(s) across %d call(s)",
                    session_base, len(pairs), len(calls))
        return False
    put = put or _put_finalize_request
    try:
        put(f"voiceprint_requests/{company_id}/{session_base}/rebind.json",
            {"op": "rebind", "company_id": str(company_id),
             "session_base": session_base,
             "user_folder": artifact.get("userFolder") or artifact.get("user_folder"),
             "date": artifact.get("date"),
             "turns": turns})
    except Exception:
        logger.exception("rebind: could not enqueue for %s", session_base)
        return False
    logger.info("rebind: requested for %s (%d pairs across %d calls)",
                session_base, len(pairs), len(calls))
    return True


def _request_match(company_id, session_base, artifact, site_id=None, put=None):
    """Ask the embedder to name this session from the profiles the company already holds.

    **The gap this closes.** Until 2026-09-23 the ONLY producer of a match request was
    `lambda_org_api.speaker_match`, an endpoint no frontend has ever called — verified by
    grep, twice. So naming a speaker propagated the name inside that one meeting and the
    next meeting started at `spk_0` again. "The system recognises Ben" was true of a code
    path nothing reached.

    Anonymous re-binding beside this does run automatically and always has; it groups
    `spk_0`/`spk_1` within one session and carries no name. The two are independent on
    purpose and stay independent here: `_request_rebind` is gated on `REBIND_SPEAKERS` and
    this is gated on `MATCH_ON_FINALIZE`, so either can be turned off without the other.

    **A separate switch, not `SPEAKER_IDENTITY_MODE != off`.** Automatic matching is a cost
    change as much as a behaviour change — every finalized session now pays for ONNX over
    its turns — and folding it into the mode would mean the only way to stop paying is to
    turn naming off entirely for everyone. It also means the rollback is a variable, not a
    deploy.

    Best effort, like the re-bind. A match that does not happen leaves the transcript
    reading exactly as it reads today; a match that takes the item write down with it trades
    a working feature for a cosmetic one.
    """
    if not MATCH_ON_FINALIZE:
        return False
    if SPEAKER_IDENTITY_MODE == "off":
        # The one gate that is NOT independent: `off` is this feature's rollback, and a
        # producer that kept writing requests under it would leave the switch meaning
        # nothing. The embedder would refuse them anyway; paying for the invocation to find
        # that out is the part worth skipping.
        logger.info("match: not requested for %s — speaker identity is off", session_base)
        return False
    turns = artifact.get("speaker_turns") or []
    put = put or _put_finalize_request
    try:
        requests = speaker_match_request.build(
            company_id=company_id,
            session_base=session_base,
            user_folder=artifact.get("userFolder") or artifact.get("user_folder"),
            date=artifact.get("date"),
            turns=turns,
            mode=SPEAKER_IDENTITY_MODE,
            site_id=site_id,
            # Who asked. No user id here by construction — nobody asked, the session ended
            # — and saying `finalize` is what lets an operator tell an automatic name apart
            # from one a person requested when a wrong one turns up.
            source="finalize")
    except ValueError:
        # A session with no `sid`, or missing folder/date. Legacy recordings are the
        # ordinary case and are not an error; they simply cannot be grouped into a session.
        logger.info("match: not requested for %s — not a session-shaped recording",
                    session_base)
        return False
    if not requests:
        logger.info("match: not requested for %s — no transcribed turns", session_base)
        return False
    try:
        for req in requests:
            put(f"voiceprint_requests/{company_id}/{session_base}/match-{req['request_id']}.json",
                req)
    except Exception:
        logger.exception("match: could not enqueue for %s", session_base)
        return False
    logger.info("match: requested for %s (%d turns across %d run(s), mode=%s)",
                session_base, len(turns), len(requests), SPEAKER_IDENTITY_MODE)
    return True


def _put_finalize_request(key, body, only_if_absent=False):
    """Write one finalize request.

    `only_if_absent` makes the key a SINGLE-WRITER key: `IfNoneMatch="*"` fails
    with 412 if anything is already there, and the loser simply stops. Two
    producers can write this key -- this lambda, once the record is durable, and
    the sweep's backstop when no final extraction arrived -- and S3 notifies on
    every put INCLUDING an overwrite, so without this the recorder gets two
    emails. Checking first is not the fix: this role cannot read the prefix, and
    AccessDenied reads as "absent" (403 != 404, eight recurrences), so both
    writers would pass the check. Same idiom as lambda_orchestrator's claim.

    Returns True when this caller wrote it."""
    import boto3
    from botocore.exceptions import ClientError
    extra = {"IfNoneMatch": "*"} if only_if_absent else {}
    try:
        boto3.client("s3").put_object(
            Bucket=S3_BUCKET, Key=key,
            Body=json.dumps(body, ensure_ascii=False),
            ContentType="application/json", **extra)
        return True
    except ClientError as e:
        status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        code = e.response.get("Error", {}).get("Code", "")
        if only_if_absent and (status == 412 or code in ("PreconditionFailed", "412")):
            logger.info("finalize request %s already enqueued by someone else", key)
            return False
        raise


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

# Track B Task 6a: the floor a candidate must clear to be worth a
# decision_records row at all -- deliberately LOWER than
# thread_match.MIN_SCORE (the real accept bar the suggestion itself uses).
# The point of Task 6 is that a REJECTED verdict is recorded too, so a
# candidate the matcher genuinely considered and turned down (0.10-0.25)
# gets a row with auto_outcome='rejected'; below 0.10 the pair barely
# shares vocabulary at all and recording it would just be noise on every
# site's corpus.
_THREAD_RECORD_FLOOR = 0.10


def _suggest_threads(conn, company_id, site_id, date, written):
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
    only this pass -- this is also the SAVEPOINT Track B Task 6a's
    per-candidate decision_records writes below ride inside; they need no
    savepoint of their own."""
    try:
        with conn.transaction():
            return _suggest_threads_inner(conn, company_id, site_id, date, written)
    except Exception:
        logger.exception("thread suggestion pass failed; topics were written")
        return 0


def _suggest_threads_inner(conn, company_id, site_id, date, written):
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
        #
        # Track B Task 6a: scored at _THREAD_RECORD_FLOOR (0.10), not the
        # real accept bar thread_match.MIN_SCORE (0.25) -- `scored` is a
        # strict superset of what a plain find_candidates() call would
        # have returned (same corpus/IDF, only the floor differs, and the
        # sort order/scores are identical), so filtering it back down to
        # `hits` below reproduces the pre-Task-6a suggestion logic exactly.
        scored = thread_match.find_candidates(
            new_topic, list(corpus) + [new_topic], min_score=_THREAD_RECORD_FLOOR)
        if not scored:
            continue
        hits = [c for c in scored if c["match_score"] >= thread_match.MIN_SCORE]
        winner = hits[0] if hits else None
        winner_row = None
        if winner is not None:
            # Join the parent's thread if it has one; otherwise anchor a new
            # thread on the parent itself. Exactly one of these, which the
            # table's CHECK enforces.
            if winner.get("thread_id"):
                winner_row = threads.upsert_suggestion(
                    conn, t["topic_id"], thread_id=winner["thread_id"],
                    score=winner["match_score"], gap_days=winner["gap_days"])
            else:
                winner_row = threads.upsert_suggestion(
                    conn, t["topic_id"], parent_topic_id=winner["id"],
                    score=winner["match_score"], gap_days=winner["gap_days"])
            if winner_row is not None:
                made += 1
        # A candidate is 'accepted' only when it is the ONE that actually
        # became a live suggestion this pass -- `winner_row is None` means
        # `already_resolved` (a human already answered this exact proposal;
        # threads.upsert_suggestion docstring), so nothing new was proposed
        # and every scored candidate here is 'rejected', winner included.
        became_suggestion_id = winner["id"] if winner_row is not None else None
        for c in scored:
            # `output` carries only ids/numbers -- match_score, gap_days,
            # the earlier topic's OWN thread_id (a uuid, not text) -- never
            # `c`'s title/summary, which are extraction-derived text (plan
            # Global Constraint: decision_records never carries transcript
            # text).
            decision_records.insert(
                conn, company_id=company_id, site_id=site_id, kind="thread",
                subject_type="topic", subject_stable_id=t["topic_id"],
                object_ref=str(c["id"]), provider="lexical", model=None,
                model_version=None, question_set=None, input_key=None, input_hash=None,
                output={"match_score": c["match_score"], "gap_days": c["gap_days"],
                        "thread_id": c.get("thread_id")},
                score=c["match_score"], threshold=thread_match.MIN_SCORE,
                auto_outcome=("accepted" if c["id"] == became_suggestion_id else "rejected"),
            )
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


# Both extraction writers (extract_session, extract_group) stamp
# llm_provider/llm_model via lambda_extract_session's shared
# _llm_identity() helper into every extraction artifact. This function
# still calls no LLM itself -- the values ride in on the JSON it already
# reads, so no LLM_PROVIDER/LLM_TEMPERATURE env pairing is needed on
# ItemWriterFunction (test_the_temperature_knob_reaches_every_function_
# that_calls_an_llm stays green because this function carries neither).
# The fallback below covers an extraction artifact written before this
# change was deployed, which has neither key -- 'unknown', never a
# guessed vendor name; model stays None either way.
_WORK_CLASS_PROVIDER_FALLBACK = "unknown"


def _record_work_class_decision(conn, company_id, site_id, topic_id,
                                work_class, work_confidence, is_mixed,
                                llm_provider, llm_model):
    """Track B Task 6a: one decision_records row for a topic's work_class
    classification, ONLY when the topic actually has one -- `work_class`
    here is the ALREADY-sanitized value (the caller's `_wc`: NULL for
    anything outside the CHECK enum), so a bad/missing LLM value produces
    no record rather than a bogus one. There is no accept/reject gate on a
    classification like there is on a programme match -- it always exists
    or it doesn't -- so `auto_outcome` is always 'accepted' and
    `threshold` is always None.

    `llm_provider`/`llm_model` are the extraction's OWN `llm_provider`/
    `llm_model` fields (Ruling R15) -- already defaulted by the caller
    (`llm_provider` to `_WORK_CLASS_PROVIDER_FALLBACK`, `llm_model` to None)
    for an extraction written before those fields existed.

    Wrapped in its OWN SAVEPOINT with a WARNING on failure, same posture as
    Ruling R10/_suggest_threads: this write must never be able to abort the
    topic/finding/action-item write it rides alongside."""
    if work_class is None:
        return
    try:
        with conn.transaction():
            decision_records.insert(
                conn, company_id=company_id, site_id=site_id, kind="work_class",
                subject_type="topic", subject_stable_id=topic_id, object_ref=None,
                provider=llm_provider, model=llm_model, model_version=None,
                question_set=None, input_key=None, input_hash=None,
                output={"work_class": work_class, "work_confidence": work_confidence,
                        "is_mixed": is_mixed},
                score=work_confidence, threshold=None, auto_outcome="accepted",
            )
    except Exception:
        logger.warning("work_class decision record not stored for topic=%s", topic_id)


def write_extraction_items(date, user_folder, extraction_key):
    raw = s3().get_object(Bucket=S3_BUCKET, Key=extraction_key)["Body"].read()
    extraction = json.loads(raw.decode("utf-8"))

    # One extraction pass, one LLM call -- read ONCE here and reused for
    # every topic's work_class record below. Both extraction writers
    # (extract_session, extract_group) stamp these via _llm_identity(), so
    # the fallback covers only an extraction artifact written before this
    # change was deployed -- 'unknown', never a guessed vendor name.
    # `llm_model` is already None-safe on its own (a missing key and an
    # extraction that genuinely couldn't name its model both read the same
    # way).
    extraction_llm_provider = extraction.get("llm_provider") or _WORK_CLASS_PROVIDER_FALLBACK
    extraction_llm_model = extraction.get("llm_model")

    # Track B Task 3: identifies THIS pass to repositories.topics.supersede_topics_for_source
    # (stamped onto the retired row's superseded_by_run) -- tier + extracted_at is unique per
    # pass of a given extraction key (live/final tiers of the same key share out_key but never
    # extracted_at; a group pass carries its own).
    run = f"{extraction.get('tier')}:{extraction.get('extracted_at')}"

    # Rows this invocation retires, across every supersede call below -- the authority-flip
    # branch (report_source_key), this key's own idempotent clear, and (group tier) each
    # member's own supersede via _supersede_member_topics all write into it. Not consumed
    # here; Task 4 reads it to carry stable ids and human edits from a retired row to the
    # row that replaced it.
    retired_topics = []

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
            if not lambda_ingest.AUTHORITY_FLIP:
                reason = "nightly report already ingested — late session extraction superseded"
                logger.info("%s: %s", extraction_key, reason)
                return {"skipped": True, "reason": reason}
            # Under the flip the extraction IS the item store, and a day that has one
            # is by definition not a "zero-extraction fallback day" -- the only kind
            # the comment above says report topics may exist for. Skipping here
            # deadlocked with ingest, which defers only once extraction topics
            # already exist: each waited for the other, and the day could never
            # become session-scoped again. So the extraction wins. Only this
            # (date, user)'s report rows go; its chunks survive (topic_id is ON
            # DELETE SET NULL) and the next ingest of that report re-links them.
            retired_report = topics.supersede_topics_for_source(conn, report_source_key, run)
            retired_topics.extend(retired_report)
            logger.info("%s: authority flip -- superseded %s report topic(s) from %s",
                        extraction_key, len(retired_report), report_source_key)

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

        # The customer deleted this recording. Write nothing.
        #
        # Checked AFTER the advisory lock and BEFORE the idempotent clear, so a delete
        # racing an extraction cannot end with the old rows cleared and no new ones -- the
        # clear is what makes this ordering matter, not the insert.
        if _source_is_deleted(conn, extraction_key):
            logger.info("%s: source is deleted — writing no topics", extraction_key)
            return {"skipped": "source_deleted", "key": extraction_key}

        # Source-key idempotency (Phase 4a pattern): supersede this extraction's prior rows
        # (Track B Task 3) before re-inserting -- an UPDATE that stamps superseded_at /
        # superseded_by_run, not a DELETE, so the row and its children (action_items,
        # findings, ...) survive in the table. Task 2's read predicates already hide a
        # superseded row from every display path, so a reader sees exactly what it saw when
        # this was delete-then-insert; what changed is that nothing underneath is gone.
        #
        # The check-off IS `action_items.status` -- a column on the now-superseded row. The
        # live and final tiers write to the SAME key (`extract_session.out_key` is computed
        # once; the tier rides inside the artifact), so every final pass retires whatever a
        # person ticked while the meeting was still running. It is not lost -- it sits on the
        # hidden row until Task 4 carries it forward to its replacement by stable_id -- but
        # nothing carries it forward YET, so until Task 4 lands it is exactly as unreachable to
        # a reader as a deleted row was. Re-matching by text is the wrong fix regardless: the
        # model rewords, merges and splits, and a confident wrong match puts a supervisor's
        # tick on a DIFFERENT action item where nobody would ever see it -- matching by
        # stable_id (Task 4) is the only safe rule.
        #
        # What used to be discarded here is now carried forward by _carry_forward_children,
        # below the topic-insert loop (Track B Task 4) -- the count an operator needs to see
        # is now measured AFTER matching (an actual orphan), not before (every closed item
        # about to be superseded, most of which will find their successor).
        retired_topics.extend(topics.supersede_topics_for_source(conn, extraction_key, run))

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
            retired_topics.extend(_supersede_member_topics(conn, extraction, run))

        # Task 3 (authority-flip plan) -- list the pictures prefix ONCE per
        # invocation (paginator, outside the per-topic loop below).
        #
        # THE LIST IS DAY-WIDE AND ALWAYS HAS BEEN. Until 2026-09-23 it was
        # matched against THIS EXTRACTION's topics only, which is the whole
        # defect: idempotency is keyed on source_s3_key, so session B's run
        # never cleared session A's bindings and both kept the same photo.
        # Ben_UCPK2 produced seven artifacts for one afternoon; prod held 22
        # multi-bound photos out of 161, the worst under six topics spanning
        # seven minutes. The binding now happens once for the whole day, after
        # this extraction's own topics are in -- see photo_rebind.py.
        pictures_prefix = f"users/{user_folder}/pictures/{date}/"
        photo_objects = _list_pictures(pictures_prefix)
        extraction_topics = extraction.get("topics", [])

        # The day's location markers, written where the READER can reach them.
        #
        # This lambda has the database and the company id; org-api, which serves
        # the day view, has neither the extraction artifact nor -- checked
        # before choosing this design -- any S3 grant that would let it read
        # one: ListBucket and GetObject on `extractions/` both simulate as
        # implicitDeny for its role. Reading the artifact from there would have
        # meant two new grants of exactly the shape that has silently 403'd
        # eight times in this repo.
        #
        # Replace, never append: a session is re-driven routinely (finalize,
        # the backlog probe's repairs, a manual invoke) and each run produces
        # the day's complete set.
        #
        # Never fatal. The markers are an addition to a day that already works
        # without them; a write that can turn a good extraction into a failed
        # one would be a worse bug than ungrouped photos.
        try:
            location_markers.replace_for_day(
                conn, company["id"], user_folder, date,
                extraction.get("location_markers") or [])
        except Exception:  # noqa: BLE001 -- see above
            logger.exception("location markers not stored for %s/%s", user_folder, date)

        # Self-introduction suggestions ("Hi, this is Petros from Cassidy"), INSIDE the
        # connection block, deliberately -- `_request_rebind`/`_request_match` below are
        # called AFTER `with get_connection() as conn:` has closed (psycopg3's `with conn:`
        # closes it on exit), and a DB write placed there raises on every single run. See
        # the comment beside the group-merge email a few lines down for the incident that
        # placement caused once already.
        #
        # Gated on tier=='final' as belt and braces: `lambda_extract_session` already
        # writes `[]` for a live artifact (Task 2), so this should be a no-op for every
        # live pass, but the writer does not trust the producer alone for a DB write --
        # the same reasoning `report_already_ingested`'s guard above applies.
        #
        # NOT gated on SPEAKER_IDENTITY_MODE (owner decision, spec "Review outcome"
        # decision 1): a company's queue should already be full of correctly-detected
        # introductions on the day identity gets switched on, and these rows hold no
        # biometric data -- only text and offsets. The confirm path is what is gated, in
        # org-api.
        #
        # Never fatal, same rule as `location_markers` just above: a store failure must
        # not turn a good extraction into a failed one.
        if extraction.get("tier") == "final" and extraction.get("self_introductions"):
            try:
                counts = speaker_intro_suggestions.store(
                    conn, company["id"], session_base, user_folder, date,
                    extraction["self_introductions"])
                logger.info(
                    "intro suggestions: inserted=%d skipped_named=%d skipped_no_sid=%d",
                    counts["inserted"], counts["skipped_named"], counts["skipped_no_sid"])
            except Exception:  # noqa: BLE001 -- see above
                logger.exception("self-introduction suggestions not stored for %s/%s",
                                 user_folder, date)

        topics_n = 0
        collected_topics = []
        # Track B Task 4: the durable ids of THIS pass's own topics (uuid.UUID objects, not
        # the str(...) collected_topics uses for the match_request artifact) -- the "new"
        # side of carry_forward's site-scoped pool, gathered once the loop below is done.
        new_topic_ids = []
        keyframe_topics = []  # video-keyframe plan: {topic_id, time_range} of gate-passers
        for i, t in enumerate(extraction_topics):
            mapped_action_items = lambda_ingest._map_action_items(t.get("action_items"), date)
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
                # `questions`, not `open_questions`: that is the key
                # lambda_extract_session's own schema asks the model for, and
                # the one chunking.py and lambda_ask_agent.py have always read.
                #
                # THIS is the writer that matters for open questions, not
                # lambda_ingest. On an authority-flip day ingest defers and
                # writes no report topics at all -- every topic in Aurora comes
                # through here. Wiring the column, the ingest pass-through and
                # the org-api serializer while leaving this line out would have
                # produced exactly the same empty section, one layer further in.
                open_questions=[q.get("question") if isinstance(q, dict) else q
                                for q in (t.get("questions") or [])
                                if (q.get("question") if isinstance(q, dict) else q)] or None,
                # Kept whole, unlike `questions` above. The extraction schema's
                # decision carries `rationale` and `decided_by` alongside the
                # sentence, and v1 not SENDING them is not a reason to discard
                # them on the way in -- a superseded session's transcript window
                # may be gone by the time anyone wants them. The narrowing to
                # plain strings happens in lambda_org_api, at the payload
                # boundary, where React forces it. Blank-only entries are still
                # dropped: they render as a bullet with nothing in it.
                decisions=[d for d in (t.get("decisions") or [])
                           if (d.get("decision") if isinstance(d, dict) else d)] or None,
                work_class=_wc, work_confidence=_wconf, is_mixed=(t.get("is_mixed") is True),
                evidence=_evidence_payload(t),
                # NO `photos=` ANY MORE. Two writers for one table is how the
                # rows diverged: this one inserted its own session's binds and
                # nothing was ever responsible for removing another session's.
                # `photo_rebind.rebind_day_photos` owns topic_photos for the whole
                # day, and it runs after this loop because it needs these rows
                # to exist before it can bind to them.
            )
            new_topic_ids.append(row["id"])
            # Track B Task 6a: one work_class decision_records row per topic
            # that actually carries one (see `_record_work_class_decision`
            # for what "actually carries one" means and why this must not
            # be able to abort the write below it).
            _record_work_class_decision(
                conn, company["id"], site["id"], row["id"], _wc, _wconf,
                t.get("is_mixed") is True, extraction_llm_provider, extraction_llm_model)
            # Task 2 (programme-impact-link plan) -- persist this topic's
            # rich extraction findings in the SAME transaction as the topic
            # upsert (inherits the I-3 advisory lock + I-4 supersession
            # guard already established above). Legacy extraction JSON with
            # no 'findings' key (pre-#46 extractions still in S3, and the
            # report/ingest path which never has findings) -> t.get(...) or
            # [] -> insert_findings returns [] -> zero rows, zero crash.
            finding_rows = findings.insert_findings(
                conn, row["id"], site["id"], t.get("findings") or [])

            # Track B Task 5 -- decisions/open_questions become their OWN rows, dual-written
            # in the SAME transaction right after findings above, alongside (not instead of)
            # the jsonb upsert_topic already wrote a few lines up (decisions=/open_questions=
            # kwargs). Each insert does its own blank-dropping identical to those kwargs
            # (see topic_decisions.insert_decisions / topic_questions.insert_questions
            # docstrings) so the row table and the jsonb mirror never disagree about which
            # entries exist. A stable_id here is what lets Task 4's carry_forward (below)
            # keep a decision or an answered question attached to the same commitment
            # across a re-extraction that reworded it.
            topic_decisions.insert_decisions(
                conn, row["id"], site["id"], t.get("decisions") or [])
            topic_questions.insert_questions(
                conn, row["id"], site["id"], t.get("questions") or [])

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

        # Track B Task 4 -- carry a human's tick, status, reassignment or deadline edit from
        # a row this pass just superseded to the row that replaced it. Same transaction,
        # after the new topics/children above are inserted (their ids are only known now)
        # and after every supersede call this invocation made (retired_topics is complete by
        # here: the idempotent clear, the group-member supersede, and the authority-flip
        # branch all ran above, before this loop). A pass that retired nothing has no "old"
        # pool and nothing to report -- the common case, and the whole reason retired_topics
        # is checked here rather than always calling into an empty match.
        #
        # _carry_forward_children runs its own work inside a SAVEPOINT, not a bare
        # try/except (Ruling R10): a bug there must DEGRADE (the topics/findings/action-items
        # already inserted above still commit) rather than ABORT the whole pass -- and only a
        # real SAVEPOINT undoes that, since Postgres aborts the enclosing transaction on any
        # SQL error and a Python try/except cannot un-abort it. See that function's own
        # docstring for the full reasoning (same shape as _suggest_threads above it).
        if retired_topics:
            _carry_forward_children(
                conn, [t["id"] for t in retired_topics], new_topic_ids, site["id"],
                extraction_key)

        # THE DAY'S PHOTOS, BOUND ONCE, AFTER THIS EXTRACTION'S TOPICS EXIST.
        # Never fatal: a rebind that turned a good extraction into a failed one
        # would be a worse bug than a misplaced thumbnail, and the day view
        # lists every photo regardless of binding.
        try:
            photo_rebind.rebind_day_photos(
                conn, company["id"], user_folder, date, photo_objects)
        except Exception:  # noqa: BLE001 -- see above
            logger.exception("day photo rebind failed for %s/%s", user_folder, date)

        if collected_topics:
            if SUGGEST_THREADS:
                _suggest_threads(conn, company["id"], site["id"], date, collected_topics)
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
        # The recorder's own confirmation email. Resolved HERE (the connection
        # dies with this block) and enqueued AFTER it, for the same reason the
        # merged email is: the rows must be durable before anyone is told to
        # look at them.
        #
        # Enqueued by this step rather than at the sweep's claim, because the
        # email should show the record -- and only the step that LANDS the
        # record knows it landed. The sweep enqueued at claim time, so the email
        # routinely went out before the final extraction existed and had to be
        # built from a second summariser.
        #
        # tier alone is not enough. A final is written again by the re-run chain
        # (up to FINAL_RERUN_MAX_GENERATIONS) and by org-api's regenerate, months
        # later, for a session that was sent long ago -- each would mail the
        # recorder about an old meeting. The session must be WAITING for it.
        final_email_ctx = None
        if extraction.get("tier") == "final":
            final_email_ctx = _final_email_context(conn, session_base, extraction, date)

        if ENABLE_GROUP_MERGE and extraction.get("tier") == "group" and topics_n:
            session_group.mark_result(conn, extraction["groupId"], "merged")
            # Resolved HERE because the connection dies with the block below,
            # and used AFTER it because the merged topics must be committed
            # before anyone is emailed about them.
            member_contexts = _resolve_member_contexts(conn, extraction)

    logger.info("item-writer wrote extraction=%s topics=%d", extraction_key, topics_n)

    # The updated email, AFTER the connection block commits: the merged topics
    # must be durable before every member is told to look at them. Same ordering
    # rule as the matcher artifact below, for the same reason.
    #
    # Enqueued here rather than by the sweep because the email has to contain
    # the merged record, and only the step that LANDS the result knows it
    # landed. This lambda is in-VPC and cannot invoke another (BUG-36), so the
    # request rides the same S3 channel as everything else crossing that line.
    if final_email_ctx:
        try:
            _enqueue_final_email(final_email_ctx)
        except Exception:
            # The rows are already durable; failing to announce them must not
            # undo them. The sweep's backstop still mails this session.
            logger.exception("session %s: topics written but the confirmation "
                             "email could not be enqueued", final_email_ctx.get("sessionId"))

    if ENABLE_GROUP_MERGE and extraction.get("tier") == "group" and topics_n:
        try:
            _enqueue_updated_emails(extraction, member_contexts)
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

    # Post-commit and AFTER the deleted-source gate above, like the two requests beside it. A
    # session the customer deleted returns long before here, so a re-bind is never asked for
    # one — which matters because the groups would outlive the deletion in a table the
    # tombstone does not reach.
    if REBIND_SPEAKERS:
        _request_rebind(company["id"], session_base, extraction)

    # Naming, which is the other half and gated separately. Same placement and the same
    # reasons: post-commit, and after the deleted-source gate, so a session the customer
    # deleted is never matched — the names would outlive the deletion in a table the
    # tombstone does not reach.
    #
    # `site` narrows the candidate pool to the people who were on that site, which is what
    # keeps the margin meaningful as a company accumulates profiles. It is already resolved
    # above by the ladder BUG-41 settled, and None is a valid answer meaning "no narrowing".
    _request_match(company["id"], session_base, extraction,
                   site_id=(site or {}).get("id"))

    return {"skipped": False, "topics": topics_n}


# ----------------------------------------------------------
# Entry point — S3 event
# ----------------------------------------------------------
def _carry_forward_one_table(conn, repo, old_topic_ids, new_topic_ids, site_id):
    """Match one child table's retired rows to their replacements and carry stable_id (plus
    any human edit) forward. Returns the number of human-touched OLD rows that found no
    successor -- the caller's contribution to the OrphanedHumanEdits metric/log.

    `repo` is `action_items`, `findings`, `topic_decisions` or `topic_questions` (Track B
    Task 4, extended to the last two by Task 5): all four expose
    list_for_carry_forward(conn, topic_ids, site_id) -> rows with a computed `human_touched`,
    and carry_identity(conn, new_id, old_row), with the identical shape -- which is what lets
    this be one function instead of four near-duplicates."""
    old_rows = repo.list_for_carry_forward(conn, old_topic_ids, site_id)
    if not old_rows:
        return 0
    new_rows = repo.list_for_carry_forward(conn, new_topic_ids, site_id)
    old_by_id = {r["id"]: r for r in old_rows}
    pairs, orphans = carry_forward.match(old_rows, new_rows)
    for old_id, new_id, _how in pairs:
        repo.carry_identity(conn, new_id, old_by_id[old_id])
    return sum(1 for oid in orphans if old_by_id[oid]["human_touched"])


def _carry_forward_children(conn, old_topic_ids, new_topic_ids, site_id, extraction_key):
    """Track B Task 4: carry a human's tick, status, reassignment or deadline edit from a row
    this pass just superseded to the row that replaced it, matched by TEXT -- the new row's
    id did not exist when the person made the edit, so stable_id can only be assigned
    afterwards, by finding which new row is "the same" commitment reworded.

    `old_topic_ids` / `new_topic_ids` are both narrowed to `site_id` inside
    `list_for_carry_forward` (Ruling R9): the pool is exactly this invocation's retired
    topics and this pass's own new topics, never another extraction key's superseded rows
    and never another site's.

    A SAVEPOINT (`conn.transaction()` nested inside the caller's already-open transaction),
    not a bare try/except -- same reason `_suggest_threads` above uses one (Ruling R10):
    Postgres aborts the WHOLE enclosing transaction on any SQL error, and catching that in
    Python does not un-abort it. A bare try/except here would let the exception stop
    propagating while every later statement in this pass -- the photo rebind below, the
    final-email lookup, the commit itself -- started failing too, so a carry_forward bug
    would silently take the whole extraction down with it. The SAVEPOINT makes "degrade, do
    not abort" (R10) actually true: on failure it rolls back only what carry_forward itself
    did, leaving the topics/action_items/findings already inserted above intact and
    committable, exactly like a matcher bug leaves the topics `_suggest_threads` was fed
    intact today.

    Always reports, including zero (Ruling R5) -- an operator reading "0 for that key" is
    the point of an EMF line with a `key` property, not a lucky silence indistinguishable
    from a producer that never ran. On failure the metric is NOT zero: nothing was carried,
    so every human-touched old row is -- by definition -- an orphan this pass, and the count
    is recomputed by a fallback read after the SAVEPOINT has rolled back (R10: "the metric
    must not read 0 when carry-forward crashed")."""
    try:
        with conn.transaction():
            orphaned = (_carry_forward_one_table(conn, action_items, old_topic_ids,
                                                 new_topic_ids, site_id)
                       + _carry_forward_one_table(conn, findings, old_topic_ids,
                                                  new_topic_ids, site_id)
                       + _carry_forward_one_table(conn, topic_decisions, old_topic_ids,
                                                  new_topic_ids, site_id)
                       + _carry_forward_one_table(conn, topic_questions, old_topic_ids,
                                                  new_topic_ids, site_id))
    except Exception:
        logger.exception(
            "carry_forward failed for %s -- topics were written, no identity was carried "
            "forward this pass", extraction_key)
        orphaned = _count_human_touched_old(conn, old_topic_ids, site_id, extraction_key)
    _report_orphaned_human_edits(extraction_key, orphaned)


def _count_human_touched_old(conn, old_topic_ids, site_id, extraction_key):
    """Fallback for `_carry_forward_children`'s except branch: every human-touched OLD row
    across all four child tables, unconditionally -- carry_forward crashed, so none of them
    found a successor this pass, regardless of which table or which pair was mid-flight when
    it failed. Runs AFTER the failed SAVEPOINT has already rolled back, as a plain read that
    was not itself part of what failed.

    Never raises further: if even this cannot run, there is no better number left to report,
    so it logs and answers 0 -- which undercounts, but a metric that also throws would take
    the whole pass down for real, the one outcome R10 exists to prevent."""
    try:
        return sum(1 for repo in (action_items, findings, topic_decisions, topic_questions)
                   for row in repo.list_for_carry_forward(conn, old_topic_ids, site_id)
                   if row["human_touched"])
    except Exception:
        logger.exception(
            "could not count human-touched rows for %s after carry_forward failed -- "
            "OrphanedHumanEdits will under-report for this pass", extraction_key)
        return 0


def _report_orphaned_human_edits(extraction_key, count):
    """Tell an operator about human-touched rows that carry_forward could not carry.

    Two channels, because they answer two different questions: the WARNING is for someone
    reading THIS extraction's logs ("did we lose a tick just now?"), the metric is for
    someone watching the fleet ("is this getting worse?") -- and only the metric is
    non-zero-suppressed, so a dashboard can tell "nothing lost" from "this key never ran"
    (Ruling R5).

    Embedded Metric Format printed to stdout, not `put_metric_data`: ItemWriterFunction is
    in-VPC with no CloudWatch endpoint (CLAUDE.md BUG-36) -- a real API call here would
    blackhole to a timeout with zero logs, exactly like ExtractionBacklogFunction's working
    put_metric_data call would if it were deployed in-VPC. Modelled on
    lambda_transcribe._emit_failure_metric's `_aws` block. Never raises: a metric or a log
    line that fails must not take an already-committed extraction down with it -- this runs
    after the transaction line above, by which point the rows are durable either way.
    """
    if count:
        logger.warning(
            "carry_forward: %d human-touched rows had no successor (key=%s)",
            count, extraction_key)
    try:
        import time as _t
        print(json.dumps({
            "_aws": {
                "Timestamp": int(_t.time() * 1000),
                "CloudWatchMetrics": [{
                    "Namespace": "FieldSight/Pipeline",
                    "Dimensions": [["Stage"]],
                    "Metrics": [{"Name": "OrphanedHumanEdits", "Unit": "Count"}],
                }],
            },
            "Stage": os.environ.get("STAGE", "unknown"),
            "OrphanedHumanEdits": count,
            "key": extraction_key,
        }))
    except Exception:
        logger.warning("could not emit the OrphanedHumanEdits metric for %s", extraction_key)


def _source_is_deleted(conn, source_s3_key) -> bool:
    """Whether this extraction's source has been deleted by its owner.

    The pipeline is the resurrection path. `lambda_ingest` deletes a day's topics and
    re-inserts them with NEW uuids when the nightly report supersedes the live extraction,
    so a tombstone naming a topic uuid stops matching within a day and the deleted content
    is back on the customer's dashboard the next morning — with no error anywhere, because
    from the pipeline's point of view nothing went wrong. This is where the write side
    learns to ask.

    Fails OPEN, and that is the safe direction here: if the tombstone table is unreachable,
    writing the row loses nothing (the read filters still hide it), whereas refusing to
    write destroys the only record this audio has.

    Logs either way. A skip nobody can count is indistinguishable from a guard that never
    ran, which is how three separate missing grants in this codebase each looked like
    nothing at all.
    """
    try:
        hit = redactions.is_source_deleted(conn, source_s3_key)
    except Exception:
        logger.exception("deleted-source check failed for %s — writing anyway; the read "
                         "filters still apply", source_s3_key)
        return False
    logger.info("deleted-source check: key=%s deleted=%s", source_s3_key, hit)
    return bool(hit)


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
