"""reconcile_worker.py -- cross-session status reconcile R0, the worker (Task 5).

MatcherFunction routes each `reconcile_requests/` artifact (reconcile_request.emit) here.
The worker reads THIS session's transcript with extract_session's own turn helpers, offers
the request's open items to the model as C1..Cn, and runs the hits through the pure judge in
status_reconcile. It proposes; lambda_suggestion_writer re-reads every live row and decides
what is written.

Fail closed: a model error, an unparseable answer, or a transcript whose segments were
listed but all came back unreadable raises, so the S3 event retries. Zero hits is the
expected, ordinary result and returns empty lists.

Deviation from spec §4.2, recorded in the Task 5 brief: the turn helpers are imported from
lambda_extract_session (lazily, inside process_request) instead of being lifted out of it.
That import graph has no psycopg -- tests/unit/test_reconcile_worker.py pins it.

The prompt's transcript is session_brief.render_turns(turns): raw speaker labels, not the
names extraction's render_transcript maps in. The quote check (_check_status_quote) runs on
the same `turns`, so the text and the [HH:MM:SS] clock the model copies from are identical to
what is verified; only the speaker column differs from the extraction prompt.

The model call is bounded by the Lambda's remaining time (`deadline_s`, from the handler's
context), not by raising LLM_HTTP_TIMEOUT on the matcher: that would change the programme
path's calls too.

Logs carry ids and counts only, never quote, transcript or item text.
"""
import json
import logging
import os

import llm_utils
import status_reconcile

logger = logging.getLogger(__name__)

#: The artifact prefix MatcherFunction routes here. reconcile_request.PREFIX, repeated so
#: the matcher never imports reconcile_request (in-VPC; it pulls psycopg).
PREFIX = "reconcile_requests/"
CALLER = "status_reconcile"
MAX_TOKENS = 4000


def _empty(counts):
    return {"status_suggestions": [], "pick_cards": [], "counts": counts}


def _parse(raw, n):
    """The model's hits, or None. A bare JSON answer goes straight to parse_hits; a fenced
    or prose-wrapped one is unwrapped first (quietly: extract_json's failure log quotes the
    answer, which carries transcript text)."""
    hits = status_reconcile.parse_hits(raw, n)
    if hits is not None:
        return hits
    data = llm_utils.extract_json(raw or "", quiet=True)
    if data is None:
        return None
    return status_reconcile.parse_hits(json.dumps(data), n)


def process_request(req: dict, input_key: str, call=None, bucket=None,
                    deadline_s=None) -> dict:
    """One reconcile_requests/ artifact body -> {status_suggestions, pick_cards, counts}.

    `call` defaults to llm_utils.call_llm, resolved at call time so a monkeypatched
    llm_utils is honoured. `bucket` is the lake bucket the transcripts live in (the
    matcher's S3_BUCKET); it defaults to the S3_BUCKET env var. `deadline_s` (seconds from
    now, or None for no bound) is passed to the model call as its `deadline`; a non-positive
    one raises before the call, so the S3 event retries instead of the runtime killing us.

    Each status suggestion / pick card carries the writer's event fields (Task 3), with
    `site_id` from the request and `run = "reconcile:R0:{extracted_at}"`."""
    import lambda_extract_session as les
    import session_brief

    call = call or llm_utils.call_llm
    bucket = bucket or os.environ.get("S3_BUCKET", "")
    site_id = req["site_id"]
    date = req["date"]
    user_folder = req["user_folder"]
    run = f"reconcile:R0:{req['extracted_at']}"
    candidates = list(req.get("candidates") or [])

    if not candidates:
        logger.info("reconcile %s: no candidates", input_key)
        return _empty({"offered": 0, "kept": 0, "picks": 0, "dropped_by": {}})

    keys = les.gather_session_segments(bucket, user_folder, date, req["session_base"])
    if not keys:
        # The extraction this request came from read these transcripts; none left means
        # they were removed since. Nothing to reconcile against, and a retry cannot help.
        logger.warning("reconcile %s: no transcript segments for the session", input_key)
        return _empty({"offered": 0, "kept": 0, "picks": 0, "dropped_by": {},
                       "no_transcript": 1})
    turns, _source_filenames, _stats = les.assemble_session_turns(bucket, keys)
    if not turns:
        # assemble_session_turns swallows a failed GetObject per segment, so listed keys
        # with no turns is more likely a denied read than silence. Fail closed.
        raise RuntimeError(
            f"reconcile {input_key}: {len(keys)} segment(s) listed but no readable turns")

    if deadline_s is not None and deadline_s <= 0:
        raise RuntimeError(f"reconcile {input_key}: no time left for the model call "
                           f"(deadline {deadline_s:.0f}s)")
    prompt = status_reconcile.build_prompt(candidates, session_brief.render_turns(turns))
    raw, error = call(prompt, max_tokens=MAX_TOKENS, force_json=True, enable_thinking=True,
                      deadline=deadline_s, caller=CALLER)
    if raw is None:
        raise RuntimeError(f"reconcile {input_key}: model call failed: {error}")

    hits = _parse(raw, len(candidates))
    if hits is None:
        # The answer itself is never put in the message: it quotes the transcript.
        raise RuntimeError(f"reconcile {input_key}: unparseable model answer "
                           f"({len(raw)} chars)")

    outcome = status_reconcile.judge(
        hits, candidates,
        lambda quote, at: les._check_status_quote(quote, at, turns, date, user_folder))

    suggestions = [dict(s, site_id=site_id, run=run) for s in outcome.suggestions]
    picks = [dict(p, site_id=site_id, run=run) for p in outcome.picks]
    logger.info("reconcile %s site=%s candidates=%d turns=%d counts=%s", input_key, site_id,
                len(candidates), len(turns), json.dumps(outcome.counts, sort_keys=True))
    return {"status_suggestions": suggestions, "pick_cards": picks,
            "counts": outcome.counts}
