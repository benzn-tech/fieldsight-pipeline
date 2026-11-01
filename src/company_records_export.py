"""Structured records export: one company's Aurora rows as JSONL, one file per table.

Spec 2026-11-01-company-offboarding §7. Written next to the file export of the same export id:

    exports/{company_id}/{export_id}/records/<name>.jsonl   one per entry of TABLES
    exports/{company_id}/{export_id}/records/README.txt     what each file is, rules, counts
    exports/{company_id}/{export_id}/records_state.json     resume state (not under records/)

Selection is a WHITELIST by project-owned tenancy (design 2026-10-09): a row belongs to the
company of the SITE it sits on; a site-less session belongs to its recorder's HOME company.
Each table's SQL selects only rows the company owns, and selects the owning company id(s)
alongside; every row is then re-checked in Python (`TableSpec.check`). A row whose owner is
not this company is dropped and counted under `foreign_company`, never written.

Deletion, the same rules the product applies (and the file export's `Tombstones`):
* topics: a `redactions` topic tombstone or a recording tombstone over its source key
  (deleted_predicates' two deletion arms) -> `deleted`; superseded by a later extraction
  pass -> `superseded` (not deleted, but never shown by the product; the live pass is
  exported instead).
* topic children (action_items, findings, topic_decisions, safety_observations): the same
  two judgements, made on their parent topic.
* action item status history (content_edits): dropped when the item's topic is deleted;
  kept for superseded items (it is history, keyed to the item's stable_id).
* sessions (meeting_session): the session id is named by an active recording tombstone.
* observations (manual): archived_at set (the product's soft delete for them).
* compliance_resolutions: the recorder folder/day carries an active recording tombstone
  (fail closed, as the file export's day-level rule: the row's content_sample is derived
  from a row that may belong to the deleted recording).
* project_documents: deleted_at set.
archived_at on companies' structure (sites, users, memberships) is a state, not a deletion:
those rows are exported with the column.

Columns: discovered from information_schema at run time (schema drift safe), minus vector /
tsvector / bytea columns and anything named like an embedding. `users` is an explicit
whitelist (USER_COLUMNS): no prefs, avatar keys or anything credential-like.

Resumable: rows are read with keyset pagination on each table's key. A table's file is an S3
multipart upload whose UploadId, parts and row cursor live in records_state.json; a part is
uploaded when the buffer reaches `part_bytes` and the state is saved right after, so a
resume continues from the last durable row (rows buffered after it are re-read). Small
tables are a single put_object. A call stops at `time_budget_s` (checked between pages) and
returns `done: False`; calling again with the same export_id continues. state is written
conditionally (If-None-Match / If-Match) and a lost race stops the call (`lost_race`).
Uploads are never aborted here (the upload id may be the other racer's); the
AbortIncompleteMultipartUpload lifecycle rule on exports/ (prod prerequisite of the file
export) collects orphans.

A table that does not exist (or whose query hits an undefined table/column) is written as an
empty file and noted in README.txt and the state; it never fails the export.
"""
import datetime as _dt
import decimal
import json
import logging
import time
import uuid
from dataclasses import dataclass, field

import psycopg
from psycopg.rows import dict_row

import company_export
from deleted_predicates import DELETED_SOURCE_PREDICATE, DELETED_TOPIC_PREDICATE

logger = logging.getLogger(__name__)

DEFAULT_TIME_BUDGET_S = company_export.DEFAULT_TIME_BUDGET_S
DEFAULT_PART_BYTES = 8 * 1024 * 1024
PAGE_SIZE = 200
STATE_NAME = "records_state.json"
RECORDS_DIR = "records/"

# users: an explicit whitelist. Excluded on purpose: prefs (opaque per-user settings),
# avatar_s3_key (internal object key), last_seen_at (telemetry). There are no password /
# token columns on users; a future one stays out because this is a whitelist.
USER_COLUMNS = ("id", "cognito_sub", "company_id", "email", "first_name", "last_name",
                "global_role", "folder_name", "kind", "created_at", "archived_at")

_DROP_UDT = {"vector", "tsvector", "bytea"}

_TOPIC_DELETED = ("NOT ({t} AND {s})".format(t=DELETED_TOPIC_PREDICATE.format(alias="x"),
                                             s=DELETED_SOURCE_PREDICATE.format(alias="x")))
_PARENT_DELETED = ("EXISTS (SELECT 1 FROM topics tt WHERE tt.id = {col} AND NOT ({t} AND {s}))"
                   .replace("{t}", DELETED_TOPIC_PREDICATE.format(alias="tt"))
                   .replace("{s}", DELETED_SOURCE_PREDICATE.format(alias="tt")))
_PARENT_SUPERSEDED = ("NOT EXISTS (SELECT 1 FROM topics tl WHERE tl.id = {col} "
                      "AND tl.superseded_at IS NULL)")
_TOPIC_SITE_CO = ("(SELECT s2.company_id FROM topics t2 JOIN sites s2 ON s2.id = t2.site_id "
                  "WHERE t2.id = {col})")
_TASK_SITE_CO = ("(SELECT s3.company_id FROM programme_tasks pt3 "
                 "JOIN programmes p3 ON p3.id = pt3.programme_id "
                 "JOIN sites s3 ON s3.id = p3.site_id WHERE pt3.id = {col})")


def _owned_by(*cols):
    """check(): every named owner column must be this company."""
    def check(row, cid):
        return None if all(str(row.get(c)) == cid for c in cols) else "foreign_company"
    return check


def _membership_check(row, cid):
    # a row on this company's site (any person, incl. an external member), or a home user's
    # own membership on another company's site (the row only; that site is not exported)
    if str(row.get("_fs_site_co")) == cid or str(row.get("_fs_user_co")) == cid:
        return None
    return "foreign_company"


def _session_check(row, cid):
    owner = row.get("_fs_site_co") if row.get("_fs_has_site") else row.get("_fs_user_co")
    return None if str(owner) == cid else "foreign_company"


@dataclass
class TableSpec:
    name: str                      # output file stem
    table: str                     # real table (alias x)
    where: str                     # ownership WHERE (params: %(cid)s)
    keys: tuple                    # ((column, sql cast), ...) keyset order
    hidden: dict = field(default_factory=dict)    # _fs_<name> -> sql expr (never written)
    check: object = None           # (row, cid) -> reason | None
    joins: str = ""
    extra: dict = field(default_factory=dict)     # extra written columns: name -> sql expr
    columns: tuple = None          # explicit whitelist (else discovered)
    flags: tuple = ()              # ((hidden bool column, reason), ...) -> excluded when true
    describe: str = ""
    owner_rule: str = ""


_SITE_JOIN = "JOIN sites s ON s.id = x.site_id"
_SITE_WHERE = "s.company_id = %(cid)s"
_PROG_JOIN = "JOIN programmes p ON p.id = x.programme_id JOIN sites s ON s.id = p.site_id"
_TASK_JOIN = ("JOIN programme_tasks pt ON pt.id = {col} JOIN programmes p ON p.id = pt.programme_id "
              "JOIN sites s ON s.id = p.site_id")
_SITE_RULE = "rows on the company's sites (project-owned: whoever recorded them)"


def _child(name, table, describe):
    return TableSpec(
        name=name, table=table, joins=_SITE_JOIN, where=_SITE_WHERE, keys=(("id", "uuid"),),
        hidden={"site_co": "s.company_id", "topic_co": _TOPIC_SITE_CO.format(col="x.topic_id"),
                "deleted": _PARENT_DELETED.replace("{col}", "x.topic_id"),
                "superseded": _PARENT_SUPERSEDED.format(col="x.topic_id")},
        check=_owned_by("_fs_site_co", "_fs_topic_co"),
        flags=(("_fs_deleted", "deleted"), ("_fs_superseded", "superseded")),
        describe=describe, owner_rule=_SITE_RULE + "; its topic must be on one too")


TABLES = [
    TableSpec("company", "companies", where="x.id = %(cid)s", keys=(("id", "uuid"),),
              hidden={"owner": "x.id"}, check=_owned_by("_fs_owner"),
              describe="the company row", owner_rule="the company itself"),
    TableSpec("sites", "sites", where="x.company_id = %(cid)s", keys=(("id", "uuid"),),
              hidden={"owner": "x.company_id"}, check=_owned_by("_fs_owner"),
              describe="the company's sites (projects), archived ones included",
              owner_rule="sites.company_id"),
    TableSpec("users", "users", where="x.company_id = %(cid)s", keys=(("id", "uuid"),),
              hidden={"owner": "x.company_id"}, check=_owned_by("_fs_owner"),
              columns=USER_COLUMNS,
              describe="the company's own people (home company), archived ones included; "
                       "columns: " + ", ".join(USER_COLUMNS),
              owner_rule="users.company_id (home company). People of other companies who were "
                         "external members on this company's sites are not included; their "
                         "user_id appears in memberships/sessions/topics"),
    TableSpec("memberships", "memberships",
              joins="JOIN sites s ON s.id = x.site_id JOIN users u ON u.id = x.user_id",
              where="s.company_id = %(cid)s OR u.company_id = %(cid)s", keys=(("id", "uuid"),),
              hidden={"site_co": "s.company_id", "user_co": "u.company_id"},
              check=_membership_check,
              describe="site memberships (role, external flag, archived_at)",
              owner_rule="every membership on the company's sites (including other companies' "
                         "external members), plus the company's own people's memberships on "
                         "other companies' sites (the row only: that site is not exported)"),
    TableSpec("sessions", "meeting_session",
              joins="LEFT JOIN sites s ON s.id = x.site_id LEFT JOIN users u ON u.id = x.user_id",
              where="(x.site_id IS NOT NULL AND s.company_id = %(cid)s) OR "
                    "(x.site_id IS NULL AND u.company_id = %(cid)s)",
              keys=(("session_id", "text"),),
              hidden={"site_co": "s.company_id", "user_co": "u.company_id",
                      "has_site": "x.site_id IS NOT NULL"},
              check=_session_check,
              describe="recording sessions (table meeting_session)",
              owner_rule="sessions on the company's sites; a session with no site belongs to "
                         "its recorder's home company"),
    TableSpec("topics", "topics", joins=_SITE_JOIN, where=_SITE_WHERE, keys=(("id", "uuid"),),
              hidden={"site_co": "s.company_id", "deleted": _TOPIC_DELETED,
                      "superseded": "x.superseded_at IS NOT NULL"},
              check=_owned_by("_fs_site_co"),
              flags=(("_fs_deleted", "deleted"), ("_fs_superseded", "superseded")),
              describe="extracted topics (summary, participants, decisions, open questions)",
              owner_rule=_SITE_RULE),
    _child("action_items", "action_items", "action items (current status on each row)"),
    TableSpec("action_item_history", "content_edits",
              joins="JOIN action_items ai ON ai.id = x.row_id JOIN sites s ON s.id = ai.site_id",
              where="x.table_name = 'action_items' AND s.company_id = %(cid)s",
              keys=(("id", "uuid"),), extra={"action_item_stable_id": "ai.stable_id"},
              hidden={"site_co": "s.company_id",
                      "deleted": _PARENT_DELETED.replace("{col}", "ai.topic_id")},
              check=_owned_by("_fs_site_co"), flags=(("_fs_deleted", "deleted"),),
              describe="action item change history (status, priority, deadline, responsible, "
                       "text edits; table content_edits); action_item_stable_id links an entry "
                       "to the item across re-extractions",
              owner_rule="history of action items on the company's sites"),
    _child("decisions", "topic_decisions", "decisions (table topic_decisions)"),
    _child("findings", "findings", "findings (safety / quality / progress)"),
    TableSpec("observations", "observations", where="x.company_id = %(cid)s",
              keys=(("id", "uuid"),),
              hidden={"owner": "x.company_id", "archived": "x.archived_at IS NOT NULL"},
              check=_owned_by("_fs_owner"), flags=(("_fs_archived", "archived"),),
              describe="manually entered safety / quality observations",
              owner_rule="observations.company_id (the site's company)"),
    _child("safety_observations", "safety_observations",
           "extracted safety observations (table safety_observations)"),
    TableSpec("compliance_resolutions", "compliance_resolutions", joins=_SITE_JOIN,
              where=_SITE_WHERE, keys=(("id", "uuid"),), hidden={"site_co": "s.company_id"},
              check=_owned_by("_fs_site_co"),
              describe="resolved / reopened state of safety and quality items",
              owner_rule=_SITE_RULE),
    TableSpec("programmes", "programmes", joins=_SITE_JOIN, where=_SITE_WHERE,
              keys=(("id", "uuid"),), hidden={"site_co": "s.company_id"},
              check=_owned_by("_fs_site_co"), describe="programmes (schedules)",
              owner_rule=_SITE_RULE),
    TableSpec("programme_versions", "programme_versions", joins=_PROG_JOIN, where=_SITE_WHERE,
              keys=(("id", "uuid"),), hidden={"site_co": "s.company_id"},
              check=_owned_by("_fs_site_co"), describe="programme import versions",
              owner_rule="programmes on the company's sites"),
    TableSpec("programme_tasks", "programme_tasks", joins=_PROG_JOIN, where=_SITE_WHERE,
              keys=(("id", "uuid"),), hidden={"site_co": "s.company_id"},
              check=_owned_by("_fs_site_co"), describe="programme tasks",
              owner_rule="programmes on the company's sites"),
    TableSpec("programme_task_deps", "programme_task_deps",
              joins=_TASK_JOIN.format(col="x.predecessor_id"), where=_SITE_WHERE,
              keys=(("predecessor_id", "uuid"), ("successor_id", "uuid"), ("dep_type", "text")),
              hidden={"site_co": "s.company_id",
                      "succ_co": _TASK_SITE_CO.format(col="x.successor_id")},
              check=_owned_by("_fs_site_co", "_fs_succ_co"), describe="task dependencies",
              owner_rule="both tasks in programmes on the company's sites"),
    TableSpec("programme_task_assignees", "programme_task_assignees",
              joins=_TASK_JOIN.format(col="x.task_id"), where=_SITE_WHERE,
              keys=(("task_id", "uuid"), ("assignee", "text")),
              hidden={"site_co": "s.company_id"}, check=_owned_by("_fs_site_co"),
              describe="task assignees (folder names)",
              owner_rule="programmes on the company's sites"),
    TableSpec("programme_delay_flags", "programme_delay_flags",
              joins=_TASK_JOIN.format(col="x.task_id"), where=_SITE_WHERE,
              keys=(("id", "uuid"),), hidden={"site_co": "s.company_id"},
              check=_owned_by("_fs_site_co"), describe="delay flags raised on tasks",
              owner_rule="programmes on the company's sites"),
    TableSpec("project_documents", "project_documents", joins=_SITE_JOIN, where=_SITE_WHERE,
              keys=(("id", "uuid"),),
              hidden={"site_co": "s.company_id", "deleted": "x.deleted_at IS NOT NULL"},
              check=_owned_by("_fs_site_co"), flags=(("_fs_deleted", "deleted"),),
              describe="project document index (the documents themselves are in the file "
                       "export by source_key)",
              owner_rule=_SITE_RULE),
]

# Every other table the migrations create, named in README.txt under "Not included".
# tests/unit/test_company_records_export_not_included.py pins this list against
# src/migrations: a table is either in TABLES or here.
NOT_EXPORTED = (
    # Company work product that is not exported as records
    ("recordings", "recording metadata (the recorded media itself is in the file export)"),
    ("topic_photos", "photos linked to topics"),
    ("voice_note_photo", "photos linked to voice notes"),
    ("topic_questions", "open-question rows (topics.open_questions carries them)"),
    ("topic_threads", "recurring-item threads across topics"),
    ("topic_thread_suggestions", "suggested links of topics to recurring-item threads"),
    ("action_status_suggestions", "suggested action item status changes awaiting review"),
    ("action_task_numbers", "the per-site task numbers (#N) shown for action items"),
    ("action_packs", "action agent runs (instruction, reading, conclusion, run state)"),
    ("action_pack_sources", "the records, documents and web pages an action agent run cited"),
    ("action_pack_actions", "actions an action agent run proposed, and whether they were "
                            "applied"),
    ("programme_progress_suggestions", "suggested programme task progress awaiting review"),
    ("checklist_reports", "inspection / checklist reports made from a spoken check (status "
                          "and the result file key)"),
    ("inspection_windows", "spoken inspection checks (start / end times per recording)"),
    ("report_templates", "the company's report templates"),
    ("report_template_versions", "versions of the company's report templates"),
    ("report_template_bindings", "which report template is used where"),
    ("report_modules", "the company's report section modules"),
    ("report_chunks", "report / search text chunks derived from transcripts"),
    ("report_chunks_archive", "archived report / search text chunks"),
    ("project_doc_chunks", "search chunks of project documents (the documents are in the "
                           "file export)"),
    ("day_location_markers", "location markers recorded during a day"),
    ("day_recording_segments", "per-day recording segment index"),
    ("site_attendance", "who was on which site on which day"),
    ("session_group", "meeting groups (sessions merged into one meeting)"),
    ("voice_messages", "Site Voice messages"),
    ("voice_ask_log", "Ask questions and answers log"),
    ("name_aliases", "spelling aliases for names"),
    ("classification_feedback", "feedback on topic classification"),
    ("decision_records", "model decision records"),
    ("keyframe_events", "video keyframe events"),
    ("keyframe_tombstones", "deleted video keyframes"),
    ("redactions", "deletion / redaction records (deleted content is excluded above)"),
    # Speaker identification
    ("speaker_voiceprints", "voiceprints (biometric)"),
    ("speaker_voiceprint_samples", "voiceprint samples (biometric)"),
    ("speaker_voiceprint_company_floors", "voiceprint matching thresholds"),
    ("speaker_turn_names", "names given to speaker turns"),
    ("speaker_label_groups", "speaker label groupings"),
    ("speaker_name_proposals", "proposed speaker names"),
    ("speaker_name_rejections", "rejected speaker names"),
    ("speaker_intro_suggestions", "suggested speaker introductions"),
    # Audit, operational and system state
    ("membership_audit", "membership change audit"),
    ("company_status_events", "company status change audit (suspend / exit / reactivate)"),
    ("company_status_cognito", "sign-in disable / enable work items"),
    ("lake_objects", "storage object ownership index (the objects are in the file export)"),
    ("devices", "recording devices"),
    ("device_upload_freezes", "device upload freezes"),
    ("ws_connections", "live websocket connections"),
)

# compliance_resolutions' day tombstone is applied in Python from the Tombstones set
_DAY_TOMBSTONED = {"compliance_resolutions"}
_SESSION_TOMBSTONED = {"sessions"}


class _LostRace(Exception):
    pass


class _UploadGone(Exception):
    pass


def _q(name):
    return '"' + str(name).replace('"', '""') + '"'


def _json_default(v):
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, _dt.timedelta):
        return v.total_seconds()
    if isinstance(v, (set, frozenset, tuple)):
        return list(v)
    return str(v)


def _line(row) -> bytes:
    return (json.dumps(row, default=_json_default, ensure_ascii=False) + "\n").encode("utf-8")


def _table_exists(conn, table) -> bool:
    return conn.execute("SELECT to_regclass(%s) IS NOT NULL", (table,)).fetchone()[0]


def _columns(conn, spec):
    rows = conn.execute(
        "SELECT column_name, udt_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s ORDER BY ordinal_position",
        (spec.table,)).fetchall()
    present = [c for c, udt in rows
               if udt not in _DROP_UDT and "embedding" not in c.lower()]
    if spec.columns is not None:
        return [c for c in spec.columns if c in present]
    return present


def _page_sql(spec, cols, with_cursor):
    sel = [f"x.{_q(c)}" for c in cols]
    sel += [f"{expr} AS {_q(n)}" for n, expr in spec.extra.items()]
    sel += [f"{expr} AS {_q('_fs_' + n)}" for n, expr in spec.hidden.items()]
    key_cols = ", ".join(f"x.{_q(k)}" for k, _ in spec.keys)
    where = f"({spec.where})"
    if with_cursor:
        marks = ", ".join(f"%(k{i})s::{cast}" for i, (_, cast) in enumerate(spec.keys))
        where += f" AND ({key_cols}) > ({marks})"
    return (f"SELECT {', '.join(sel)} FROM {spec.table} x {spec.joins} WHERE {where} "
            f"ORDER BY {key_cols} LIMIT %(lim)s")


def _key_of(spec, row):
    return [str(row[k]) for k, _ in spec.keys]


def records_prefix(company_id, export_id):
    return company_export.export_prefix(company_id, export_id) + RECORDS_DIR


def run(conn, s3, company_id, *, export_id, dest_bucket, time_budget_s=DEFAULT_TIME_BUDGET_S,
        part_bytes=DEFAULT_PART_BYTES, page_size=PAGE_SIZE, now=None, clock=time.monotonic,
        tombstones=None) -> dict:
    """Export (part of) one company's records. See the module docstring.

    Returns {company_id, export_id, done, tables: {name: {done, rows, excluded, note?}},
    files: [keys] (when done), lost_race?}."""
    company_id = company_export._canon_uuid(company_id, "company_id")
    export_id = company_export._canon_uuid(export_id, "export_id")
    now = now or _dt.datetime.now(_dt.timezone.utc)
    base = company_export.export_prefix(company_id, export_id)
    rprefix = base + RECORDS_DIR
    state_key = base + STATE_NAME

    state, etag = company_export._read_state(s3, dest_bucket, state_key)
    existed = state is not None
    if state is None:
        state = {"export_id": export_id, "company_id": company_id, "done": False,
                 "generated_at": now.isoformat(), "tables": {}, "files": []}
    elif state.get("export_id") != export_id or state.get("company_id") != company_id:
        raise ValueError("saved records state belongs to a different export")
    box = {"etag": etag, "existed": existed}

    def result(done, **extra):
        tables = {n: {"done": bool(t.get("done")), "rows": t.get("rows", 0),
                      "excluded": dict(t.get("excluded") or {}),
                      **({"note": t["note"]} if t.get("note") else {})}
                  for n, t in state["tables"].items()}
        out = {"company_id": company_id, "export_id": export_id, "done": done,
               "tables": tables, "files": list(state.get("files") or []) if done else []}
        out.update(extra)
        return out

    def save():
        kw = ({"IfMatch": box["etag"]} if box["etag"] else {} if box["existed"]
              else {"IfNoneMatch": "*"})
        try:
            resp = s3.put_object(Bucket=dest_bucket, Key=state_key,
                                 Body=json.dumps(state, default=str).encode("utf-8"),
                                 ContentType="application/json", **kw)
        except Exception as exc:  # noqa: BLE001
            code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
            if code in company_export._PRECONDITION_CODES:
                raise _LostRace() from None
            raise
        box["etag"] = (resp or {}).get("ETag")
        box["existed"] = True

    if state.get("done"):
        return result(True)

    tombs = tombstones if tombstones is not None else company_export.Tombstones.load(conn)
    started = clock()
    pages_read = [0]

    def out_of_time():
        return pages_read[0] > 0 and clock() - started >= time_budget_s

    try:
        for spec in TABLES:
            ts = state["tables"].setdefault(spec.name, {"done": False})
            if ts.get("done"):
                continue
            if out_of_time():
                return result(False)
            while True:
                try:
                    finished = _export_table(conn, s3, dest_bucket, rprefix, spec, ts, company_id,
                                             tombs, part_bytes, page_size, out_of_time,
                                             pages_read, save)
                except _UploadGone:
                    logger.warning("records export %s: upload of %s is gone; restarting the table",
                                   export_id, spec.name)
                    ts.clear()
                    ts["done"] = False
                    continue
                break
            if not finished:
                return result(False)
        readme_key = rprefix + "README.txt"
        s3.put_object(Bucket=dest_bucket, Key=readme_key,
                      Body=_readme(conn, company_id, export_id, state, now).encode("utf-8"),
                      ContentType="text/plain; charset=utf-8")
        state["files"] = [rprefix + f"{s.name}.jsonl" for s in TABLES] + [readme_key]
        state["done"] = True
        save()
        return result(True)
    except _LostRace:
        logger.warning("records export %s: lost the records_state race; stopping", export_id)
        return result(False, lost_race=True)


def _export_table(conn, s3, bucket, rprefix, spec, ts, cid, tombs, part_bytes, page_size,
                  out_of_time, pages_read, save) -> bool:
    """Advance one table. True when its file is complete (state saved), False when the time
    budget ran out (durable progress saved at the last uploaded part)."""
    key = rprefix + f"{spec.name}.jsonl"
    excluded = dict(ts.get("excluded") or {})          # durable (up to ts["cursor"])
    rows = ts.get("rows", 0)
    cursor = ts.get("cursor")
    buf = bytearray()
    pend_rows = 0
    pend_excl = {}
    last_key = cursor

    def finish_empty(note):
        s3.put_object(Bucket=bucket, Key=key, Body=b"", ContentType="application/x-ndjson")
        ts.clear()
        ts.update({"done": True, "rows": 0, "excluded": {}, "note": note})
        save()
        return True

    if not _table_exists(conn, spec.table):
        return finish_empty("absent: table does not exist in this database")

    def flush_part():
        nonlocal buf, pend_rows, pend_excl, rows, excluded
        if not ts.get("upload_id"):
            ts["upload_id"] = s3.create_multipart_upload(
                Bucket=bucket, Key=key, ContentType="application/x-ndjson")["UploadId"]
            ts["parts"] = []
        n = len(ts["parts"]) + 1
        try:
            resp = s3.upload_part(Bucket=bucket, Key=key, UploadId=ts["upload_id"],
                                  PartNumber=n, Body=bytes(buf))
        except Exception as exc:  # noqa: BLE001
            if str(getattr(exc, "response", {}).get("Error", {}).get("Code", "")) == "NoSuchUpload":
                raise _UploadGone() from None
            raise
        ts["parts"].append({"ETag": resp["ETag"], "PartNumber": n})
        rows += pend_rows
        for r, c in pend_excl.items():
            excluded[r] = excluded.get(r, 0) + c
        ts.update({"rows": rows, "excluded": dict(excluded), "cursor": last_key})
        buf = bytearray()
        pend_rows = 0
        pend_excl = {}
        save()

    try:
        with conn.transaction():
            cols = _columns(conn, spec)
            cur = conn.cursor(row_factory=dict_row)
            while True:
                if out_of_time():
                    return False
                params = {"cid": cid, "lim": page_size}
                if last_key is not None:
                    params.update({f"k{i}": v for i, v in enumerate(last_key)})
                page = cur.execute(_page_sql(spec, cols, last_key is not None), params).fetchall()
                pages_read[0] += 1
                for row in page:
                    last_key = _key_of(spec, row)
                    reason = spec.check(row, cid) if spec.check else None
                    if reason is None:
                        for flag, why in spec.flags:
                            if row.get(flag):
                                reason = why
                                break
                    if reason is None and spec.name in _SESSION_TOMBSTONED:
                        if str(row.get("session_id", "")).lower() in tombs.sids:
                            reason = "deleted"
                    if reason is None and spec.name in _DAY_TOMBSTONED:
                        day = row.get("report_date")
                        day = day.isoformat() if hasattr(day, "isoformat") else str(day)
                        if (str(row.get("user_folder", "")).casefold(), day) in tombs.by_day:
                            reason = "deleted_day"
                    if reason is not None:
                        pend_excl[reason] = pend_excl.get(reason, 0) + 1
                    else:
                        out = {k: v for k, v in row.items() if not k.startswith("_fs_")}
                        buf += _line(out)
                        pend_rows += 1
                    if len(buf) >= part_bytes:
                        flush_part()
                if len(page) < page_size:
                    break
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn) as exc:
        logger.warning("records export: %s unavailable: %s", spec.name, exc)
        return finish_empty(f"unavailable: {type(exc).__name__}")

    # the table is complete: the tail, then the object
    rows_total = rows + pend_rows
    for r, c in pend_excl.items():
        excluded[r] = excluded.get(r, 0) + c
    if not ts.get("upload_id"):
        s3.put_object(Bucket=bucket, Key=key, Body=bytes(buf), ContentType="application/x-ndjson")
    else:
        parts = list(ts["parts"])
        try:
            if buf:
                n = len(parts) + 1
                resp = s3.upload_part(Bucket=bucket, Key=key, UploadId=ts["upload_id"],
                                      PartNumber=n, Body=bytes(buf))
                parts.append({"ETag": resp["ETag"], "PartNumber": n})
            s3.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=ts["upload_id"],
                                         MultipartUpload={"Parts": parts})
        except Exception as exc:  # noqa: BLE001
            if str(getattr(exc, "response", {}).get("Error", {}).get("Code", "")) == "NoSuchUpload":
                raise _UploadGone() from None
            raise
    ts.clear()
    ts.update({"done": True, "rows": rows_total, "excluded": excluded})
    save()
    return True


def _readme(conn, company_id, export_id, state, now):
    name = None
    try:
        with conn.transaction():
            row = conn.execute("SELECT name FROM companies WHERE id = %s", (company_id,)).fetchone()
            name = row[0] if row else None
    except psycopg.Error:
        name = None
    lines = [
        "FieldSight structured records export",
        "====================================",
        f"Company: {name or '(unknown)'} ({company_id})",
        f"Export id: {export_id}",
        f"Generated at (UTC): {now.isoformat()}",
        "",
        "Format: one JSON object per line (JSON Lines), UTF-8. Ids are UUID strings; times are",
        "ISO 8601. The recorded media and documents are in the file export (part-*.zip) of the",
        "same export id; manifest.json lists them.",
        "",
        "Ownership rule (project-owned tenancy): a record belongs to the company of the SITE it",
        "was captured on, whoever recorded it. A session with no site belongs to its recorder's",
        "home company. Only this company's records are included; each row was re-checked",
        "after selection and any row owned by another company was dropped (counted below as",
        "foreign_company).",
        "",
        "Excluded on purpose:",
        "- Secrets and credentials: none are exported. users.jsonl carries only: "
        + ", ".join(USER_COLUMNS) + ".",
        "- Search indexes and embeddings (vector columns).",
        "- Deleted content: topics deleted by a user or whose recording was deleted, and their",
        "  action items, findings, decisions and observations; deleted sessions; archived",
        "  manual observations; deleted project documents; compliance resolutions of a",
        "  recorder/day with a deleted recording.",
        "- Superseded extraction passes (an older extraction of a recording that was",
        "  re-extracted; the current pass is included). Action item history keeps them.",
        "- People of other companies who were external members on this company's sites: their",
        "  memberships and their work on these sites are included, their user record is not.",
        "",
        "Files:",
    ]
    for spec in TABLES:
        t = state["tables"].get(spec.name, {})
        ex = t.get("excluded") or {}
        ex_txt = ", ".join(f"{k}={v}" for k, v in sorted(ex.items())) or "none"
        lines.append(f"- {spec.name}.jsonl (table {spec.table}): {spec.describe}.")
        lines.append(f"    rows: {t.get('rows', 0)}; excluded: {ex_txt}")
        lines.append(f"    selection: {spec.owner_rule}.")
        if t.get("note"):
            lines.append(f"    NOTE: {t['note']} -- this file is empty.")
    lines += ["", "Not included (no file):"]
    lines += [f"- {table}: {why}" for table, why in NOT_EXPORTED]
    lines.append("")
    return "\n".join(lines)
