"""One place that knows the shape of every lake key. Callers get named fields, never an index."""
import re

_DATE = r"(?P<day>\d{4}-\d{2}-\d{2})"
_FOLDER = r"(?P<folder>[^/]+)"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
SID_RE = re.compile(r"sid(?P<sid>[0-9a-f]{32})")
# `-r{n}`: a late-chunk reopen's round (session_scope.round_suffix), before `-notes`.
_SID_SEGMENT_RE = re.compile(r"^(?P<sid>[0-9a-f]{32})(?:-r[0-9]+)?(?:-(?:updated|notes))?$")
_UUID_RE = re.compile(rf"^{_UUID}$")

_TENANT = [
    ("media",        rf"^users/{_FOLDER}/(?:video|audio|pictures)/{_DATE}/[^/]+$"),
    ("segment",      rf"^audio_segments/{_FOLDER}/{_DATE}/[^/]+$"),
    ("web_video",    rf"^web_video/{_FOLDER}/{_DATE}/[^/]+$"),
    ("transcript",   rf"^transcripts/{_FOLDER}/{_DATE}/[^/]+$"),
    ("extraction",   rf"^extractions/{_FOLDER}/{_DATE}/[^/]+$"),
    ("report",       rf"^reports/{_DATE}/{_FOLDER}/[^/]+$"),
    ("embedding",    rf"^embeddings/{_DATE}/{_FOLDER}/[^/]+$"),
    ("reindex",      rf"^reindex_(?:requests|vectors)/{_DATE}/{_FOLDER}/[^/]+$"),
    ("search_index", rf"^index_(?:requests|vectors)/{_DATE}/{_FOLDER}/[^/]+$"),
    # .../{sid|day}/{request id}.ext : the directory segment is the session, the file is a request id
    ("session_report", rf"^session_report(?:s|_requests|_results)/{_FOLDER}/{_DATE}/(?P<sidseg>[^/]+)/(?P<rid>[^/]+)$"),
    ("session_aux",  rf"^session_(?:rolling|brief)/{_FOLDER}/{_DATE}/(?P<sidseg>[^/]+)/.+$"),
    ("redaction",    rf"^redactions/{_FOLDER}/{_DATE}/[^/]+$"),
    ("voice_ask",    rf"^voice_ask/{_FOLDER}/{_DATE}/[^/]+$"),
    ("trace",        rf"^traces/{_DATE}/{_FOLDER}/[^/]+$"),
    ("keyframe_req", rf"^keyframe_requests/{_FOLDER}/{_DATE}/[^/]+$"),
    ("report_request", rf"^report_requests/{_FOLDER}/(?P<rid>[^/]+)$"),
    ("photo_selection", rf"^report_photo_selection/{_FOLDER}/{_DATE}\.json$"),
]
_SESSION = [  # folder only in the payload; the session id is the only handle
    ("session_marker", r"^(?:extraction_requests|extraction_pending|extraction_empty|"
                       r"session_finalize_requests|session_finalize_results)/(?P<base>[^/]+)\.json$"),
]
_SITE_OR_COMPANY = [
    ("voice",       rf"^voice/(?P<company_id>{_UUID})/(?P<site_id>{_UUID})/[^/]+$"),
    ("voiceprint",  rf"^voiceprint_requests/(?P<company_id>{_UUID})/(?P<base>[^/]+)/[^/]+$"),
    ("match",       rf"^match_requests/(?P<site_id>{_UUID})/{_DATE}/[^/]+$"),
    # weather/ and programmes/ carry a site uuid, or the site slug as a fallback
    ("weather",     rf"^weather/(?P<site_ref>[^/]+)/{_DATE}/[^/]+$"),
    ("programme",   r"^programmes/(?P<site_ref>[^/]+)/[^/]+$"),
]
_LAKE_WIDE = [rf"^reports/{_DATE}/[^/]+_report\.[a-z]+$", rf"^reports/{_DATE}/summary_report[^/]*$"]
_MEETING_MINUTES = re.compile(r"^meeting_minutes/.+$")
# A company's own daily summary: the company is carried by the key (Task 10).
_COMPANY_SUMMARY = re.compile(
    rf"^reports/{_DATE}/companies/(?P<company_id>{_UUID})/summary_report\.[a-z]+$")
_SITE_REPORT = re.compile(rf"^reports/{_DATE}/sites/(?P<site_slug>[^/]+)/.+$")
_GLOBAL = re.compile(r"^(?:config|models|app-releases|scripts|pending_downloads|download_claims|other|"
                     r"org-assets|notices|lake_manifest|exports|export_requests)/")

_TENANT_RE = [(k, re.compile(p)) for k, p in _TENANT]
_SESSION_RE = [(k, re.compile(p)) for k, p in _SESSION]
_SITE_RE = [(k, re.compile(p)) for k, p in _SITE_OR_COMPANY]
_LAKE_WIDE_RE = [re.compile(p) for p in _LAKE_WIDE]


def _result(scope, kind, **kw):
    out = {"scope": scope, "kind": kind, "folder": None, "day": None, "session_base": None,
           "sid": None, "site_id": None, "site_slug": None, "company_id": None, "group_id": None,
           "request_id": None}
    out.update(kw)
    return out


_SID_DIR_RE = re.compile(r"^(?:sid)?(?P<sid>[0-9a-f]{32})$")


def _sid_from_name(name):
    """`sid{32 hex}` inside a file name. The literal `sid` prefix makes it unambiguous: request
    ids are uuid4 hex and can never contain it."""
    m = SID_RE.search(name)
    return m.group("sid") if m else None


def _sid_from_dir(seg):
    """The directory segment of a session_* key: `{sid}`, `sid{sid}` or the word `day`."""
    m = _SID_DIR_RE.match(seg)
    return m.group("sid") if m else None


def _stem(name):
    return name.rsplit(".", 1)[0] if "." in name else name


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
    m = _COMPANY_SUMMARY.match(key)
    if m:
        return _result("tenant", "company_summary", day=m.group("day"),
                       company_id=m.group("company_id"))
    m = _SITE_REPORT.match(key)
    if m:
        return _result("tenant", "site_report", day=m.group("day"), site_slug=m.group("site_slug"))
    last = key.rsplit("/", 1)[-1]
    for kind, rx in _TENANT_RE:
        m = rx.match(key)
        if m:
            g = m.groupdict()
            extra = {}
            if kind in ("extraction", "transcript"):
                stem = _stem(last)
                if kind == "extraction" and stem.startswith("grp"):
                    extra["group_id"] = stem[3:]  # merged group extraction, not one session
                else:
                    extra["session_base"] = stem
            if "sidseg" in g:  # sid comes from the directory segment only, never the file name
                sid = _sid_from_dir(g["sidseg"])
            else:
                sid = _sid_from_name(last)
            if g.get("rid") is not None:  # the file name of a request/result is a request id
                extra["request_id"] = _stem(g["rid"])
            return _result("tenant", kind, folder=g.get("folder"), day=g.get("day"),
                           sid=sid, **extra)
    for kind, rx in _SESSION_RE:
        m = rx.match(key)
        if m:
            base = m.group("base")
            if base.startswith("group-"):
                return _result("tenant", kind, group_id=base[len("group-"):])
            sm = _SID_SEGMENT_RE.match(base)
            if sm:  # session_finalize_requests/{sid}-updated: the session is {sid}
                return _result("tenant", kind, session_base=sm.group("sid"), sid=sm.group("sid"))
            return _result("tenant", kind, session_base=base, sid=_sid_from_name(base))
    for kind, rx in _SITE_RE:
        m = rx.match(key)
        if m:
            g = m.groupdict()
            site_id = g.get("site_id")
            site_slug = None
            if "site_ref" in g:
                if _UUID_RE.match(g["site_ref"]):
                    site_id = g["site_ref"]
                else:
                    site_slug = g["site_ref"]
            sid = None
            if kind == "voiceprint":  # the session this request is about (the key's company is not its owner)
                base = g.get("base") or ""
                sid = _sid_from_name(base) or _sid_from_dir(base)
            return _result("tenant", kind, day=g.get("day"), site_id=site_id, site_slug=site_slug,
                           company_id=g.get("company_id"), session_base=g.get("base"), sid=sid)
    return None
