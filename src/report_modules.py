"""Report section modules: reviewed section text a template picks by name.

Owner, 2026-09-30: a customer builds a report by picking modules -- "I need
Safety, Quality, Actions, as a table" -- and ordering them. The words each
module puts into the prompt are OURS: written once, reviewed, versioned. We do
not rewrite a prompt per customer. The same module gives the same instruction
to every company on every day, which is the consistency the owner asked for.

TWO LAYERS, company first. STANDARD below is the version every company gets.
A company can have its own version of a module (report_modules rows with its
company_id), written by us when we tailor a report for them; it applies to
that company only. `resolve` returns the company's latest version of a key,
else the standard one.

A TEMPLATE PINS A VERSION. A section built from a module stores
`module: {key, hash}` beside its purpose. `pin_modules`, run on every save,
looks the pair up and REPLACES the purpose with the stored text -- so the text
that reaches the prompt is always one we published, whatever the client sent.
A newer module version never changes a saved template; the editor offers it,
and a person accepts it (owner, 2026-09-30).

`hash` is sha256(key + "\\n" + purpose), 16 hex -- content, not a counter, so
two texts can never share a version and a revert is recognised as the old
version it is.

WHAT THE CUSTOMER STILL SAYS: one `note` per section (<= MAX_NOTE_CHARS),
rendered under the module text inside the customer region -- "write NO DATA
TODAY when empty", "group by subcontractor", "only variations over $5k".
"""
import hashlib
import json

MAX_NOTE_CHARS = 200

# The standard catalogue. Changing a purpose here publishes a new version: old
# templates keep the text they pinned (it is in report_modules from the first
# time it was synced) and are offered the new one.
STANDARD = [
    {"key": "summary", "title": "Daily Summary", "kind": "narrative",
     "purpose": ("The day in two to four sentences, most important first: the work that "
                 "moved forward, then anything that changes tomorrow. If any construction "
                 "progress was recorded, say what it was here, even if it is short. Do not "
                 "list detail that belongs to a more specific section.")},
    {"key": "work_done", "title": "Work Completed", "kind": "list",
     "purpose": ("Work that was finished or clearly progressed today, one item per line, "
                 "with where it happened and who did it when that was said. Only work "
                 "that happened; plans and intentions are not completed work.")},
    {"key": "work_not_done", "title": "Work Not Completed", "kind": "list",
     "purpose": ("Work that was planned or expected today and did not happen or did not "
                 "finish, one item per line, each with the reason given. Work nobody "
                 "expected today does not belong here.")},
    {"key": "safety", "title": "Safety", "kind": "list",
     "purpose": ("Hazards raised, incidents, near misses, stop-works, toolbox talks and "
                 "the controls agreed, one item per line, with who raised it and where. "
                 "Quality defects are not safety items.")},
    {"key": "quality", "title": "Quality", "kind": "list",
     "purpose": ("Inspections, hold points, defects raised or closed, non-conformances and "
                 "anything awaiting sign-off, one item per line, with the location and "
                 "who is responsible. Safety matters are not quality items.")},
    {"key": "decisions", "title": "Decisions", "kind": "list",
     "purpose": ("Decisions actually made, one per line: what was decided, who decided "
                 "it, and what it changes in programme, cost or scope. Suggestions that "
                 "were not agreed are not decisions.")},
    {"key": "actions", "title": "Actions", "kind": "table", "columns": ["Action", "Owner", "Due"],
     "purpose": ("The actions already on record for this recording, as they are listed "
                 "for you, with their owner and date exactly as recorded.")},
    {"key": "open_items", "title": "Open Discussion", "kind": "list",
     "purpose": ("Matters raised and not resolved: questions waiting for an answer, points "
                 "left open, figures or dates marked as to be confirmed, one per line, "
                 "with who is expected to answer when that was said. Settled matters do "
                 "not belong here.")},
    {"key": "programme", "title": "Programme & Delays", "kind": "list",
     "purpose": ("Delays and accelerations against the programme, one per line, each with "
                 "its cause and the activity affected. Weather on its own is not a "
                 "programme item unless a delay was said to follow from it.")},
    {"key": "workforce", "title": "Workforce", "kind": "kpi",
     "purpose": ("Numbers on site by trade or subcontractor, only where a number was "
                 "actually said. Never estimate a headcount.")},
    {"key": "deliveries_plant", "title": "Deliveries & Plant", "kind": "list",
     "purpose": ("Deliveries that arrived or were missed, plant brought on or taken off "
                 "site, and breakdowns, one per line, with what and where.")},
    {"key": "commercial", "title": "Commercial", "kind": "list",
     "purpose": ("Variations, site instructions, dayworks, and cost or claim matters that "
                 "were mentioned, one per line, with any figure exactly as said.")},
    {"key": "visitors", "title": "Visitors & Inspections", "kind": "list",
     "purpose": ("Visits by consultants, the client, council or engineers, one per line: "
                 "who came, and what they looked at or asked for. Internal staff are not "
                 "visitors.")},
    {"key": "look_ahead", "title": "Look-ahead", "kind": "list",
     "purpose": ("What was said to be happening next, one item per line, with the day "
                 "when it was given.")},
]

# Filled by code in the report rather than written by the model. Not offered as
# pickable modules until the renderer fills them (owner, 2026-09-29: the code
# decides, the model words) -- a Weather module today would have the model write
# the weather, which is exactly what weather_advice took away from it.
CODE_FILLED_LATER = ("weather", "header", "weather_log")


def module_hash(key, purpose):
    return hashlib.sha256(("%s\n%s" % (key, purpose)).encode("utf-8")).hexdigest()[:16]


def _standard(key):
    for m in STANDARD:
        if m["key"] == key:
            return m
    return None


def _payload(m, source, company_id=None):
    out = {"key": m["key"], "title": m["title"], "kind": m["kind"],
           "purpose": m["purpose"], "hash": module_hash(m["key"], m["purpose"]),
           "source": source}
    if m.get("columns"):
        out["columns"] = list(m["columns"])
    if company_id:
        out["company_id"] = str(company_id)
    return out


# ---- the table ---------------------------------------------------------------

def sync_standard(conn):
    """Record the current standard versions, so a template that pins one can be
    checked against it after the text here has moved on. Idempotent."""
    for m in STANDARD:
        conn.execute(
            "INSERT INTO report_modules (company_id, key, hash, title, kind, columns, purpose) "
            "VALUES (NULL, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (m["key"], module_hash(m["key"], m["purpose"]), m["title"], m["kind"],
             json.dumps(m.get("columns")) if m.get("columns") else None, m["purpose"]))


def _company_latest(conn, company_id):
    rows = conn.execute(
        "SELECT DISTINCT ON (key) key, hash, title, kind, columns, purpose "
        "FROM report_modules WHERE company_id = %s ORDER BY key, created_at DESC",
        (str(company_id),)).fetchall()
    out = {}
    for key, h, title, kind, columns, purpose in rows:
        if isinstance(columns, str):
            columns = json.loads(columns)
        out[key] = {"key": key, "title": title, "kind": kind, "purpose": purpose,
                    "columns": columns}
    return out


def resolve_all(conn, company_id):
    """Every module this company can pick, company version first."""
    own = _company_latest(conn, company_id)
    out = []
    for m in STANDARD:
        if m["key"] in own:
            out.append(_payload(own[m["key"]], "company", company_id))
        else:
            out.append(_payload(m, "standard"))
    for key, m in own.items():
        if not _standard(key):
            out.append(_payload(m, "company", company_id))
    return out


def find_version(conn, company_id, key, h):
    """The stored text of (key, hash) for this company or the standard, or None.
    Another company's version is never found: its text is theirs."""
    row = conn.execute(
        "SELECT purpose FROM report_modules WHERE key = %s AND hash = %s "
        "AND (company_id IS NULL OR company_id = %s) LIMIT 1",
        (key, h, str(company_id))).fetchone()
    return row[0] if row else None


def add_company_version(conn, company_id, key, title, kind, purpose, columns=None,
                        created_by=None):
    """Publish a company's own version of a module (ours to write). Re-publishing
    an earlier text makes it current again."""
    h = module_hash(key, purpose)
    conn.execute(
        "INSERT INTO report_modules (company_id, key, hash, title, kind, columns, purpose, created_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (coalesce(company_id, '00000000-0000-0000-0000-000000000000'::uuid), key, hash) "
        "DO UPDATE SET created_at = now()",
        (str(company_id), key, h, title, kind, json.dumps(columns) if columns else None,
         purpose, str(created_by) if created_by else None))
    return h


# ---- the save-time check -----------------------------------------------------

def _sections_of(body):
    for s in (body.get("sections") or []):
        yield s
        for c in (s.get("children") or []):
            yield c
    if isinstance(body.get("catch_all"), dict):
        yield body["catch_all"]


def pin_modules(conn, company_id, body, lookup=None):
    """(body, error). Every section that names a module gets that version's text
    as its purpose; an unknown key or version is refused rather than guessed.
    A note is kept, capped. Sections without a module are left alone."""
    lookup = lookup or (lambda key, h: find_version(conn, company_id, key, h))
    if conn is not None:
        sync_standard(conn)
    for s in _sections_of(body or {}):
        note = s.get("note")
        if note is not None:
            if not isinstance(note, str):
                return None, "a section note must be text"
            note = note.strip()
            if len(note) > MAX_NOTE_CHARS:
                return None, ("the note on '%s' is longer than %d characters"
                              % (s.get("title") or "a section", MAX_NOTE_CHARS))
            if note:
                s["note"] = note
            else:
                s.pop("note", None)
        mod = s.get("module")
        if mod is None:
            continue
        if not isinstance(mod, dict) or not isinstance(mod.get("key"), str) \
                or not isinstance(mod.get("hash"), str):
            return None, "a module section needs module.key and module.hash"
        text = lookup(mod["key"], mod["hash"])
        if text is None:
            return None, ("'%s' names a module version this company does not have "
                          "(%s %s)" % (s.get("title") or mod["key"], mod["key"], mod["hash"]))
        s["module"] = {"key": mod["key"], "hash": mod["hash"]}
        s["purpose"] = text
    return body, None
