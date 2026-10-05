"""Operator task: copy legacy DynamoDB action tick-offs into action_items.status.

The legacy overlay (`fieldsight-audit`, PK `ACTIONS#{date}`) recorded a tick
POSITIONALLY: SK `[USER#{folder}#]TOPIC#{topic}#ACTION#{index}`, where the index
is a position in an extraction that has since been re-run. Position therefore
means nothing today; the only thing that identifies the task is its TEXT
(`action_text`), scoped to (date, recorder folder). A tick is copied only when
exactly one action item of that recorder and day has the same normalised text --
anything else is reported and left alone. Dry run unless apply is True.

Each applied tick writes the status change and a content_edits row (actor NULL,
role `legacy_tick_backfill`) stamped with the legacy `checked_at`, NOT now():
count_action_closures_by_day buckets closures by content_edits.created_at, and
a burst stamped with the run day would show up as closures made that day.
"""
import re
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from repositories import action_items, content_edits

ACTOR_ROLE = "legacy_tick_backfill"

_SK_RE = re.compile(r"^(?:USER#(?P<folder>.+?)#)?TOPIC#(?P<topic>-?\d+)#ACTION#(?P<action>.+)$")
_INT_RE = re.compile(r"^\d+$")


def _norm(s):
    return " ".join(str(s or "").lower().split())


def classify(row):
    pk, sk = str(row.get("PK") or ""), str(row.get("SK") or "")
    date = pk.split("#", 1)[1] if pk.startswith("ACTIONS#") else None
    m = _SK_RE.match(sk)
    sk_folder = (m.group("folder") if m else None) or None
    attr_folder = row.get("user_folder") or None
    folder = sk_folder or attr_folder
    conflict = bool(sk_folder and attr_folder and sk_folder != attr_folder)
    topic = int(m.group("topic")) if m else None
    action = m.group("action") if m else ""
    plain = bool(m) and topic >= 0 and bool(_INT_RE.match(action))
    if not plain:
        kind = "finding"
    elif folder is None:
        kind = "no_folder"
    else:
        kind = "action"
    text = row.get("action_text")
    return {"date": date, "folder": folder, "topic": topic, "action": action, "kind": kind,
            "folder_conflict": conflict,
            "text": text if text is not None else row.get("text"),
            "checked": row.get("checked") is True,
            "checked_at": row.get("checked_at"), "checked_by": row.get("checked_by")}


def _parse_ts(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# Candidates: the recorder's action items on that report date. Topics removed
# by a recording deletion are excluded the same way topics.list_site_topics
# does (an active 'deleted' redaction), so a tick can never land on an item the
# UI no longer renders.
_CANDIDATES_SQL = (
    "SELECT ai.id, ai.text, ai.status, s.company_id, "
    "EXISTS (SELECT 1 FROM content_edits ce WHERE ce.table_name = 'action_items' "
    "AND ce.row_id = ai.id AND ce.field = 'status') AS has_status_edit "
    "FROM action_items ai JOIN sites s ON s.id = ai.site_id "
    "JOIN topics t ON t.id = ai.topic_id "
    "JOIN users u ON u.id = t.user_id "
    "WHERE t.report_date = %(date)s AND u.folder_name = %(folder)s "
    "AND NOT EXISTS (SELECT 1 FROM redactions r WHERE r.target_type = 'topic' "
    "AND r.target_id = t.id AND r.scope = 'deleted' AND r.reverted_at IS NULL)"
)


def _candidates(conn, date, folder):
    return conn.cursor(row_factory=dict_row).execute(
        _CANDIDATES_SQL, {"date": date, "folder": folder}).fetchall()


def run(conn, rows, apply):
    """In a dry run `applied` lists what WOULD be applied; `apply` in the report says which.

    A tick is applied ONLY to an item that is still 'open' AND has no status row in
    content_edits: any status decision recorded in Aurora (a re-open, an in_progress,
    a block) outranks a stale legacy tick, so those items go to `superseded`."""
    apply = apply is True
    report = {"apply": bool(apply), "applied": [], "already_done": [], "unmatched": [],
              "ambiguous": [], "findings": [], "no_folder": [], "no_timestamp": [],
              "folder_conflict": [], "superseded": [],
              "failed": [], "unchecked": 0}
    seen = set()
    for raw in rows:
        c = classify(raw)
        entry = {"date": c["date"], "folder": c["folder"], "text": c["text"]}
        if not c["checked"]:
            report["unchecked"] += 1
            continue
        if c["kind"] == "finding":
            report["findings"].append(entry)
            continue
        if c["kind"] == "action" and c["folder_conflict"]:
            report["folder_conflict"].append(entry)
            continue
        if c["kind"] == "no_folder":
            report["no_folder"].append(entry)
            continue
        entry.update(checked_by=c["checked_by"], checked_at=c["checked_at"])
        ts = _parse_ts(c["checked_at"])
        if ts is None:
            report["no_timestamp"].append(entry)
            continue
        want = _norm(c["text"])
        hits = ([r for r in _candidates(conn, c["date"], c["folder"]) if _norm(r["text"]) == want]
                if want and c["date"] else [])
        if not hits:
            report["unmatched"].append(entry)
            continue
        if len(hits) > 1:
            report["ambiguous"].append(entry)
            continue
        item = hits[0]
        entry["action_item_id"] = str(item["id"])
        if item["status"] == "done" or item["id"] in seen:
            report["already_done"].append(entry)
            continue
        if item["status"] != "open" or item["has_status_edit"]:
            report["superseded"].append({**entry, "status": item["status"]})
            continue
        seen.add(item["id"])
        if not apply:
            report["applied"].append(entry)
            continue
        if action_items.get_action_item(conn, item["id"]) is None:
            seen.discard(item["id"])
            report["failed"].append({**entry, "error": "action item not found"})
            continue
        try:
            with conn.transaction():
                # On a SQL error the repo calls conn.rollback(), which psycopg forbids
                # inside a savepoint: the real cause is then the exception's
                # __context__, which is what gets recorded below.
                if action_items.update_action_item_fields(
                        conn, item["id"], {"status": "done"}, None) is None:
                    raise RuntimeError("update failed: the row vanished or the UPDATE errored")
                content_edits.append_content_edit(
                    conn, item["company_id"], "action_items", item["id"], "status",
                    item["status"], "done", None, ACTOR_ROLE, created_at=ts)
        except Exception as e:  # recorded, never counted as applied
            seen.discard(item["id"])
            cause = e.__context__ if isinstance(e, psycopg.ProgrammingError) and e.__context__ else e
            report["failed"].append({**entry, "error": f"{type(cause).__name__}: {cause}"})
            continue
        report["applied"].append(entry)
    return report
