"""Pure reconciliation of an imported programme against what is already stored.

Spec: fieldsight-ui/docs/superpowers/specs/2026-08-02-programme-foundation-design.md §6.3

The join key is `source_task_id` and nothing else. Names change between
revisions; ids do not. Our own surrogate `id` never participates in matching —
it exists so allocations, progress and breakdown subtrees keep pointing at the
same row no matter what the file does (design §4.1).

Three rules the rest of the system depends on:

  1. A `local` row is never updated or removed by an import. It is ours. The
     one exception is being archived alongside an imported parent that left
     the file — and even then the row survives, flagged, because completed
     work and real progress records hang off it.
  2. `progress_pct` and `status` are never taken from the file. Progress is an
     observation made on site; the file's 0% is a planning artefact, and
     overwriting with it would erase what someone recorded.
  3. Nothing is deleted. Departures are stamped with `removed_in_version`.

Pure: no database, no S3, no clock.
"""
from datetime import date

# Fields the file owns. Everything else on an imported row is ours or
# structural. progress_pct and status are deliberately absent — see rule 2.
_IMPORT_OWNED = ("name", "wbs_code", "start_date", "end_date", "duration_days")

# Rename detection. A candidate needs the same normalised name AND a start
# within this many days of the row that disappeared. Anything looser starts
# pairing genuinely unrelated tasks, and a wrong pairing silently transplants
# one task's history — its allocations and recorded progress — onto another.
_RENAME_MAX_DAY_DRIFT = 14

# Below this share of shared source ids, the file is probably a different
# programme; Update would produce a huge add plus a huge remove that reads as
# data loss.
_REPLACE_SUGGEST_BELOW = 0.30


def _norm(s):
    return " ".join((s or "").lower().split())


def _as_date(v):
    if v is None:
        return None
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def _days_between(a, b):
    da, db = _as_date(a), _as_date(b)
    if da is None or db is None:
        return None
    return abs((da - db).days)


def _iso(v):
    if v is None:
        return None
    return v if isinstance(v, str) else v.isoformat()


def _incoming_rows(parents, leaves):
    """One flat list keyed by the file's id, groups first so a leaf can
    resolve its parent."""
    rows = []
    for p in parents or []:
        rows.append({
            "source_task_id": p["task_id"],
            "parent_source_id": None,
            "name": p.get("name") or p["task_id"],
            "wbs_code": p.get("wbs"),
            "start_date": None, "end_date": None, "duration_days": None,
        })
    for t in leaves or []:
        rows.append({
            "source_task_id": t["task_id"],
            "parent_source_id": t.get("parent_id"),
            "name": t.get("name") or t["task_id"],
            "wbs_code": t.get("wbs"),
            "start_date": _iso(t.get("start")),
            "end_date": _iso(t.get("end")),
            "duration_days": t.get("duration_days"),
        })
    return rows


def reconcile(existing, parents, leaves, *, version_no):
    """Returns {insert, update, remove, rename_candidates, summary}."""
    incoming = _incoming_rows(parents, leaves)
    incoming_by_src = {r["source_task_id"]: r for r in incoming}

    imported = [t for t in existing if t.get("origin") == "imported"]
    local = [t for t in existing if t.get("origin") == "local"]
    existing_by_src = {t["source_task_id"]: t for t in imported
                       if t.get("source_task_id")}

    updates, removals, inserts = [], [], []
    locally_modified_overwritten = []
    date_shifted = 0
    max_shift = 0

    for src, row in existing_by_src.items():
        inc = incoming_by_src.get(src)
        if inc is None:
            continue

        fields = {}
        for col in _IMPORT_OWNED:
            new = inc.get(col)
            old = _iso(row.get(col)) if col.endswith("_date") else row.get(col)
            if new != old:
                fields[col] = new

        # A row that left in an earlier version and is back in this one is
        # revived in place: its allocations and progress are still attached.
        if row.get("removed_in_version") is not None:
            fields["removed_in_version"] = None

        if not fields:
            # An import that changes nothing must not bump every row's version
            # and make the diff read as a hundred changes.
            continue

        shift = _days_between(inc.get("start_date"), row.get("start_date"))
        if shift:
            date_shifted += 1
            max_shift = max(max_shift, shift)
        if row.get("locally_modified"):
            locally_modified_overwritten.append(
                {"id": row["id"], "name": row.get("name")})
        updates.append({"id": row["id"], "source_task_id": src, "fields": fields})

    departed = [row for src, row in existing_by_src.items()
                if src not in incoming_by_src
                and row.get("removed_in_version") is None]
    for row in departed:
        removals.append({"id": row["id"], "source_task_id": row["source_task_id"],
                         "name": row.get("name"),
                         "removed_in_version": version_no,
                         "archived_with_parent": False})

    # Local rows hanging off a departing imported row go with it — archived,
    # not deleted, and flagged so the UI can say why they vanished.
    departing_ids = {r["id"] for r in removals}
    for row in local:
        if row.get("parent_id") in departing_ids \
                and row.get("removed_in_version") is None:
            removals.append({"id": row["id"], "source_task_id": None,
                             "name": row.get("name"),
                             "removed_in_version": version_no,
                             "archived_with_parent": True})

    for src, inc in incoming_by_src.items():
        if src in existing_by_src:
            continue
        inserts.append({
            "source_task_id": src,
            "parent_source_id": inc.get("parent_source_id"),
            "name": inc["name"], "wbs_code": inc.get("wbs_code"),
            "start_date": inc.get("start_date"), "end_date": inc.get("end_date"),
            "duration_days": inc.get("duration_days"),
            "first_seen_version": version_no,
        })

    return {
        "insert": inserts,
        "update": updates,
        "remove": removals,
        "rename_candidates": _rename_candidates(departed, inserts),
        "summary": {
            "added": len(inserts),
            "removed": len([r for r in removals if not r["archived_with_parent"]]),
            "archived_with_parent": len(
                [r for r in removals if r["archived_with_parent"]]),
            "updated": len(updates),
            "date_shifted": date_shifted,
            "max_shift_days": max_shift,
            "locally_modified_overwritten": locally_modified_overwritten,
        },
    }


def _rename_candidates(departed, inserts):
    """Pair a disappearance with an arrival that is plausibly the same task.

    A planner renaming an Activity ID produces exactly this shape. Offering
    the repair costs one column update and preserves the row's whole history;
    not offering it orphans allocations and progress with no visible cause.

    Deliberately strict, and each arrival is claimed at most once: a wrong
    pairing transplants one task's history onto another, which is worse than
    making the user redo an allocation.
    """
    out = []
    taken = set()
    for gone in departed:
        for inc in inserts:
            if inc["source_task_id"] in taken:
                continue
            if _norm(gone.get("name")) != _norm(inc.get("name")):
                continue
            drift = _days_between(gone.get("start_date"), inc.get("start_date"))
            if drift is not None and drift > _RENAME_MAX_DAY_DRIFT:
                continue
            out.append({
                "existing_id": gone["id"],
                "existing_source_task_id": gone.get("source_task_id"),
                "incoming_source_task_id": inc["source_task_id"],
                "name": inc.get("name"),
            })
            taken.add(inc["source_task_id"])
            break
    return out


def suggest_mode(existing, parents, leaves) -> str:
    """Which mode to preselect on the diff screen.

    A first import has nothing to replace, so it is always 'update' —
    preselecting Replace on an empty programme would put a destructive-looking
    confirmation in front of a harmless action.
    """
    have = {t.get("source_task_id") for t in existing
            if t.get("origin") == "imported" and t.get("source_task_id")}
    incoming = {r["source_task_id"] for r in _incoming_rows(parents, leaves)}
    if not have or not incoming:
        return "update"
    overlap = len(have & incoming) / len(have | incoming)
    return "update" if overlap >= _REPLACE_SUGGEST_BELOW else "replace"
