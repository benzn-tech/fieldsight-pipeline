"""lambda_session_report.py — Delivery-C generate worker (Tier-2 T3).

Non-VPC (has the python-docx layer + can reach SES). Triggered by an org-api
enqueue writing `session_report_requests/{...}.json`: org-api is in-VPC and so
can neither render docx nor reach SES (CLAUDE.md BUG-36), so it hands the work
off via an S3 request artifact (the match_requests/ pattern). This worker reads
the artifact, renders the reviewed session content into a Word doc (reusing
`lambda_meeting_minutes.generate_word_document`), writes it under
`session_reports/`, optionally emails it, and writes the result the frontend
polls for at the artifact's `resultKey`.

Design: docs/superpowers/specs/2026-07-28-session-report-review-export-design.md §6.
"""
import datetime
import json
import logging
import os
import time
from io import BytesIO
from urllib.parse import unquote_plus

import boto3

import lambda_meeting_minutes
import llm_utils
import report_template
import transcript_window
from email_sender import get_sender
from lambda_meeting_minutes import generate_word_document

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Photo evidence budgets. Two caps, because either one alone leaves a hole: a
# per-topic cap does not bound a forty-topic day, and a total budget alone lets
# one photo-heavy topic eat everything before the later topics are reached.
# The numbers keep the doc something a site manager actually opens on a phone,
# and keep the render inside the Lambda's memory.
MAX_PHOTOS_PER_TOPIC = 4
MAX_PHOTO_BYTES_TOTAL = 12 * 1024 * 1024

_s3_client = None


def s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _is_day(artifact):
    return artifact.get("scope") == "day"


def _doc_key(artifact):
    """The Word doc lives under a DEDICATED session_reports/ prefix — NOT the
    nightly meeting_minutes/ path — so this on-demand Delivery-C artifact never
    collides with the report-generator's manifest machinery (BUG-18 sidestepped;
    authority-flip already de-dupes the nightly path). A day report takes the
    literal `day` segment where a session report takes its session id; the session
    routes refuse `day` as an id (spec 2026-09-15 F2)."""
    segment = "day" if _is_day(artifact) else artifact["sessionId"]
    return (f"session_reports/{artifact['folder']}/{artifact['date']}/"
            f"{segment}/{artifact['requestId']}.docx")


def _scope_ids_and_folders(artifact):
    """(session ids in scope, folders whose deletion mirror must be read).

    A day names every session it bundles, and every folder those sessions' topics
    were written under -- a merged meeting's rows live under the LEAD's folder, and
    so does its tombstone."""
    folder = artifact.get("folder")
    if _is_day(artifact):
        ids = [str(s).strip() for s in (artifact.get("sessionIds") or []) if str(s).strip()]
        folders = {f for f in (artifact.get("mirrorFolders") or []) if f}
        if folder:
            folders.add(folder)
        return ids, sorted(folders)
    sid = (artifact.get("sessionId") or "").strip()
    return ([sid] if sid else []), ([folder] if folder else [])


def _scope_result_fields(artifact):
    """What a day result must carry so the status route can re-check it."""
    if not _is_day(artifact):
        return {}
    ids, folders = _scope_ids_and_folders(artifact)
    return {"scope": "day", "sessionIds": ids, "mirrorFolders": folders}


def _humanize(key):
    return str(key).replace("_", " ").title()


def _fetch_photos(folder, date, filenames, budget):
    """Download a topic's photos as open streams, newest failure tolerated.

    The renderer does no I/O and must stay that way, so the bytes are fetched
    here and handed over. `budget` is a one-element list carrying the REMAINING
    total allowance, mutated as it is spent — the caller walks the topics in
    order, so an early photo-heavy topic cannot silently starve a later one of
    its cap, only of the shared budget.

    A photo that cannot be read costs only itself. The prose is the
    deliverable; the pictures support it, and losing the report because one
    object was deleted would be the wrong trade."""
    streams = []
    for name in (filenames or [])[:MAX_PHOTOS_PER_TOPIC]:
        if budget[0] <= 0:
            logger.info("photo budget spent; skipping %s", name)
            break
        key = f"users/{folder}/pictures/{date}/{name}"
        try:
            body = s3().get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
        except Exception:
            logger.warning("could not read photo %s; leaving it out", key)
            continue
        streams.append(BytesIO(body))
        budget[0] -= len(body)
    return streams


def _content_to_minutes(artifact):
    """Map the reviewed session content + the user's confirmed fields into the
    `minutes_data` shape `generate_word_document` consumes (T4).

    The fixed meeting-minutes layout has no arbitrary-placeholder slots (a real
    per-company template engine is the fast-follow, once the template store moves
    server-side — spec §2/§10.6), so the user's declared fields render generically
    as labeled Executive-Summary lines: any field the modal collects shows up with
    zero per-field code (spec §5). Our topic shape (`render_report_shape`) maps to
    the doc's shape 1:1 except action items' `responsible` -> `owner`."""
    content = artifact.get("content") or {}
    fields = artifact.get("fields") or {}

    folder, date = artifact.get("folder"), content.get("date") or artifact.get("date")
    budget = [MAX_PHOTO_BYTES_TOTAL]

    topics = []
    for t in (content.get("topics") or []):
        photos = _fetch_photos(folder, date, t.get("related_photos"), budget)
        topics.append({
            "topic_title": t.get("topic_title"),
            "category": t.get("category") or "general",
            "time_range": t.get("time_range") or "",
            "participants": t.get("participants") or [],
            "summary": t.get("summary") or "",
            "key_decisions": t.get("key_decisions") or [],
            "action_items": [{"action": a.get("action"),
                              "owner": a.get("responsible"),      # our shape -> the doc's shape
                              "deadline": a.get("deadline"),
                              "priority": a.get("priority") or "medium"}
                             for a in (t.get("action_items") or [])],
            "open_questions": [],
            # Absent, not empty, when there is nothing — so the renderer's
            # `if topic.get('photo_streams')` needs no second check.
            **({"photo_streams": photos} if photos else {}),
        })

    minutes = {
        "meeting_date": content.get("date"),
        "attendees": artifact.get("attendees") or content.get("participants") or [],
        "topics": topics,
    }
    exec_summary = [f"{_humanize(k)}: {v}" for k, v in fields.items()
                    if v not in (None, "", [], {})]
    if exec_summary:
        minutes["executive_summary"] = exec_summary

    title = artifact.get("title") or content.get("title") or "Session report"
    return minutes, title


def _write_result(result_key, payload):
    s3().put_object(Bucket=S3_BUCKET, Key=result_key,
                    Body=json.dumps(payload), ContentType="application/json")


def _send_email(artifact):
    recipients = artifact.get("recipients") or []
    title = artifact.get("title") or "Site report"
    subject = f"FieldSight report — {title}"
    body_text = (f"The report \"{title}\" for {artifact.get('date', '')} is ready in FieldSight.")
    sender = get_sender()
    for to in recipients:
        sender.send(to, subject, body_text)


def _session_was_deleted(artifact):
    """Is this session in the day's deletion mirror?

    Owner decision (2026-09-16): a mirror-read failure now FAILS CLOSED. This
    used to log and return False ("proceed as if nothing was deleted"), which
    could put a removed recording into a document and an email; that lenient
    posture is superseded for this path. On a read failure this raises, and
    `process_request`'s existing exception handling records a `status: error`
    result instead of rendering or mailing anything.

    `lambda_session_finalize._session_was_deleted` is a sibling that still has
    the old lenient posture -- this decision was scoped to the report worker,
    not to that sibling. `lambda_org_api._session_was_removed` was already
    strict, backing read endpoints where a failed check costs one reader one
    refresh; this function now matches that posture for the same reason a
    read endpoint does, even though the two are still separate functions.

    Both spellings are compared: the mirror carries whatever `sessionBase` the
    delete endpoint had, and this artifact's `sessionId` is bare hex.

    Logged before raising, because a permission fault here looks exactly like
    "nothing was deleted".

    A day request is deleted when ANY of its sessions is, in ANY of its folders'
    mirrors.
    """
    date = artifact.get("date")
    ids, folders = _scope_ids_and_folders(artifact)
    if not (date and ids and folders):
        return False
    try:
        import boto3

        import deletion_mirror
        client = boto3.client("s3")
        deleted = set()
        for folder in folders:
            deleted |= set(deletion_mirror.deleted_sessions(client, S3_BUCKET, folder, date))
    except Exception:
        logger.exception("report: deletion mirror unreadable for %s on %s -- failing closed, "
                         "not rendering or mailing", folders, date)
        raise
    for sid in ids:
        bare = sid[3:] if sid.startswith("sid") else sid
        if sid in deleted or bare in deleted or f"sid{bare}" in deleted:
            return True
    return False


# The function has Timeout: 900 and llm_utils retries up to four times at
# LLM_HTTP_TIMEOUT (300) each, so an unbounded ladder outlives the function and writes
# no result at all -- the poller then spins forever. Bound it well inside the timeout.
# Used only as a FALLBACK when the invocation carries no `context` (unit tests, or
# any caller that never got one) -- `context.get_remaining_time_in_millis()` is the
# authority whenever it is available (see `_model_budget_seconds`).
GENERATION_BUDGET_SECONDS = float(os.environ.get("GENERATION_BUDGET_SECONDS", "600"))

# Reserved out of whatever time is left for the docx render + the result write that
# must still happen AFTER the model answers -- without this reserve the model call
# could legitimately use every remaining millisecond and still get SIGKILLed one
# step from finishing, which writes no result at all.
RENDER_AND_WRITE_RESERVE_SECONDS = float(
    os.environ.get("RENDER_AND_WRITE_RESERVE_SECONDS", "30"))


def _remaining_seconds(context):
    """Lambda's own account of what is left on this invocation, in seconds.
    None when there is no context (a unit test calling process_request directly,
    or any caller that never got one) or it does not offer the method."""
    getter = getattr(context, "get_remaining_time_in_millis", None)
    if getter is None:
        return None
    try:
        return getter() / 1000.0
    except Exception:
        return None


def _model_budget_seconds(context):
    """Seconds available for whatever comes next (a read phase, or the model call),
    reserving RENDER_AND_WRITE_RESERVE_SECONDS for what must still happen after the
    model answers. Falls back to the fixed GENERATION_BUDGET_SECONDS when there is
    no context to ask -- the authority this replaces whenever a real one exists.
    Called again right before the model call so it reflects time the read phase
    actually spent, not an estimate made before it ran."""
    remaining = _remaining_seconds(context)
    if remaining is None:
        return GENERATION_BUDGET_SECONDS
    return remaining - RENDER_AND_WRITE_RESERVE_SECONDS


def _clock(date, hhmm):
    """A wall-clock time on the report's own date. No timezone conversion happens
    anywhere on this path (spec 2026-09-15 global constraints)."""
    return datetime.datetime.strptime("%s %s" % (date, hhmm), "%Y-%m-%d %H:%M")


def _action_items_for_prompt(content):
    """The actions the model is given: extraction's, not its own reading of the
    transcript. Owner and date are already recorded against them."""
    out = []
    for topic in (content.get("topics") or []):
        for a in (topic.get("action_items") or []):
            out.append({"action": a.get("action") or a.get("text"),
                        "owner": a.get("owner") or a.get("responsible"),
                        "deadline": a.get("deadline")})
    return out


def _prose_sections(text):
    """Split the model's markdown back into {title, paragraphs}. Anything before the
    first heading is kept under an empty title rather than dropped."""
    sections, current = [], {"title": "", "paragraphs": []}
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if line.startswith("#"):
            if current["title"] or current["paragraphs"]:
                sections.append(current)
            current = {"title": line.lstrip("#").strip(), "paragraphs": []}
        elif line.strip():
            current["paragraphs"].append(line.strip())
    if current["title"] or current["paragraphs"]:
        sections.append(current)
    return [s for s in sections if s["title"] or s["paragraphs"]]


def _put_document(artifact, buf):
    """Puts a generated docx at the same address a topics-assembled one would use,
    and returns its key -- mirrors the inline put in `process_request` below."""
    doc_key = _doc_key(artifact)
    s3().put_object(Bucket=S3_BUCKET, Key=doc_key,
                    Body=buf.getvalue(), ContentType=DOCX_CONTENT_TYPE)
    return doc_key


def _generate_document(artifact, context=None):
    """Returns (buffer, meta). Raises on anything that must not produce a document."""
    gen = artifact["generate"]
    template = report_template.load_template(gen["templateId"], int(gen["templateVersion"]))
    content = artifact.get("content") or {}
    date = artifact.get("date") or content.get("date")
    window = artifact.get("window") or {}
    win_from = _clock(date, window.get("from") or "00:00")
    win_to = _clock(date, window.get("to") or "23:59")

    # An excluded topic wholly outside THIS window cannot appear in it and cannot
    # be proven placeable-but-irrelevant unless its time_range actually parses --
    # see transcript_window.excluded_spans for the fail-closed ruling on the
    # unparseable case (that one still raises, window or no window).
    spans = transcript_window.excluded_spans(date, artifact.get("excludedTopics") or [],
                                             win_from, win_to)

    read_budget = _model_budget_seconds(context)
    if read_budget <= llm_utils.MIN_USEFUL_SECONDS:
        raise RuntimeError(
            "generation budget exhausted before reading the window: %.1fs left "
            "after reserving %.1fs for the render and result write"
            % (read_budget, RENDER_AND_WRITE_RESERVE_SECONDS))
    read_deadline = time.time() + read_budget

    client = s3()
    picked = transcript_window.select_keys(client, S3_BUCKET, artifact["folder"], date,
                                           win_from, win_to)
    turns = transcript_window.drop_spans(
        transcript_window.assemble(client, S3_BUCKET, picked, deadline=read_deadline), spans)
    if not turns:
        raise RuntimeError("no recorded speech in this window after exclusions")

    prompt = report_template.render_prompt(
        template,
        {"folder": artifact["folder"], "date": date,
         "from": window.get("from") or "00:00", "to": window.get("to") or "23:59",
         "recordings": len(picked)},
        _action_items_for_prompt(content),
        "\n".join(t["line"] for t in turns))

    # Recomputed from `context` (not reused from `read_budget`) because this is the
    # actual authority on what is left after the read phase ran, not an estimate
    # made before it did.
    model_budget = _model_budget_seconds(context)
    if model_budget <= llm_utils.MIN_USEFUL_SECONDS:
        raise RuntimeError(
            "generation budget exhausted before the model call: %.1fs left "
            "after reserving %.1fs for the render and result write"
            % (model_budget, RENDER_AND_WRITE_RESERVE_SECONDS))
    text, err = llm_utils.call_llm(prompt, max_tokens=8000, deadline=model_budget)
    if err or not (text or "").strip():
        raise RuntimeError(err or "empty answer from model")

    buf = lambda_meeting_minutes.generate_prose_document(
        artifact.get("title") or template.get("name") or "Report",
        "%s  %s - %s" % (date, window.get("from") or "00:00", window.get("to") or "23:59"),
        _prose_sections(text),
        _action_items_for_prompt(content))
    meta = {"generated": True, "templateId": template["template_id"],
            "templateVersion": template["version"], "model": llm_utils.active_model(),
            "promptChars": len(prompt)}
    return buf, meta


def process_request(artifact, context=None):
    """Render one enqueued request → Word doc (+ optional email) → result JSON.

    `context` (optional): the Lambda invocation context, threaded through to the
    generate path so it can bound itself against `get_remaining_time_in_millis()`
    instead of a fixed budget guessed at the top of the function. None in tests
    that call this directly, or in the non-generate path, which does not need it."""
    result_key = artifact["resultKey"]
    request_id = artifact.get("requestId")

    scope_fields = _scope_result_fields(artifact)
    if _is_day(artifact) and not scope_fields["sessionIds"]:
        # A day with no sessions cannot be checked against any mirror, so it is not rendered.
        _write_result(result_key, {"status": "error", "requestId": request_id,
                                   "error": "day request carries no sessionIds"})
        return

    # A session deleted before this request is rendered must not become a DOCX in
    # S3, and must not be emailed.
    #
    # `GET /report/status` stops the POLL from serving a removed session, but this
    # worker is S3-triggered: a delete landing between org-api's enqueue and this
    # run -- or an event redelivery hours later -- reaches neither guard. It is
    # the same defect fixed in `lambda_session_finalize` one lambda over, and it
    # was the last surface in the deletion enumeration still carrying it.
    #
    # Checked BEFORE the render, so nothing is produced from content that must
    # not leave. The outcome is RECORDED because the requester polls `resultKey`;
    # a silent skip leaves that poll spinning forever, which is a different bug
    # wearing this fix's clothes.
    # The mirror check itself must not escape uncaught: a read failure here used to
    # be swallowed inside _session_was_deleted (return False), which this branch
    # removed -- it now raises. That raise is caught HERE, not left to whichever
    # downstream try happens to wrap this call, so it guards BOTH the `generate`
    # path below and the legacy assemble-and-render path, not just one of them.
    try:
        deleted = _session_was_deleted(artifact)
    except Exception as exc:                      # noqa: BLE001 -- recorded, not retried
        logger.exception("report: deletion mirror check failed for %s", request_id)
        _write_result(result_key,
                      dict({"status": "error", "requestId": request_id,
                            "error": str(exc)}, **scope_fields))
        return
    if deleted:
        logger.info("report: %s was deleted -- not rendering, not sending", request_id)
        _write_result(result_key, {"status": "skipped", "requestId": request_id,
                                   "reason": "recording deleted", **scope_fields})
        return

    if artifact.get("generate"):
        try:
            buf, meta = _generate_document(artifact, context)
        except Exception as exc:                      # noqa: BLE001 -- recorded, not retried
            logger.exception("report: generation failed for %s", artifact.get("requestId"))
            _write_result(artifact["resultKey"],
                          dict({"status": "error", "requestId": artifact.get("requestId"),
                                "error": str(exc)}, **_scope_result_fields(artifact)))
            return
        doc_key = _put_document(artifact, buf)
        _write_result(artifact["resultKey"],
                      dict({"status": "done", "requestId": artifact.get("requestId"),
                            "docKey": doc_key, "emailed": False}, **meta,
                           **_scope_result_fields(artifact)))
        return

    try:
        minutes, title = _content_to_minutes(artifact)
        buf = generate_word_document(minutes, title)
        if buf is None:
            # DOCX layer missing / disabled — record it, don't crash the trigger.
            _write_result(result_key, {"status": "error", "requestId": request_id,
                                       "error": "document generation unavailable", **scope_fields})
            return
        doc_key = _doc_key(artifact)
        s3().put_object(Bucket=S3_BUCKET, Key=doc_key,
                        Body=buf.getvalue(), ContentType=DOCX_CONTENT_TYPE)
        emailed = False
        if artifact.get("deliver") == "email":
            _send_email(artifact)
            emailed = True
        _write_result(result_key, {"status": "done", "requestId": request_id,
                                   "docKey": doc_key, "emailed": emailed, **scope_fields})
    except Exception as e:
        logger.exception("session report generation failed for %s", request_id)
        _write_result(result_key, {"status": "error", "requestId": request_id, "error": str(e),
                                   **scope_fields})


def lambda_handler(event, context):
    for rec in event.get("Records", []):
        s3rec = rec.get("s3") or {}
        key = (s3rec.get("object") or {}).get("key")
        bucket = (s3rec.get("bucket") or {}).get("name") or S3_BUCKET
        if not key:
            continue
        key = unquote_plus(key)          # S3 notifications URL-encode the key
        obj = s3().get_object(Bucket=bucket, Key=key)
        artifact = json.loads(obj["Body"].read().decode("utf-8"))
        process_request(artifact, context)
    return {"ok": True}
