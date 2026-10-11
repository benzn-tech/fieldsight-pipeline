"""One place that knows the shape of every lake key. Callers get named fields, never an index."""
import re

_DATE = r"(?P<day>\d{4}-\d{2}-\d{2})"
_FOLDER = r"(?P<folder>[^/]+)"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
SID_RE = re.compile(r"sid(?P<sid>[0-9a-f]{32})")
_BARE_SID_RE = re.compile(r"^[0-9a-f]{32}$")

_TENANT = [
    ("media",        rf"^users/{_FOLDER}/(?:video|audio|pictures)/{_DATE}/[^/]+$"),
    ("segment",      rf"^audio_segments/{_FOLDER}/{_DATE}/[^/]+$"),
    ("web_video",    rf"^web_video/{_FOLDER}/{_DATE}/[^/]+$"),
    ("transcript",   rf"^transcripts/{_FOLDER}/{_DATE}/[^/]+$"),
    ("extraction",   rf"^extractions/{_FOLDER}/{_DATE}/[^/]+$"),
    ("report",       rf"^reports/{_DATE}/{_FOLDER}/[^/]+$"),
    ("embedding",    rf"^embeddings/{_DATE}/{_FOLDER}/[^/]+$"),
    ("reindex",      rf"^reindex_(?:requests|vectors)/{_DATE}/{_FOLDER}/[^/]+$"),
    ("session_report", rf"^session_report(?:s|_requests|_results)/{_FOLDER}/{_DATE}/.+$"),
    ("session_aux",  rf"^session_(?:rolling|brief)/{_FOLDER}/{_DATE}/.+$"),
    ("redaction",    rf"^redactions/{_FOLDER}/{_DATE}/[^/]+$"),
    ("voice_ask",    rf"^voice_ask/{_FOLDER}/{_DATE}/[^/]+$"),
    ("trace",        rf"^traces/{_DATE}/{_FOLDER}/[^/]+$"),
    ("keyframe_req", rf"^keyframe_requests/{_FOLDER}/{_DATE}/[^/]+$"),
    ("report_request", rf"^report_requests/{_FOLDER}/[^/]+$"),
    ("photo_selection", rf"^report_photo_selection/{_FOLDER}/{_DATE}\.json$"),
]
_SESSION = [  # folder only in the payload; the session id is the only handle
    ("session_marker", r"^(?:extraction_requests|extraction_pending|extraction_empty|"
                       r"session_finalize_requests|session_finalize_results)/(?P<base>[^/]+)\.json$"),
]
_SITE_OR_COMPANY = [
    ("voice",       rf"^voice/(?P<company_id>{_UUID})/(?P<site_id>{_UUID})/[^/]+$"),
    ("voiceprint",  rf"^voiceprint_requests/(?P<company_id>{_UUID})/[^/]+/[^/]+$"),
    ("match",       rf"^match_requests/(?P<site_id>{_UUID})/{_DATE}/[^/]+$"),
    ("weather",     rf"^weather/(?P<site_id>{_UUID})/{_DATE}/[^/]+$"),
    ("programme",   rf"^programmes/(?P<site_id>{_UUID})/[^/]+$"),
]
_LAKE_WIDE = [rf"^reports/{_DATE}/[^/]+_report\.[a-z]+$", rf"^reports/{_DATE}/summary_report[^/]*$"]
_MEETING_MINUTES = re.compile(r"^meeting_minutes/.+$")
_SITE_REPORT = re.compile(rf"^reports/{_DATE}/sites/(?P<site_slug>[^/]+)/.+$")
_GLOBAL = re.compile(r"^(?:config|models|app-releases|scripts|pending_downloads|download_claims|other|"
                     r"org-assets|notices|lake_manifest|lake_index|exports)/")

_TENANT_RE = [(k, re.compile(p)) for k, p in _TENANT]
_SESSION_RE = [(k, re.compile(p)) for k, p in _SESSION]
_SITE_RE = [(k, re.compile(p)) for k, p in _SITE_OR_COMPANY]
_LAKE_WIDE_RE = [re.compile(p) for p in _LAKE_WIDE]


def _result(scope, kind, **kw):
    out = {"scope": scope, "kind": kind, "folder": None, "day": None, "session_base": None,
           "sid": None, "site_id": None, "company_id": None}
    out.update(kw)
    return out


def _sid_of(name):
    m = SID_RE.search(name)
    if m:
        return m.group("sid")
    return name if _BARE_SID_RE.match(name) else None


def parse(key):
    """Return the named fields of a lake key, or None for a key of no known family
    (the backfill reports those as unknown_shape; the parser never guesses)."""
    if not key or not isinstance(key, str):
        return None
    if _GLOBAL.match(key):
        return _result("global", "global")
    if _MEETING_MINUTES.match(key):
        return _result("lake_wide", "meeting_minutes")
    for rx in _LAKE_WIDE_RE:
        if rx.match(key):
            return _result("lake_wide", "lake_wide")
    m = _SITE_REPORT.match(key)
    if m:
        return _result("tenant", "site_report", day=m.group("day"), site_slug=m.group("site_slug"))
    for kind, rx in _TENANT_RE:
        m = rx.match(key)
        if m:
            g = m.groupdict()
            return _result("tenant", kind, folder=g.get("folder"), day=g.get("day"),
                           sid=_sid_of(key.rsplit("/", 1)[-1]))
    for kind, rx in _SESSION_RE:
        m = rx.match(key)
        if m:
            base = m.group("base")
            return _result("tenant", kind, session_base=base, sid=_sid_of(base))
    for kind, rx in _SITE_RE:
        m = rx.match(key)
        if m:
            g = m.groupdict()
            return _result("tenant", kind, day=g.get("day"), site_id=g.get("site_id"),
                           company_id=g.get("company_id"))
    return None
