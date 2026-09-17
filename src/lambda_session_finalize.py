"""lambda_session_finalize.py — Tier-0 auto-finalize: the ≤2-min post-meeting
confirmation email (voice-timeliness spec 2026-07-27, §Tier-0).

When a recording's grace window elapses without a resume (state machine
open→pending_close→finalizing→sent, repositories/meeting_session.py), the recorder
gets a short email summarising what was captured — so they can confirm or correct
it before leaving site.

Split (CLAUDE.md BUG-36: in-VPC lambdas can't reach SES): an in-VPC CLAIM step —
fired by the grace one-shot — does the version-CAS `claim_finalize` (the idempotency
guard: a mis-touch stop→resume bumps `version`, so the scheduled finalize no-ops)
and enqueues a request; a non-VPC WORKER builds this email from the session's rolling
summary + still-open to-dos and sends it via SES (email_sender). This module holds
the pure email builder now; the claim step, the worker, and the one-shot wiring land
in following slices. Pure at import — boto3 / email_sender are imported lazily by the
worker, never at module load.
"""
import html as _html
import json
import logging
import os
import re
import time
from urllib.parse import unquote_plus

logger = logging.getLogger()
# INFO, set HERE. The Lambda runtime leaves the root logger at WARNING, and this
# module's decisions are all logged at INFO -- including which source a final
# email's rows came from (brief or extraction). The level used to be raised only
# as a side effect of importing email_sender, which happens at SEND time, i.e.
# AFTER those decisions were logged; so on TEST (2026-09-18) the email went out
# and the one line saying whether it used the brief was silently dropped, while
# the "[email:ses] sent" line right after it printed. A decision nobody can see
# is indistinguishable from one that never ran.
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")

# Which summariser the finalize re-summary uses. Off = the terse rolling
# summariser this has always used. On = session_brief, which writes the
# narrative first and derives the to-dos from it; it returns the same
# {summary, open_todos} keys, so the email below is unchanged either way.
SESSION_BRIEF = os.environ.get("SESSION_BRIEF", "false").lower() == "true"
BRIEF_PREFIX = "session_brief/"
FINALIZE_RESULTS_PREFIX = "session_finalize_results/"
# handoff-sync plan §2.2: how long the FINAL email waits for the brief that
# lambda_finalize_claim asked for CONCURRENTLY with the final extraction
# (§2.1), and how often it checks. Both env-tunable so the wait can move
# without a code change -- the same reasoning as every other timing constant
# in this file.
BRIEF_POLL_SECONDS = float(os.environ.get("BRIEF_POLL_SECONDS", "10"))
BRIEF_WAIT_SECONDS = float(os.environ.get("BRIEF_WAIT_SECONDS", "90"))
#: Request kinds that arrive WITH the rows the email should show, so this worker
#: renders them as handed instead of summarising the session again.
#:
#:   final    the session's final extraction, enqueued by item-writer once those
#:            rows are durable in Aurora -- the same account of the meeting the
#:            website and the reports show
#:   rolling  the backstop's rows, for a session whose final extraction never
#:            arrived
#:   updated  the ONE merged summary every member of a group must receive
#:
#: A request with NO kind is the path this function has always taken: re-derive a
#: complete summary from the transcripts. That is a SECOND summariser over one
#: meeting -- the reason the same commitment could be worded one way in the email
#: and another way on the site -- and it is what the kinds above retire.
RENDERED_KINDS = ("final", "rolling", "updated")

# ---- handoff-sync plan §7.4: is an extraction action item already said, in
# different words, by a brief task? One shared constant, named identically to
# its (independent) frontend counterpart, so neither side can drift without a
# renamed test failing to find it.
#
# Deliberately conservative and biased AWAY from dropping a commitment (§10):
# a tie at the threshold, or either side producing no tokens at all, reads as
# NOT represented -- the cost of that bias is an occasional duplicate row, and
# a duplicate is visible and annoying where a silently dropped commitment is
# neither.
BRIEF_MATCH_JACCARD_THRESHOLD = 0.30
BRIEF_MATCH_STOP_WORDS = frozenset(
    "the a an and or to of for on in at is are be by with from that this it as".split())


def _match_tokens(text):
    """Lower-case `[a-z0-9]+` tokens of 3+ characters, stop words dropped
    (plan §7.4) -- the comparison unit for the Jaccard test below."""
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) >= 3 and w not in BRIEF_MATCH_STOP_WORDS}


def _is_represented(text, brief_texts):
    """True iff `text`'s best Jaccard overlap against any of `brief_texts`
    strictly exceeds `BRIEF_MATCH_JACCARD_THRESHOLD`.

    Strictly, not `>=`: a tie at the threshold is the boundary case plan §7.4
    calls out by name ("a tie... counts as NOT represented"), so it is decided
    the same way as an empty token set -- towards keeping the row."""
    tokens = _match_tokens(text)
    if not tokens:
        return False
    best = 0.0
    for brief_text in brief_texts:
        other = _match_tokens(brief_text)
        union = tokens | other
        if not union:
            continue
        overlap = len(tokens & other) / len(union)
        if overlap > best:
            best = overlap
    return best > BRIEF_MATCH_JACCARD_THRESHOLD


#: Coverage-rule time parsing (plan §7.1), kept local rather than reused from
#: `chunking.parse_time_range`: the frontend's parallel implementation (same
#: plan, §7) accepts en dash, em dash OR a plain hyphen as the range
#: separator, and `chunking.parse_time_range` only normalises the en dash --
#: it is tuned for real lake `time_range` data, not for parity with a sibling
#: repo. Getting this ONE acceptance set wrong would make an otherwise-good
#: topic_range read as unparsable on one surface and not the other, which is
#: exactly the silent drift plan §7.5 exists to prevent.
_RANGE_SEP_RE = re.compile(r"\s*[–—-]\s*")   # en dash / em dash / hyphen
_HMS_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


def _parse_hms(value):
    """'HH:MM' or 'HH:MM:SS' -> seconds-of-day, or None if unparsable.

    'HH:MM' means HH:MM:00 -- no invented seconds (parity ruling, 2026-09-18):
    a topic whose range ends '13:41' covers a brief task at 13:41:00, not one
    at 13:41:59."""
    m = _HMS_RE.match((value or "").strip())
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    # SHAPE IS NOT A CLOCK. The regex accepts "25:00" and "09:99"; the plan
    # (§7.1) says anything that is not two parsable clock times is unparsable
    # and therefore never covers a topic. Without this the two surfaces
    # disagreed: fieldsight-ui `parseClockSeconds` rejects these, so an
    # out-of-range range from an upstream arithmetic slip would silently
    # suppress a topic row in the email and keep it in Preview & copy.
    if h > 23 or mi > 59 or s > 59:
        return None
    return h * 3600 + mi * 60 + s


def _parse_hms_range(time_range):
    """A topic's raw `time_range` -> (start_sec, end_sec), or None.

    Anything that isn't exactly two `HH:MM`/`HH:MM:SS` sides joined by one
    separator is unparsable -- and unparsable means never covered (§7.1), not
    a window guessed wide open."""
    if not isinstance(time_range, str):
        return None
    parts = _RANGE_SEP_RE.split(time_range.strip())
    if len(parts) != 2:
        return None
    start, end = _parse_hms(parts[0]), _parse_hms(parts[1])
    if start is None or end is None:
        return None
    return start, end


def _topic_covered(topic_range, brief_ats):
    """Plan §7.1: a topic is covered when at least one brief task's `at` falls
    inside the topic's own `time_range`, both ends inclusive.

    An unparsable `time_range` is never covered. A brief task with no `at`
    covers nothing: `_parse_hms(None)` returns None, so it is skipped by the
    same check with no special-casing needed. Judged only against the SAME
    session's own brief tasks -- this function, and the `final` email it
    serves, are already scoped to one session's artifact and one session's
    polled brief, so there is no cross-session set to accidentally draw from
    (parity ruling, 2026-09-18, point 4)."""
    window = _parse_hms_range(topic_range)
    if window is None:
        return False
    start, end = window
    for at in brief_ats:
        at_sec = _parse_hms(at)
        if at_sec is None:
            continue
        if start <= at_sec <= end:
            return True
    return False


def _display_name(folder):
    """The recording owner's display name, derived from the S3 folder ("Ben_Lin"
    -> "Ben Lin"). Plumbing only (2026-09-17 plan, task B2): this is what lets
    the brief prompt name the owner (session_brief.build_brief_prompt) -- what
    the prompt DOES with the name is a later task. Returns None for a blank
    folder rather than an empty string, so a caller can `if owner_name:` it.

    Separators are collapsed rather than substituted one-for-one: a NULL
    `last_name` produces the folder "Ben_UCPK_" (a shape this repo has seen),
    and a literal replace turns that into "Ben UCPK " -- which reaches the brief
    prompt as "...belongs to Ben UCPK ." A folder made only of separators has no
    name in it, so it returns None like a blank one."""
    return " ".join(folder.replace("_", " ").split()) or None if folder else None


def _clean_todos(open_todos):
    """Keep only to-dos with real text; normalise responsible + due to a value or
    None. Each item carries {text, responsible, due, at} for the structured render.

    `why` is dropped here even when the caller's dict still carries one (2026-09-17,
    "the code stops carrying `why` and `basis`"): the confirmation email no longer
    renders a per-item context line at all (see `build_confirmation_email` below),
    so there is nothing downstream for it to reach.
    """
    # A speaker label is not a name, on EVERY row -- not only the brief's. The
    # brief path already filtered it; the extraction path, which is the only one
    # prod runs, did not, so the email showed "spk_0" where Preview & copy showed
    # "—" for the same row. One filter, reused, so the two cannot drift:
    # fieldsight-ui email-preview-modal.js `isSpeakerLabel` mirrors this pattern.
    import session_brief
    out = []
    for t in (open_todos or []):
        text = (t.get("text") or "").strip()
        if text:
            out.append({"text": text,
                        "responsible": session_brief._real_name(t.get("responsible")),
                        "due": (t.get("due") or None),
                        "at": (t.get("at") or None),
                        # "action" (someone owes something) or "topic" (something
                        # was discussed and nobody promised anything). Dropping it
                        # here would have cost the distinction the table is built
                        # on, since this function rebuilds each row from scratch.
                        "kind": ("topic" if t.get("kind") == "topic" else "action"),
                        # handoff-sync plan §8: the owning topic's raw time_range,
                        # carried through for the same reason as `kind` above --
                        # this function rebuilds each row from scratch, and the
                        # trap that once dropped `kind` here is exactly what a new
                        # key silently missed would repeat.
                        "topic_range": (t.get("topic_range") or None)})
    return out


def _ordered_rows(todos):
    """Actions first, topics last.

    ONE table, not two. A session that produced no action item is the common
    case -- 11 of 16 measured -- and it used to be emailed as a header and one
    sentence saying nothing was captured, while the recording plainly had
    content. Its topics now appear as rows of their own, with no owner and no
    due date, and they sink below anything anyone actually owes."""
    return ([t for t in todos if t["kind"] != "topic"]
            + [t for t in todos if t["kind"] == "topic"])


def _pipe_cell(value):
    """A cell's text for the plain-text pipe table (plan §1.7), mirrored
    BYTE-FOR-BYTE from the frontend's `cell()` in
    `fieldsight-ui/scripts/composites/email-preview-modal.js` (`renderEmailText`)
    -- that file is the other half of this contract, and a reader who gets one
    surface's table cannot tell it apart from the other's. A `\\r?\\n` run
    collapses to a single space (a wrapped sentence stays reachable rather than
    breaking the row mid-table); a literal `|` is escaped so it can't be read
    as a column boundary."""
    return re.sub(r"\r?\n", " ", str(value)).replace("|", "\\|")


def _pipe_row(cells):
    """`| a | b | c |` -- mirrors the frontend's `pipe()` in the same file:
    leading and trailing pipes, space-padded, so the two tables are identical
    lines, not merely equivalent ones."""
    return "| " + " | ".join(_pipe_cell(c) for c in cells) + " |"


def build_confirmation_email(*, date=None, time_range=None, site_name=None,
                             summary=None, open_todos=None):
    """(subject, body_text, body_html) for the recorder's confirmation email, built
    from the session's still-open to-dos. Pure — no I/O. To-dos with no text are
    dropped; all HTML content is escaped so transcript text can't inject markup. The
    subject carries date + meeting time range so the recorder can tell WHICH meeting
    it is (multiple recordings a day otherwise share one subject), and the same stamp
    repeats on the body's Date line so the email states WHEN without relying on the
    client showing the subject.

    2026-08-10: the narrative summary is NO LONGER RENDERED. What the recorder acts
    on is the action table; the prose restated it at length and pushed the table
    below the fold. `summary` is still accepted — process_finalize_request still
    chooses between the fresh and the rolling one, and it still reaches the stored
    result — it simply does not appear in the email. A session with no to-dos now
    says so explicitly: with the prose gone it would otherwise be a header and
    nothing else, which reads as a broken send."""
    stamp = " ".join(p for p in (date, time_range) if p)
    subject = "FieldSight — your site notes" + (f" ({stamp})" if stamp else "")
    todos = _ordered_rows(_clean_todos(open_todos))
    no_todos_note = "Nothing was captured for this recording."
    # A topic row is not a task: nobody owes it, so its owner and due date are
    # N/A rather than the em dash an UNASSIGNED action carries. The two mean
    # different things and the table has to keep them apart -- "—" invites
    # someone to pick the task up; "N/A" says there is no task here.
    na = "N/A"

    lines = ["Here's what we captured from your recording — reply or open FieldSight "
             "to correct anything before you leave site.", ""]
    if site_name:
        lines.append(f"Site: {site_name}")
    if stamp:
        lines.append(f"Date: {stamp}")
    if todos:
        # Plan §1.7: the plain-text flavour is a pipe table with the same three
        # columns and a header separator -- leading AND trailing pipes, exactly
        # as the frontend's Preview & copy renders it, not the old bulleted
        # list. One table shape on both surfaces, one line format.
        lines += ["", _pipe_row(("AGENDA ITEM", "ASSIGNED", "DUE DATE")), "| --- | --- | --- |"]
        for t in todos:
            if t["kind"] == "topic":
                who, due = na, na
            else:
                who = t["responsible"] or "—"
                due = t["due"] or "—"
            lines.append(_pipe_row((t["text"], who, due)))
    else:
        lines += ["", no_todos_note]
    body_text = "\n".join(lines).rstrip() + "\n"

    esc = _html.escape
    parts = ["<p>Here's what we captured from your recording — reply or open FieldSight "
             "to correct anything before you leave site.</p>"]
    meta = []
    if site_name:
        meta.append(f"<strong>Site:</strong> {esc(site_name)}")
    if stamp:
        meta.append(f"<strong>Date:</strong> {esc(stamp)}")
    if meta:
        parts.append("<p>" + "<br>".join(meta) + "</p>")
    if todos:
        def _row(t):
            topic = t["kind"] == "topic"
            who = na if topic else (esc(t["responsible"]) if t["responsible"] else "—")
            when = na if topic else (esc(t["due"]) if t["due"] else "—")
            # Greyed, so a reader scanning for what they owe can stop at the last
            # black row; the context is still there for a reader who wants it. No
            # per-item line under the title: `why` stopped being carried (#864).
            tone = ';color:#666' if topic else ''
            return ("<tr>"
                    f'<td style="padding:6px;border-bottom:1px solid #eee{tone}">'
                    f'{esc(t["text"])}</td>'
                    f'<td style="padding:6px;border-bottom:1px solid #eee;'
                    f'vertical-align:top{tone}">{who}</td>'
                    f'<td style="padding:6px;border-bottom:1px solid #eee;'
                    f'vertical-align:top{tone}">{when}</td>'
                    "</tr>")

        rows = "".join(_row(t) for t in todos)
        # No <h3>Items</h3>: the header row below carries the table's title now
        # (plan §2.4) -- one heading, not two.
        parts.append(
            '<table role="presentation" cellspacing="0" cellpadding="0" '
            'style="border-collapse:collapse;width:100%;font-size:14px">'
            '<thead><tr style="text-align:left;border-bottom:2px solid #ccc">'
            # Literal header text (plan §0/§1.1/§2.4): "AGENDA ITEM / ASSIGNED /
            # DUE DATE" on BOTH surfaces -- the email and the frontend's Preview
            # & copy render the same three words, or the two surfaces the plan
            # exists to sync would drift on the very first thing a reader sees.
            '<th style="padding:6px">AGENDA ITEM</th><th style="padding:6px">ASSIGNED</th>'
            '<th style="padding:6px">DUE DATE</th></tr></thead>'
            f"<tbody>{rows}</tbody></table>")
    else:
        parts.append(f"<p>{esc(no_todos_note)}</p>")
    body_html = "\n".join(parts)

    return subject, body_text, body_html


# ============================================================
# Non-VPC send worker — S3-triggered on session_finalize_requests/
# ============================================================

def _already_sent(result_id):
    """True when this session's confirmation email has already gone out.

    The worker is S3-triggered, S3 notifies on EVERY put of the request key --
    an overwrite included -- and this function sets no MaximumRetryAttempts, so
    the Lambda async default of two retries applies on top. Anything that writes
    the request a second time (a re-drive, a race between the two writers, a
    hand-run `aws s3 cp`) was therefore a second email to the recorder, and
    nothing here asked.

    A read that FAILS answers False, deliberately. Losing a confirmation
    permanently is worse than the double this guards, and the double has a
    second guard (the conditional put on the request key). But it is logged:
    the absent grant is exactly how this class of bug stays invisible, and
    `NoSuchKey` -- the ordinary "not sent yet" -- is not logged at all.
    """
    import boto3
    from botocore.exceptions import ClientError
    key = f"{FINALIZE_RESULTS_PREFIX}{result_id}.json"
    try:
        obj = boto3.client("s3").get_object(Bucket=S3_BUCKET, Key=key)
        return (json.loads(obj["Body"].read().decode("utf-8")) or {}).get("status") == "sent"
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return False
        logger.warning("finalize: could not read %s (%s) -- sending rather than "
                       "risk losing the confirmation", key, e)
        return False
    except Exception as e:
        logger.warning("finalize: unreadable result %s (%s) -- sending anyway", key, e)
        return False


def _default_write_result(session_id, payload):
    """Record the send outcome the in-VPC sweep's reconcile pass reads."""
    import boto3
    boto3.client("s3").put_object(
        Bucket=S3_BUCKET, Key=f"{FINALIZE_RESULTS_PREFIX}{session_id}.json",
        Body=json.dumps(payload), ContentType="application/json")


def _complete_summary(artifact, summarize=None):
    """Re-summarise the WHOLE session from its transcripts at finalize time. The
    enqueued rolling summary can be stale/partial — A throttles mid-meeting and the
    session may grow after its last tick, and C can claim before the next one. But
    finalize runs after close + grace, so every transcript is in by now; re-gathering
    yields a COMPLETE summary. Returns {summary, open_todos} or None (missing keys /
    no turns / LLM failure) so the caller falls back to the rolling summary. Imports
    are lazy (extract_session/rolling pull boto3 + llm_utils) — this worker is non-VPC
    so the LLM is reachable; SessionFinalizeFunction carries the LLM env."""
    folder, date, sid = artifact.get("folder"), artifact.get("date"), artifact.get("sessionId")
    if not (folder and date and sid):
        return None
    try:
        import lambda_extract_session as ex
        keys = ex.gather_session_segments(S3_BUCKET, folder, date, "sid" + sid)
        turns, _sources = ex.assemble_deduped_turns(S3_BUCKET, keys)
        if not turns:
            return None
        if summarize is None:
            if SESSION_BRIEF:
                import functools
                import session_brief
                summarize = functools.partial(session_brief.brief_from_turns,
                                              owner_name=_display_name(folder))
            else:
                import lambda_rolling_summary as rs
                summarize = rs.summarize_turns
        result = summarize(turns)
        # A brief is worth more than the two keys the email reads, and this is
        # the only point in the pipeline where the whole session exists as one
        # clean turn stream. Store it before handing the caller its two keys.
        # Best-effort: the email must still go out if the write fails.
        if result and result.get("sections"):
            _store_brief(folder, date, sid, result)
        return result
    except Exception:
        logger.exception("finalize: complete re-summary failed for %s — using rolling summary", sid)
        return None


def _store_brief(folder, date, session_id, brief):
    """Write the session brief to S3, mirroring session_rolling/'s layout so the
    read path is the one already proven for the rolling summary. Never raises:
    losing the stored copy must not cost the recorder their email."""
    try:
        import boto3
        key = f"{BRIEF_PREFIX}{folder}/{date}/sid{session_id}/latest.json"
        boto3.client("s3").put_object(
            Bucket=S3_BUCKET, Key=key,
            Body=json.dumps(brief, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json")
        logger.info("session brief stored at %s", key)
    except Exception:
        logger.exception("session brief could not be stored for %s "
                         "-- the email is unaffected", session_id)


def _session_was_deleted(artifact):
    """Is this session in the day's deletion mirror? Never raises.

    THERE ARE TWO OF THESE AND THE DIFFERENCE IS DELIBERATE. `lambda_org_api.
    _session_was_removed` answers the same question for the three read endpoints
    (brief, rolling, report status) and RAISES when the mirror is unreadable.
    This one returns False and proceeds.

    Neither is the mistake, and neither should be "made consistent" with the
    other:

    * There, a failed check costs one reader one refresh, and answering "removed"
      when the truth is "could not check" would turn a broken grant on
      `redactions/` into a silent total outage wearing the costume of normal
      operation.
    * Here, it costs a recorder their ONLY confirmation email for a session that
      was probably never deleted -- and this worker records failures instead of
      retrying them, so failing closed loses the email permanently.

    They are separate functions rather than one because this lambda is non-VPC
    and cannot import org-api's module; the shared part is `deletion_mirror`,
    which both use. If a third caller appears with the strict posture, it belongs
    in org-api's helper, not here.

    The mirror is exactly what this worker is supposed to read: it is the copy of
    the answer written for the lambdas that hold no database connection, and this
    one is non-VPC. Both spellings are matched -- the mirror carries whatever
    `sessionBase` the delete endpoint had, and the artifact's `sessionId` is bare
    hex -- because two spellings of a session are equal as sessions and not as
    strings.

    Failure reads as "not deleted", which is the lenient direction and is argued
    at the call site. It is LOGGED, because a permission fault here looks exactly
    like "nothing was deleted" and that indistinguishability has cost this
    project three separate silent breakages.
    """
    folder, date = artifact.get("folder"), artifact.get("date")
    sid = (artifact.get("sessionId") or "").strip()
    if not (folder and date and sid):
        return False
    try:
        import boto3

        import deletion_mirror
        deleted = deletion_mirror.deleted_sessions(
            boto3.client("s3"), S3_BUCKET, folder, date)
    except Exception:
        logger.exception("finalize: deletion mirror unreadable for %s/%s -- proceeding "
                         "as if nothing was deleted, which may mail a removed recording",
                         folder, date)
        return False
    return sid in deleted or f"sid{sid}" in deleted


def _brief_key(folder, date, session_id):
    return f"{BRIEF_PREFIX}{folder}/{date}/sid{session_id}/latest.json"


def _read_brief(folder, date, session_id):
    """The stored session brief, or None if it is not there YET.

    Distinguishes "not written yet" (NoSuchKey/404 -- the ordinary, expected
    answer on every poll but the last) from any OTHER read error. This repo
    has repeatedly shipped a 403 read as "absent" (CLAUDE.md: S3 missing
    ListBucket answers AccessDenied, not NoSuchKey, for a key that was never
    written) -- and here that would mean every final email silently falls
    back to the request's own rows while every test stays green, because a
    permissions fault and "the brief just isn't ready" look identical unless
    this tells them apart. A non-404 error is logged LOUDLY -- at ERROR, not
    the quiet default -- naming the key, so a missing GetObject grant on
    session_brief/* shows up on the very first poll instead of reading as
    "brief never arrives"."""
    import boto3
    from botocore.exceptions import ClientError
    key = _brief_key(folder, date, session_id)
    try:
        obj = boto3.client("s3").get_object(Bucket=S3_BUCKET, Key=key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in ("NoSuchKey", "404") or status == 404:
            return None
        logger.error("finalize: could not read brief %s (%s, HTTP %s) -- this is a "
                    "READ FAILURE, not 'not ready yet'. Check the deployed role's "
                    "s3:GetObject (and ListBucket) grant on session_brief/*.",
                    key, code or "unknown", status)
        return None
    except Exception as e:
        logger.error("finalize: could not read brief %s (%s) -- treating as absent "
                    "this poll", key, e)
        return None


def _poll_for_brief(folder, date, session_id, *, read_brief=None, sleep=None,
                    poll_seconds=None, wait_seconds=None):
    """Poll for the concurrently-enqueued session brief (§2.1) up to
    BRIEF_WAIT_SECONDS, checking every BRIEF_POLL_SECONDS. Returns the brief
    dict, or None if it never appeared within the budget.

    `read_brief` and `sleep` are injectable so a test drives this without
    a real clock or a real S3 call: a fake `read_brief` that returns None a
    fixed number of times before a dict, and a no-op `sleep`, prove the
    retry/give-up shape in milliseconds. Attempt count is computed from
    poll/wait rather than a real deadline for the same reason -- deterministic
    under an injected sleep, not dependent on wall-clock drift inside the
    loop."""
    read_brief = read_brief or _read_brief
    sleep = sleep or time.sleep
    poll = BRIEF_POLL_SECONDS if poll_seconds is None else poll_seconds
    wait = BRIEF_WAIT_SECONDS if wait_seconds is None else wait_seconds
    attempts = max(1, int(wait // poll) + 1) if poll > 0 else 1
    for i in range(attempts):
        brief = read_brief(folder, date, session_id)
        if brief is not None:
            return brief
        if i < attempts - 1:
            sleep(poll)
    return None


def _rows_from_brief_or_request(artifact, request_todos, *, poll_brief=None):
    """The `openTodos` a `kind == "final"` email actually renders (§2.2, then
    merged per §7).

    If SESSION_BRIEF is on and a brief with at least one USABLE task turns up
    within the wait, its tasks become the ACTION rows (assignee mapped through
    the same speaker-label filter session_brief already applies -- `spk_0` is
    not a name). Otherwise the request's rows are used unchanged. "Usable" is
    judged on the rows this function actually renders (`brief["open_todos"]`,
    after `session_brief.to_session_summary` drops any task whose text is
    blank), not on the raw `brief["tasks"]` count -- a brief whose tasks are
    all blank-text must fall back exactly like a brief with none at all: an
    empty table where the extraction had rows would read as broken, not as
    "nothing to do" (plan §1.3).

    When the brief wins, two more things happen before the request's rows are
    merged in (plan §6/§7):

    * a TOPIC row is dropped when a brief task's `at` falls inside that
      topic's own `time_range` (§7.1/§7.2) -- the brief already covers it, so
      keeping the topic row too is the "same thing appears twice" defect
      (§5.1) this plan exists to close;
    * every extraction ACTION row that carries a non-empty `due` and is not
      textually represented in the brief's rows is appended after the
      brief's own rows, in the extraction's own order (§7.3) -- the "dated
      commitment the brief missed" defect (§5.2).

    Exactly one log line, naming which source was used and why, and (when the
    brief wins) how many topic rows were suppressed as covered and how many
    extraction rows were back-filled -- the same posture as every other
    silent-fallback point in this pipeline."""
    session_id = artifact.get("sessionId")
    if not SESSION_BRIEF:
        logger.info("finalize: %s email rows from the request -- SESSION_BRIEF is off",
                    session_id)
        return request_todos
    folder, date = artifact.get("folder"), artifact.get("date")
    if not (folder and date and session_id):
        logger.info("finalize: %s email rows from the request -- missing folder/date "
                    "to look up a brief", session_id)
        return request_todos
    brief = (poll_brief or _poll_for_brief)(folder, date, session_id)
    if brief is None:
        logger.info("finalize: %s email rows from the request -- no brief within %ss",
                    session_id, BRIEF_WAIT_SECONDS)
        return request_todos
    # Gate on the rows actually being RENDERED, not on raw brief["tasks"]:
    # session_brief.to_session_summary builds open_todos by dropping any task
    # whose text is blank after stripping. A brief whose tasks all have blank
    # text would pass a `tasks`-non-empty gate and then render zero action
    # rows, discarding the request's real ones -- exactly the "empty table
    # where the extraction had rows" this function exists to prevent (§1.3).
    action_rows = [dict(t, kind="action") for t in (brief.get("open_todos") or [])]
    if not action_rows:
        logger.info("finalize: %s email rows from the request -- brief has no usable "
                    "action rows (%d raw task(s), 0 with non-blank text)",
                    session_id, len(brief.get("tasks") or []))
        return request_todos

    brief_ats = [r.get("at") for r in action_rows]
    all_topic_rows = [t for t in (request_todos or []) if t.get("kind") == "topic"]
    topic_rows = [t for t in all_topic_rows
                 if not _topic_covered(t.get("topic_range"), brief_ats)]
    suppressed = len(all_topic_rows) - len(topic_rows)

    brief_texts = [r.get("text") for r in action_rows]
    extraction_action_rows = [t for t in (request_todos or []) if t.get("kind") != "topic"]
    backfilled = [t for t in extraction_action_rows
                 if (t.get("due") or "").strip()
                 and not _is_represented(t.get("text"), brief_texts)]

    logger.info("finalize: %s email action rows from the brief (%d row(s), %d "
                "back-filled from the extraction), %d topic row(s) kept from the "
                "request (%d suppressed as covered by a brief task)",
                session_id, len(action_rows) + len(backfilled), len(backfilled),
                len(topic_rows), suppressed)
    return action_rows + backfilled + topic_rows


def _process_brief_request(artifact, *, complete_summary=None):
    """kind == "brief": produce and store the session's brief, and nothing else.

    Enqueued by lambda_finalize_claim._request_extraction CONCURRENTLY with the
    final extraction request (handoff-sync plan §2.1), so the brief has a head
    start on the final email that will go looking for it (§2.2). None of the
    delivery machinery applies here: no recipient is required, no email is
    built or sent, no session_finalize_results/ is written (nothing for
    reconcile to settle -- this request never puts a session into
    `finalizing`), and no `_already_sent` check (that guards the recorder's
    inbox, not a cache write). `_complete_summary` stores the brief itself
    (via `_store_brief`) as a side effect; producing it IS this branch's job."""
    session_id = artifact.get("sessionId")
    if not SESSION_BRIEF:
        logger.info("finalize: %s brief request skipped -- SESSION_BRIEF is off", session_id)
        return {"status": "skipped", "reason": "SESSION_BRIEF off", "sessionId": session_id}
    if _session_was_deleted(artifact):
        logger.info("finalize: %s was deleted -- not building a brief", session_id)
        return {"status": "skipped", "reason": "recording deleted", "sessionId": session_id}
    result = (complete_summary if complete_summary is not None else _complete_summary)(artifact)
    if result is None:
        logger.info("finalize: %s brief request produced nothing (no turns, or the "
                    "summariser failed) -- nothing stored", session_id)
        return {"status": "skipped", "reason": "no brief produced", "sessionId": session_id}
    return {"status": "ok", "sessionId": session_id}


def process_finalize_request(artifact, *, send=None, write_result=None, complete_summary=None,
                             already_sent=None, poll_brief=None):
    """Build + SES-send the recorder's confirmation email from one enqueued finalize
    request (the in-VPC claim step wrote it), then record the outcome to
    session_finalize_results/{sid}.json — the in-VPC sweep's reconcile pass reads it
    and moves the session finalizing -> sent/failed (this non-VPC worker can't touch
    Aurora, CLAUDE.md BUG-36). A send failure is RECORDED (status 'error'), not
    re-raised: re-raising would S3-retry the trigger and risk a double-send. `send` /
    `write_result` are injectable; they default to email_sender + an S3 write (both
    lazy so the module stays pure at import). A request with no recipient is skipped —
    the claim step already marked that session failed.

    kind == "brief" is handled FIRST, before the recipient check: it is not a
    delivery at all (see `_process_brief_request`)."""
    if artifact.get("kind") == "brief":
        return _process_brief_request(artifact, complete_summary=complete_summary)
    recipient = (artifact.get("recipient") or "").strip()
    if not recipient:
        return {"status": "skipped", "reason": "no recipient"}
    session_id = artifact.get("sessionId")

    # A recording deleted before this request is processed must not be emailed,
    # and must not have a fresh brief written from its transcripts.
    #
    # There was no coordination between the two paths at all: neither this module
    # nor `lambda_finalize_claim` held a deletion check, and
    # `delete_recordings_endpoint` does not touch `meeting_session` or the
    # enqueued request. `_complete_summary` re-gathers through
    # `gather_session_segments`, which does not filter either -- so the deleted
    # session would be rebuilt in full and mailed.
    #
    # The window is narrow and real: the recordings list is populated at upload,
    # well before finalize runs, and the sweep re-drives a finalize that failed.
    #
    # SEVERITY, so it is neither over- nor under-read: the recipient is the
    # RECORDER, the person who deleted it. Not a disclosure to a third party --
    # the product telling someone their recording was removed and then mailing it
    # back. A promise broken, not a breach.
    #
    # LENIENT, unlike the brief endpoint, and the asymmetry is deliberate. There
    # a failed check costs one reader one refresh. Here it costs a recorder their
    # only confirmation for a session that was probably never deleted -- and this
    # worker RECORDS failures rather than retrying them (re-raising would
    # S3-retry the trigger and risk a double-send), so failing closed would lose
    # the email permanently. Logged loudly instead.
    if _session_was_deleted(artifact):
        logger.info("finalize: %s was deleted -- not sending, not storing a brief",
                    session_id)
        outcome = {"status": "skipped", "reason": "recording deleted",
                   "sessionId": session_id}
        # RECORDED, not merely returned. The in-VPC sweep reconciles `finalizing`
        # from this file; a skip that writes nothing leaves the session stuck in
        # `finalizing` forever, which is a different bug wearing this fix's
        # clothes.
        (write_result if write_result is not None else _default_write_result)(
            session_id, outcome)
        return outcome

    # Prefer a FRESH complete summary re-derived from the full transcript over the
    # enqueued rolling summary (which can be stale/partial — see _complete_summary);
    # fall back to the rolling summary on any failure.
    summary, todos = artifact.get("summary"), artifact.get("openTodos")
    is_updated = artifact.get("kind") == "updated"
    # Before the re-summary, so a duplicate event costs neither an email nor an
    # LLM call. Keyed exactly as the result is WRITTEN below (the `-updated`
    # suffix included), or a member's group email would be checked against the
    # solo key and vice versa.
    result_id = f"{artifact.get('sessionId')}-updated" if is_updated else artifact.get("sessionId")
    if (already_sent if already_sent is not None else _already_sent)(result_id):
        logger.info("finalize: %s was already sent -- not sending again", result_id)
        return {"status": "skipped", "reason": "already sent", "sessionId": session_id}
    # A request of a RENDERED kind already carries the rows to show. For
    # `updated` that has always been load-bearing: re-deriving would summarise
    # this member's own SOLO transcripts (_complete_summary re-gathers
    # `sid{sessionId}`), so the N members would each get a summary of what THEY
    # heard under a subject saying the meeting was merged -- N different bodies,
    # N LLM calls, and the one thing the merge promised quietly not delivered.
    # `final` and `rolling` join it for the same reason, one meeting wide: the
    # rows in the email should be the rows in the record.
    if artifact.get("kind") not in RENDERED_KINDS:
        fresh = (complete_summary if complete_summary is not None else _complete_summary)(artifact)
        if fresh:
            summary, todos = fresh.get("summary", summary), fresh.get("open_todos", todos)
    elif artifact.get("kind") == "final":
        todos = _rows_from_brief_or_request(artifact, todos, poll_brief=poll_brief)
    subject, text, html = build_confirmation_email(
        date=artifact.get("date"), time_range=artifact.get("timeRange"),
        site_name=artifact.get("siteName"), summary=summary, open_todos=todos)
    if is_updated:
        # A second email with a different body must not read as a duplicate of
        # the first. The recipient already had one for this meeting.
        subject = f"Updated: {subject}"
        # And its result goes to its own key. reconcile reads
        # session_finalize_results/{sessionId}.json to settle a CLAIMED session;
        # a member can be counted settled by quietness while still `finalizing`,
        # so an updated result on the solo key could move that session to `sent`
        # on the wrong evidence.
        session_id = f"{session_id}-updated"
    if send is None:
        from email_sender import get_sender
        send = get_sender().send
    if write_result is None:
        write_result = _default_write_result
    try:
        send(recipient, subject, text, html)
    except Exception as e:
        # Logged as well as recorded. The result file was the only trace, so 36
        # prod rejections (SES sandbox: unverified recipient) looked like nothing
        # at all in the logs.
        logger.error("finalize: send failed for session %s: %s", session_id, e)
        write_result(session_id, {"status": "error", "sessionId": session_id, "error": str(e)})
        return {"status": "error", "recipient": recipient, "sessionId": session_id}
    write_result(session_id, {"status": "sent", "sessionId": session_id, "recipient": recipient})
    return {"status": "sent", "recipient": recipient, "sessionId": session_id}


def lambda_handler(event, context):
    """S3 event on session_finalize_requests/*.json — send each enqueued
    confirmation email. Non-VPC (reaches SES, CLAUDE.md BUG-36)."""
    import boto3
    s3 = boto3.client("s3")
    results = []
    for rec in event.get("Records", []):
        s3rec = rec.get("s3") or {}
        key = (s3rec.get("object") or {}).get("key")
        bucket = (s3rec.get("bucket") or {}).get("name")
        if not key:
            continue
        key = unquote_plus(key)                 # S3 notifications URL-encode the key
        obj = s3.get_object(Bucket=bucket, Key=key)
        artifact = json.loads(obj["Body"].read().decode("utf-8"))
        results.append(process_finalize_request(artifact))
    return {"results": results}
