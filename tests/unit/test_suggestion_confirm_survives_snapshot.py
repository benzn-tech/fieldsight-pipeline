"""
Regression: confirming a matcher suggestion must survive the next Aurora
write.

Project 1 made programme_tasks (Aurora) the source of truth and kept
programme.json as a derived artefact: every Aurora programme write calls
lambda_org_api._write_snapshot, which rebuilds the whole document from the
table (seven call sites — batch PATCH, import, restore, delay flags...).

confirm_suggestion did not move with it. It read programme.json, mutated the
matched leaf in memory, and called programme.write_programme directly.
Aurora was never told. So a confirmed status/progress lived ONLY in a derived
file, and the next write of ANY task in that programme regenerated that file
from the table and erased it.

The failure was silent and delayed, which is what made it worth a test rather
than a comment. The confirm returned 200. The value was visibly applied. It
disappeared minutes or days later when an unrelated task was edited, with
nothing in the logs connecting the two — and the reviewer who confirmed it had
long since moved on.

It was also the blocker for showing suggestions inline on the Gantt (Project 3
§2): the row renders from Aurora, so an accept in place would have appeared to
do nothing on reload.

These tests drive the repository and snapshot layers directly. The handler
wiring — that confirm calls update_task at all, and that decide() unwinds when
the write loses the optimistic lock — is covered in
test_lambda_org_api.py::test_confirm_second_pending_suggestion_for_other_task_not_blocked.
"""
from repositories import programme_snapshot as snap

PROGRAMME = {"id": "p1", "name": "Main contract"}


def task(tid, *, source, parent=None, name="T", start=None, end=None,
         origin="imported", removed=None, progress=0, status="not_started"):
    return {
        "id": tid, "source_task_id": source, "parent_id": parent,
        "origin": origin, "name": name, "wbs_code": None,
        "start_date": start, "end_date": end, "duration_days": None,
        "progress_pct": progress, "status": status,
        "removed_in_version": removed,
    }


def aurora_rows():
    """Fresh each time — these tests mutate them the way an UPDATE would."""
    return [
        task("uuid-g", source="G1", name="Foundations"),
        task("uuid-a", source="A1020", parent="uuid-g", name="Pour slab",
             start="2026-04-01", end="2026-04-10"),
        task("uuid-local", source=None, origin="local", parent="uuid-a",
             name="Rebar fixing", start="2026-04-02", end="2026-04-06"),
    ]


def leaf(doc, task_id):
    return next(t for t in doc["leaves"] if t["task_id"] == task_id)


def confirm(rows, doc_id, *, status, progress):
    """What confirm_suggestion does now: resolve the document's task_id back
    to its row and write the ROW.

    The resolution rule is programme_snapshot._doc_id read backwards —
    source_task_id for imported rows, the UUID string for local ones — which
    is what repositories.programme_tasks.get_task_by_doc_id implements in
    SQL. Reproduced here in Python because these tests have no database; the
    SQL form is exercised by tests/integration.
    """
    row = next(r for r in rows
               if (r["source_task_id"] or str(r["id"])) == doc_id)
    row["status"] = status
    row["progress_pct"] = progress
    return row


def test_a_confirmed_suggestion_survives_the_next_aurora_write():
    """Ben confirms 'slab is about half poured'. Someone then edits an
    unrelated task, which regenerates the document from Aurora."""
    rows = aurora_rows()
    confirm(rows, "A1020", status="in_progress", progress=50)

    # Any of the seven _write_snapshot call sites.
    regenerated = snap.build_snapshot(PROGRAMME, rows)

    assert leaf(regenerated, "A1020")["progress_pct"] == 50, (
        "the confirmed progress was erased by an unrelated write — confirm "
        "wrote only to the derived document again")
    assert leaf(regenerated, "A1020")["status"] == "in_progress"


def test_the_gantt_sees_the_confirmation():
    """GET /programme/tasks reads programme_tasks. A confirm that reached only
    programme.json would be invisible there from the first moment, leaving the
    review queue and the Gantt disagreeing about the same task."""
    rows = aurora_rows()
    confirm(rows, "A1020", status="in_progress", progress=50)

    gantt_row = next(r for r in rows if r["source_task_id"] == "A1020")
    assert gantt_row["progress_pct"] == 50
    assert gantt_row["status"] == "in_progress"


def test_a_suggestion_on_a_local_breakdown_subtask_resolves_too():
    """Local rows have no source_task_id, so the snapshot gives them their
    UUID as a document id and a suggestion carries that string back. Both
    identifier spaces share one text column, which is why the lookup has to
    try both — and why a resolver that only checked source_task_id would fail
    silently on exactly the AI-generated subtasks Project 3 creates."""
    rows = aurora_rows()
    confirm(rows, "uuid-local", status="completed", progress=100)

    regenerated = snap.build_snapshot(PROGRAMME, rows)
    assert leaf(regenerated, "uuid-local")["progress_pct"] == 100


def test_the_document_and_the_table_agree_after_a_confirm():
    """The confirm-time staleness check compares the suggestion's
    task_status_before against the live task. That guard is only meaningful
    while every write path maintains the same value — which is precisely what
    the old confirm broke, by producing a document state no other path could
    produce."""
    rows = aurora_rows()
    confirm(rows, "A1020", status="in_progress", progress=50)

    doc = snap.build_snapshot(PROGRAMME, rows)
    table_row = next(r for r in rows if r["source_task_id"] == "A1020")

    assert leaf(doc, "A1020")["progress_pct"] == table_row["progress_pct"]
    assert leaf(doc, "A1020")["status"] == table_row["status"]
