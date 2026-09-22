"""Writing tags, and nothing else.

Adding or changing the taxonomy and re-tagging existing data must never alter
a topic's body, an action's text, a summary or a report. That is an owner-level
boundary, so it is enforced by the SHAPE of this module rather than by anyone
remembering:

  * neither `apply_tags` nor `rollback_run` takes a content argument. There is
    no parameter through which body text could travel, so no caller can pass
    one by accident and no future edit can add one without being conspicuous;
  * these are the ONLY functions that write the assignment tables, and they
    issue exactly four statement shapes. tests/unit/test_tagging_writes_only_
    tags.py captures every statement a whole run makes and asserts the set --
    an `UPDATE topics` added by a helper three calls deep fails there.

Separate from repositories/tags.py on purpose. That module is the VOCABULARY
(what words exist, who may edit them); this one is the ASSIGNMENTS (which
words are on which row). They have different callers, different permissions,
and mixing them would put a function that can write topic_tags one import away
from every read path in org-api.

`source` is the vocabulary the photo binding learned the hard way: a machine
may replace what a machine wrote, and never what a person chose.
"""
from psycopg.rows import dict_row

#: Which table an entity kind's assignments live in. Two tables rather than one
#: polymorphic (target_type, target_id) table -- see migration 0065 -- so a
#: deleted topic's tags go with it through ON DELETE CASCADE.
_TABLES = {"topic": ("topic_tags", "topic_id"),
           "action_item": ("action_item_tags", "action_item_id")}

#: Who wrote an assignment. 'human' is the one a machine never overwrites.
_SOURCES = {"extraction", "classifier", "embedding", "human"}


def start_run(conn, *, company_id, taxonomy_version, method, created_by=None) -> dict:
    """Open a re-tag batch, so its rows can be undone as a batch.

    Without a run id the only way to undo a run is to delete by (source, time
    window), which cannot tell a bad run's rows from a good one that overlapped
    it. `taxonomy_version` is recorded because the answer to "why did this row
    get that tag" is usually "the vocabulary was different then".
    """
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO tag_run (company_id, taxonomy_version, method, status, created_by) "
        "VALUES (%s,%s,%s,'running',%s) RETURNING id, company_id, method, status",
        (str(company_id), int(taxonomy_version), method,
         str(created_by) if created_by else None),
    ).fetchone()


def finish_run(conn, run_id, *, stats=None) -> None:
    from psycopg.types.json import Jsonb
    conn.execute(
        "UPDATE tag_run SET status='done', finished_at=now(), stats=%s WHERE id=%s",
        (Jsonb(stats or {}), run_id))


def apply_tags(conn, kind, entity_id, tag_ids, *, source, run_id=None,
               confidence=None) -> int:
    """Put `tag_ids` on one topic or action item. Returns rows written.

    NO CONTENT PARAMETER, and that is the point -- see the module docstring.

    Re-entrant: `ON CONFLICT DO NOTHING` against the (entity, tag) primary key.
    A re-tag is re-driven routinely (a retry, a resumed batch, an operator
    running it again) and has to be safe to run twice. It also means a row a
    HUMAN already placed is left exactly as it is rather than having its
    `source` downgraded to a machine's -- the conflict is on the pair, and
    doing nothing is the correct outcome.

    An unknown `kind` or `source` RAISES rather than being guessed at. Both
    choose something that cannot be recovered afterwards: the wrong table, or a
    row no rebind will ever touch again because it is neither machine nor
    human.
    """
    if kind not in _TABLES:
        raise ValueError(f"unknown entity kind {kind!r}; expected one of {sorted(_TABLES)}")
    if source not in _SOURCES:
        raise ValueError(f"unknown tag source {source!r}; expected one of {sorted(_SOURCES)}")
    ids = [t for t in (tag_ids or []) if t]
    if not ids:
        # Nothing to write is not an error, and writing nothing must not look
        # like a failed call: half of a real corpus takes no tag at all.
        return 0
    table, col = _TABLES[kind]
    written = 0
    for tag_id in ids:
        conn.execute(
            f"INSERT INTO {table} ({col}, tag_id, source, confidence, run_id) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (entity_id, tag_id, source, confidence, run_id))
        written += 1
    return written


def rollback_run(conn, run_id) -> None:
    """Undo one batch: remove the tags it wrote, and say so on the run.

    `source <> 'human'` is the bound, and it is not theoretical. A person can
    correct one row in the middle of a batch -- that row carries the batch's
    run_id and the person's source -- and undoing the batch must leave their
    correction standing. The photo binding has the same rule for the same
    reason.

    The run row is MARKED, never deleted. A run that vanishes takes the record
    of what happened with it, and "this was rolled back" is the fact somebody
    will be looking for.
    """
    for table, _col in _TABLES.values():
        conn.execute(
            f"DELETE FROM {table} WHERE run_id = %s AND source <> 'human'", (run_id,))
    conn.execute(
        "UPDATE tag_run SET status='rolled_back', finished_at=now() WHERE id=%s",
        (run_id,))
