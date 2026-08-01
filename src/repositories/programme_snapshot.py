"""Regenerates the legacy programmes/{site_id}/programme.json document from
the Aurora rows (migration 0027).

Why this exists: lambda_programme_matcher.py reads that S3 document and its
candidate_tasks() consumes `leaves`. Keeping the snapshot current means Aurora
can become the source of truth without touching the matcher, and means the
whole change can be reverted by pointing the frontend back at
GET/PUT /programme. A later cutover points the matcher at Aurora and deletes
this module.

The old document had exactly two levels — `parents` and `leaves` — and the
Aurora tree has arbitrary depth, so the split has to be derived. **Dates
decide it, not children.**

That is not the obvious rule, and the obvious one is actively harmful. Sorting
by "has children" looks right until a PM breaks a contract task down: "Pour
slab" acquires local subtasks, becomes a parent, drops out of `leaves`, and
silently stops being a match candidate — so a site manager saying "we poured
the slab today" no longer lands on it. Nothing raises; the task just goes
quiet. tests/unit/test_programme_snapshot_matcher_contract.py caught exactly
that.

In the old document, `parents` were WBS headers, which carry no dates. A task
with dates is schedulable work whether or not anything hangs off it, so it
belongs in `leaves`. A broken-down task therefore appears alongside its own
subtasks, and both are matchable — which is what you want: general speech
lands on the parent, specific speech on the subtask.
"""


def _iso(value):
    """psycopg hands back datetime.date; the document is JSON."""
    if value is None:
        return None
    return value if isinstance(value, str) else value.isoformat()


def _doc_id(row):
    """The file's identifier for imported rows, our UUID for local ones.

    This must stay the source id for imported rows: programme_progress_
    suggestions.task_id already holds it, and the confirm path looks the task
    up in this document by that value.
    """
    return row["source_task_id"] or str(row["id"])


def build_snapshot(programme, tasks) -> dict:
    live = [t for t in tasks if t.get("removed_in_version") is None]

    by_uuid = {str(t["id"]): t for t in live}

    parents, leaves = [], []
    for t in live:
        # No dates => a structural WBS header, which is what `parents` meant
        # in the legacy document. Anything with dates is schedulable work and
        # has to stay in `leaves` to remain matchable, children or not.
        if not t.get("start_date") and not t.get("end_date"):
            parents.append({
                "task_id": _doc_id(t),
                "name":    t.get("name"),
                "wbs":     t.get("wbs_code"),
            })
            continue

        parent_row = by_uuid.get(str(t["parent_id"])) if t.get("parent_id") else None
        leaves.append({
            "task_id":       _doc_id(t),
            "parent_id":     _doc_id(parent_row) if parent_row else None,
            "name":          t.get("name"),
            "wbs":           t.get("wbs_code"),
            "start":         _iso(t.get("start_date")),
            "end":           _iso(t.get("end_date")),
            "duration_days": t.get("duration_days"),
            "progress_pct":  t.get("progress_pct") or 0,
            "status":        t.get("status") or "not_started",
            "assignees":     t.get("assignees") or [],
            "depends_on":    t.get("depends_on") or [],
            "linked_action_items": [],
        })

    starts = [leaf["start"] for leaf in leaves if leaf["start"]]
    ends = [leaf["end"] for leaf in leaves if leaf["end"]]

    return {
        "name":       programme.get("name"),
        "start_date": min(starts) if starts else None,
        "end_date":   max(ends) if ends else None,
        "parents":    parents,
        "leaves":     leaves,
    }
