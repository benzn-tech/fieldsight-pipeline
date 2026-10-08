"""
Lambda: sitesync-api -- the LEGACY gateway (fieldsight-prod-api, /api/{proxy+}).

Retired data routes (closed 2026-10, retire-the-legacy-gateway phase B). Each
answers HTTP 410 {"error": "gone", "use": "<replacement>"} for every role and
method, before any storage access. The web moved to org-api on 2026-10-07; the
last data-route call from a browser was 2026-10-07 16:18 NZ.

Still served (proxies into ask-agent; ACL is enforced downstream by rag-search
via caller_sub, and every proxy refuses an empty sub with 401):
  GET  /api/health
  POST /api/ask  /api/ask/voice  /api/ask/corroborate  /api/search

Environment Variables:
    S3_BUCKET           reported by /api/health only
    ASK_AGENT_FUNCTION  fieldsight-ask-agent
    ASK_INVOKE_TIMEOUT  seconds (default 26)
"""
import os
import json
import logging
import re
import boto3
import botocore.exceptions
from botocore.config import Config
from datetime import datetime

# Pure module, no boto3 -- safe to import eagerly. Moved out of this file
# (2026-09-17, ask-conversation-memory Task 3) so lambda_ask_agent can share
# the same cleaner without importing this whole handler module. Re-exported
# under the SAME names at module level: existing tests reference
# fapi.MAX_VOICE_HISTORY_TURNS / fapi.MAX_VOICE_HISTORY_CHARS.
from ask_history import (
    _clean_voice_history, MAX_VOICE_HISTORY_TURNS, MAX_VOICE_HISTORY_CHARS,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# No S3 client and no DynamoDB resource: this gateway no longer touches either.

lambda_client = boto3.client('lambda')
# Task 8 review fix: this Config was originally applied to the SHARED
# module-level `lambda_client` above, which regressed every other route that
# reuses it -- corroborate_answer's target (src/corroboration.py
# HARD_STOP_SECONDS, default 27s, its stage budgets re-cut from measurement to
# fill exactly 27s) could finish between 26s and 27s and now got killed by
# THIS read_timeout instead; ask_voice's _voice_answer (STT + RAG + TTS) has
# no measured ceiling to check the claim against either. So this client is
# used ONLY by ask_question, below -- read_timeout BELOW ApiFunction's own
# Timeout (Task 7: ApiFunction=28, AskAgentFunction=27), so a hung Ask Agent
# invoke fails HERE, in code that can name it, instead of the runtime killing
# ApiFunction first and leaving a bare `Task timed out` as the only trace
# (spec SS4.8). retries=0: a synchronous user-facing invoke must not silently
# double the wait. corroborate_answer, ask_voice and search_topics keep using
# the plain `lambda_client` above, unchanged from before this task.
_LAMBDA_INVOKE_TIMEOUT = int(os.environ.get("ASK_INVOKE_TIMEOUT", "26"))
ask_lambda_client = boto3.client('lambda', config=Config(
    read_timeout=_LAMBDA_INVOKE_TIMEOUT,
    connect_timeout=5,
    retries={"max_attempts": 0},
))

S3_BUCKET = os.environ.get('S3_BUCKET', 'fieldsight-data-509194952652')
ASK_AGENT_FUNCTION = os.environ.get('ASK_AGENT_FUNCTION', 'fieldsight-ask-agent')


def ok(body, status=200):
    return {
        'statusCode': status,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Headers': 'Content-Type,Authorization',
            'Access-Control-Allow-Methods': 'GET,POST,PATCH,OPTIONS',
        },
        'body': json.dumps(body, default=str),
    }

def error(message, status=400):
    return ok({'error': message}, status)



def get_caller_identity(event):
    """Who is calling, from the authorizer's claims only. No directory lookup:
    the DynamoDB `fieldsight-users` profile and the frozen user_mapping are gone
    with the data routes. `role` is the claim's custom:role (or '') and is used
    for the LEGACY_CALL log line only -- nothing here grants access."""
    claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {}) or {}
    email = claims.get('email', '')
    return {'sub': claims.get('sub', ''), 'email': email,
            'name': claims.get('name', email),
            'role': claims.get('custom:role', '') or '',
            'display_name': ''}


def resolve_user_display_name(caller):
    if caller['display_name']:
        return caller['display_name'].replace(' ', '_')
    return ''


# ── POST /api/ask ───────────────────────────────────────────

def ask_question(body, caller):
    """Proxy question to Ask Agent Lambda. ACL is enforced downstream by
    rag-search via caller_sub (BUG-39 WS2) -- this proxy no longer gates."""
    # Fail closed: without a sub the ask-agent would fall to a company-blind S3 read.
    if not caller.get('sub'):
        return error('sign-in required', 401)

    question = body.get('question', '').strip()
    date = body.get('date', '')
    user = body.get('user', '')
    scope = body.get('scope', 'both')
    topic_id = body.get('topic_id', None)

    if not question:
        return error('Missing question')
    # `date` is read on the RAG path since scoped Ask (2026-09-15), but ONLY when
    # the body also carries `scoped: true`: with no time word in the question it
    # then narrows retrieval to that day. Without `scoped` the RAG path ignores
    # `date` exactly as before, because the deployed UI already sends it. The
    # legacy S3 path reads it too.
    #
    # `tz` is what the RAG path reads: an IANA zone id, not a date. The zone is
    # sent instead of a computed date because NZ and AU are both on daylight
    # saving for part of the year and do not switch on the same day, so a date
    # computed anywhere but in the caller's own zone is wrong for one of them.
    # Absent stays absent -- '' would be a blank every reader has to special-case.

    # REMOVED (BUG-39 WS2): legacy DynamoDB user/role gate. The RAG ACL is
    # enforced downstream by caller_sub -> rag-search (graded scope.visible_scope,
    # WS3). 'user' is optional soft context only.
    #   was: if not user: user = resolve_user_display_name(caller)
    #        if not user: return error('Missing user')
    #        if caller['role'] == 'worker': user = resolve_user_display_name(caller)
    #        elif user and not can_access_user_data(caller, user): return error('Access denied to this user', 403)

    payload = {
        'user': user,
        'question': question,
        'scope': scope,
        # Cognito sub bridge: rag-search resolves this via get_user_by_sub()
        # to scope retrieval to the caller's accessible sites (org ACL).
        'caller_sub': caller.get('sub', ''),
    }
    if date:
        payload['date'] = date
    if body.get('tz'):
        payload['tz'] = body['tz']
    if topic_id is not None:
        payload['topic_id'] = topic_id

    # Scoped Ask (spec 2026-09-15 §4.1): forwarded as sent, validated by the Ask
    # Agent, enforced by rag-search. Absent stays absent -- never ''.
    for field in ('site_id', 'author_folder', 'topic_row_id'):
        if body.get(field) not in (None, ''):
            payload[field] = body[field]
    # The gate that makes the Ask Agent honour `date` (spec §3). Forwarded as
    # sent when truthy; the Ask Agent accepts only JSON true.
    if body.get('scoped'):
        payload['scoped'] = body['scoped']

    # Same cleaner as the voice route: one set of caps, one set of key names.
    # ABSENT, never an empty list -- see ask_voice, and the agent's
    # history_turns count, which would otherwise mean two things.
    raw_history = body.get('history')
    history = _clean_voice_history(raw_history)
    if history:
        payload['history'] = history
    if isinstance(raw_history, list) and len(raw_history) != len(history):
        logger.warning("ask: dropped %d of %d history turns",
                       len(raw_history) - len(history), len(raw_history))
    elif raw_history is not None and not isinstance(raw_history, list):
        logger.warning("ask: history was %s, not a list",
                       type(raw_history).__name__)

    try:
        resp = ask_lambda_client.invoke(
            FunctionName=ASK_AGENT_FUNCTION,
            InvocationType='RequestResponse',
            Payload=json.dumps(payload)
        )
        # An unhandled exception inside the Ask Agent lambda comes back as a
        # 200 InvocationType response with FunctionError set and a Payload
        # containing {errorMessage, errorType, stackTrace}. Never pass that
        # straight through to the client -- it leaks internal stack traces.
        if resp.get('FunctionError'):
            logger.error(f"Ask agent returned FunctionError: {resp.get('FunctionError')}")
            return error('Ask agent error', 500)

        result = json.loads(resp['Payload'].read().decode('utf-8'))

        # The Ask Agent returns API Gateway format {statusCode, body}
        if 'body' in result:
            return result
        # Or direct invocation format
        return ok(result)
    except botocore.exceptions.ReadTimeoutError:
        # The gap this fixes (spec SS4.8): with no Config on lambda_client,
        # botocore's default read timeout outlived ApiFunction's own Timeout,
        # so the runtime killed this function before this except could run --
        # the only trace was a bare `Task timed out`. Named here instead, and
        # 504 (not 500/502) so a hung agent is distinguishable from every
        # other invoke failure below. The body text is generic on purpose:
        # the web client replaces every Ask failure with its own reassuring
        # line regardless of status code (Task 11) -- this status is for us.
        logger.error("ask agent read timeout after %ss", _LAMBDA_INVOKE_TIMEOUT)
        return error('Ask temporarily unavailable', 504)
    except Exception as e:
        logger.error(f"Ask agent invocation failed: {e}")
        return error('Ask temporarily unavailable', 502)


# ── POST /api/ask/corroborate ─────────────────────

# An answer long enough to be a transcript is a transcript. The corroboration
# steps read the answer, not the recording, and a caller that pastes the wrong
# thing here would be aiming meeting content at an external search.
MAX_CORROBORATE_CHARS = 8000


def corroborate_answer(body, caller):
    """Proxy an answer to the Ask Agent for external corroboration.

    Modelled on ask_question, including the FunctionError guard verbatim: that
    guard exists so an unhandled exception in the agent does not return a stack
    trace to the client, and this route needs it for exactly the same reason.
    """
    # Fail closed: without a sub the ask-agent would fall to a company-blind S3 read.
    if not caller.get('sub'):
        return error('sign-in required', 401)

    question = body.get('question', '').strip()
    answer = body.get('answer', '').strip()

    if not question:
        return error('Missing question')
    if not answer:
        return error('Missing answer')
    if len(answer) > MAX_CORROBORATE_CHARS or len(question) > MAX_CORROBORATE_CHARS:
        return error('Question or answer too long to corroborate')

    payload = {
        'question': question,
        'answer': answer,
        'mode': 'corroborate',
        'caller_sub': caller.get('sub', ''),
    }

    try:
        resp = lambda_client.invoke(
            FunctionName=ASK_AGENT_FUNCTION,
            InvocationType='RequestResponse',
            Payload=json.dumps(payload)
        )
        if resp.get('FunctionError'):
            logger.error(f"Ask agent returned FunctionError: {resp.get('FunctionError')}")
            return error('Ask agent error', 500)

        result = json.loads(resp['Payload'].read().decode('utf-8'))
        if 'body' in result:
            return result
        return ok(result)
    except Exception as e:
        logger.error(f"Corroborate invocation failed: {e}")
        return error(f'Ask agent error: {e}', 500)


# ── POST /api/ask/voice (SP-Ask) ─────────────────────────────

# ~15s of 128kbps AAC ≈ 240KB ≈ 320K base64 chars; 1.5M chars (~1.1MB decoded)
# is generous headroom while still rejecting absurd payloads early.
MAX_VOICE_AUDIO_B64 = 1_500_000


def ask_voice(body, caller):
    """Hands-free voice ask (SP-Ask): forward the base64 clip to the Ask Agent,
    which chains DashScope STT -> RAG (caller_sub ACL, voice prompt, Haiku) ->
    DashScope TTS and returns {transcript, answerText, audioBase64, audioFormat}.

    Routed here (ApiFunction, non-VPC) and NOT on lambda_org_api: the org API
    is in-VPC with no NAT and no lambda VPC endpoint (BUG-36), so it can
    neither reach DashScope nor invoke AskAgentFunction. This function already
    holds LambdaInvokePolicy on AskAgentFunction and the /api/{proxy+} route.
    caller identity comes from the Cognito authorizer claims -- never from the
    client body (mirrors ask_question's caller_sub bridge)."""
    # Fail closed: without a sub the ask-agent would fall to a company-blind S3 read.
    if not caller.get('sub'):
        return error('sign-in required', 401)

    audio_b64 = body.get('audio')
    if not audio_b64 or not isinstance(audio_b64, str):
        return error('Missing audio (base64 clip required)')
    if len(audio_b64) > MAX_VOICE_AUDIO_B64:
        return error('Audio too large', 413)

    payload = {
        'mode': 'voice',
        'audio': audio_b64,
        'format': body.get('format') or 'm4a',
        'caller_sub': caller['sub'],
    }
    if body.get('tz'):
        payload['tz'] = body['tz']   # see ask_question: an IANA zone, not a date

    # ABSENT, never an empty list. Every device in the field today sends no
    # history, and "I have no history" is a different statement from "my
    # conversation is empty" -- collapsing them would make the agent's
    # history_turns count mean two things, and would break the sibling test that
    # pins this payload to exactly four keys.
    raw_history = body.get('history')
    history = _clean_voice_history(raw_history)
    if history:
        payload['history'] = history
    if isinstance(raw_history, list) and len(raw_history) != len(history):
        # The only trace that a device is sending turns we cannot use. Silent
        # dropping is correct behaviour and a terrible diagnostic.
        logger.warning(
            "voice ask: dropped %d of %d history turns",
            len(raw_history) - len(history), len(raw_history))
    elif raw_history is not None and not isinstance(raw_history, list):
        logger.warning("voice ask: history was %s, not a list",
                       type(raw_history).__name__)
    try:
        resp = lambda_client.invoke(
            FunctionName=ASK_AGENT_FUNCTION,
            InvocationType='RequestResponse',
            Payload=json.dumps(payload)
        )
        # Same FunctionError posture as ask_question: never pass a crashed
        # agent's {errorMessage, stackTrace} payload through to the client.
        if resp.get('FunctionError'):
            logger.error(f"Voice ask agent FunctionError: {resp.get('FunctionError')}")
            return error('Ask agent error', 500)
        result = json.loads(resp['Payload'].read().decode('utf-8'))
        if 'body' in result:
            return result
        return ok(result)
    except Exception as e:
        logger.error(f"Voice ask invocation failed: {e}")
        return error(f'Ask agent error: {e}', 500)


# ── POST /api/search ─────────────────────────────────────────

def search_topics(body, caller):
    """Retrieve-only topic search: forward to the Ask Agent with mode=search.
    Returns a ranked topic list (no LLM synthesis). ACL is enforced downstream
    in rag-search (org accessible sites via caller_sub), so no per-user gate is
    needed here. date_from/date_to are an optional inclusive range."""
    # Fail closed: without a sub the ask-agent would fall to a company-blind S3 read.
    if not caller.get('sub'):
        return error('sign-in required', 401)

    question = (body.get('question') or '').strip()
    if len(question) < 2:
        return ok({'results': [], 'count': 0})

    payload = {
        'mode': 'search',
        'question': question,
        'user': resolve_user_display_name(caller),  # soft context only
        'caller_sub': caller.get('sub', ''),
        'k': int(body.get('k', 30)) if str(body.get('k', 30)).isdigit() else 30,
    }
    date_from = body.get('date_from')
    date_to = body.get('date_to')
    if date_from and not re.match(r'^\d{4}-\d{2}-\d{2}$', str(date_from)):
        return error('Invalid date_from (expected YYYY-MM-DD)')
    if date_to and not re.match(r'^\d{4}-\d{2}-\d{2}$', str(date_to)):
        return error('Invalid date_to (expected YYYY-MM-DD)')
    if date_from:
        payload['date_from'] = date_from
    if date_to:
        payload['date_to'] = date_to
    if body.get('site'):
        payload['site'] = body['site']  # project-scoped search (Ask omits site)

    try:
        resp = lambda_client.invoke(
            FunctionName=ASK_AGENT_FUNCTION,
            InvocationType='RequestResponse',
            Payload=json.dumps(payload),
        )
        if resp.get('FunctionError'):
            logger.error(f"Search agent returned FunctionError: {resp.get('FunctionError')}")
            return error('Search error', 500)
        result = json.loads(resp['Payload'].read().decode('utf-8'))
        if 'body' in result:
            return result
        return ok(result)
    except Exception as e:
        logger.error(f"Search invocation failed: {e}")
        return error(f'Search error: {e}', 500)



def health_check(params):
    return ok({'status': 'ok', 'service': 'sitesync-api', 'version': '2.0',
               'bucket': S3_BUCKET, 'timestamp': datetime.utcnow().isoformat() + 'Z'})


def _ua_family(headers):
    """Coarse client family from the User-Agent header; never the raw string."""
    ua = ''
    for k, v in (headers or {}).items():
        if str(k).lower() == 'user-agent':
            ua = v or ''
            break
    if not ua:
        return 'none'
    low = ua.lower()
    if 'okhttp' in low or 'dalvik' in low:
        return 'android'
    if 'cfnetwork' in low or 'darwin' in low or 'iphone' in low or 'ios' in low:
        return 'ios'
    if low.startswith('mozilla/'):
        return 'browser'
    return 'other'



# Closed data routes -> the org-api route that replaced each ("none" = no
# replacement). Every method, every role, answered before any storage access.
CLOSED_ROUTES = {
    '/api/timeline': '/api/org/timeline',
    '/api/dates': '/api/org/dates',
    '/api/media/presigned-url': '/api/org/media/presigned-url',
    '/api/reports/history': '/api/org/reports/history',
    '/api/reports/generate': 'POST /api/org/reports/regenerate',
    '/api/users': '/api/org/members',
    '/api/sites': '/api/org/sites',
    '/api/site-users': '/api/org/sites/{id}/members',
    '/api/transcripts': '/api/org/transcripts',
    '/api/audio-segments': '/api/org/audio-segments',
    '/api/video-segments': '/api/org/video-segments',
    '/api/recording-stats': 'none',
    '/api/actions': 'none',
    '/api/actions/toggle': 'PATCH /api/org/action-items/{id}',
}


def gone(path):
    return ok({'error': 'gone', 'use': CLOSED_ROUTES[path]}, 410)


# Literal routes this dispatcher knows. LEGACY_CALL logs only these; any other
# path is client-controlled (a 404) and is logged as 'other'. The closed routes
# stay here so their 410s remain visible in the logs.
KNOWN_ROUTES = frozenset(CLOSED_ROUTES) | frozenset({
    '/api/ask', '/api/ask/voice', '/api/ask/corroborate', '/api/search',
})


def lambda_handler(event, context):
    logger.info(f"Request: {event.get('httpMethod','GET')} {event.get('path','/')}")
    method = event.get('httpMethod', 'GET').upper()
    path = event.get('path', '/')
    if method == 'OPTIONS':
        return ok({'message': 'CORS OK'})
    if path == '/api/health':
        return health_check({})
    caller = get_caller_identity(event)
    # Who still calls this gateway? Route/method/role/client family only --
    # no query-param or body values (tenant content).
    logger.info("LEGACY_CALL %s", json.dumps({
        'route': path if path in KNOWN_ROUTES else 'other', 'method': method, 'role': caller.get('role', ''),
        'has_sub': bool(caller.get('sub')),
        'ua': _ua_family(event.get('headers')),
    }, sort_keys=True))
    if path in CLOSED_ROUTES:
        return gone(path)
    body = {}
    if method in ('POST', 'PATCH', 'PUT') and event.get('body'):
        try: body = json.loads(event['body'])
        except Exception: body = {}
    try:
        if path == '/api/ask' and method == 'POST': return ask_question(body, caller)
        elif path == '/api/ask/voice' and method == 'POST': return ask_voice(body, caller)
        elif path == '/api/ask/corroborate' and method == 'POST': return corroborate_answer(body, caller)
        elif path == '/api/search' and method == 'POST': return search_topics(body, caller)
        else: return error(f'Not found: {method} {path}', 404)
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)

        return error(f'Internal error: {e}', 500)