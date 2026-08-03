"""Validate migration 0027's DDL and the programme window CTE against the real
test Aurora, inside a transaction that is ROLLED BACK. Nothing persists —
schema_migrations is untouched and the tables do not survive the run.

Why this exists: the unit tests use a recording fake cursor, so they can assert
what the SQL *says* but not that it runs. The recursive CTE in
repositories/programme_window.py is the piece that most needs the difference —
a non-terminating recursion there is a Lambda timeout on every programme read
for that site, and no unit test can catch it.

This is a manual tool, not part of pytest: it needs AWS credentials for the
test account and the Data API. tests/integration/test_programme_window.py
covers the same ground against TEST_DATABASE_URL and is the one CI should run.

Run:  python scripts/verify_programme_schema.py
"""
import json
import os
import subprocess
import sys

CLUSTER = ("arn:aws:rds:ap-southeast-2:509194952652:cluster:"
           "fieldsight-db-test-dbcluster-hywiixu8ihi9")
SECRET = ("arn:aws:secretsmanager:ap-southeast-2:509194952652:secret:"
          "rds!cluster-1757a281-ee31-460d-b56e-950817921010-Ansbey")
DB = "fieldsight_test"

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "src", "migrations",
                         "0027_programme_tables.sql")


def aws(*args):
    r = subprocess.run(["aws"] + list(args), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:4000])
    return json.loads(r.stdout) if r.stdout.strip() else {}


def begin():
    return aws("rds-data", "begin-transaction", "--resource-arn", CLUSTER,
               "--secret-arn", SECRET, "--database", DB,
               "--output", "json")["transactionId"]


def run(tx, sql):
    return aws("rds-data", "execute-statement", "--resource-arn", CLUSTER,
               "--secret-arn", SECRET, "--database", DB,
               "--transaction-id", tx, "--sql", sql, "--output", "json")


def expect_rejection(tx, sql, label, *needles):
    """A constraint violation aborts the whole transaction in Postgres
    (SQLSTATE 25P02), so every expected-failure probe has to sit inside its
    own savepoint or the checks after it die with 'transaction is aborted'
    rather than testing anything."""
    run(tx, f"SAVEPOINT sp_{label}")
    try:
        run(tx, sql)
    except RuntimeError as e:
        run(tx, f"ROLLBACK TO SAVEPOINT sp_{label}")
        msg = str(e).lower()
        assert any(n in msg for n in needles), f"unexpected error: {e}"
        return True
    run(tx, f"ROLLBACK TO SAVEPOINT sp_{label}")
    return False


def rollback(tx):
    return aws("rds-data", "rollback-transaction", "--resource-arn", CLUSTER,
               "--secret-arn", SECRET, "--transaction-id", tx, "--output", "json")


def split_statements(sql):
    """Strip -- comments, split on top-level semicolons. The migration has no
    semicolons inside string literals, so this is sufficient here."""
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    body = "\n".join(lines)
    return [s.strip() for s in body.split(";") if s.strip()]


WINDOW_SQL = """
WITH RECURSIVE matched AS (
    SELECT t.id, t.parent_id
      FROM programme_tasks t
     WHERE t.programme_id = '{pid}'
       AND t.removed_in_version IS NULL
       AND t.start_date <= '2026-05-31'
       AND t.end_date   >= '2026-05-01'
),
with_ancestors AS (
    SELECT id, parent_id, true AS in_window FROM matched
    UNION
    SELECT p.id, p.parent_id, false AS in_window
      FROM programme_tasks p
      JOIN with_ancestors w ON w.parent_id = p.id
     WHERE p.removed_in_version IS NULL
)
SELECT t.source_task_id, bool_or(w.in_window) AS in_window
  FROM programme_tasks t
  JOIN with_ancestors w ON w.id = t.id
 GROUP BY t.id, t.source_task_id, t.sort_order
 ORDER BY t.sort_order
"""


def main():
    sql = open(MIGRATION, encoding="utf-8").read()
    statements = split_statements(sql)
    print(f"migration 0027: {len(statements)} statements")

    tx = begin()
    print(f"transaction {tx[:16]}… opened — everything below is rolled back\n")
    try:
        for i, st in enumerate(statements, 1):
            run(tx, st)
            head = " ".join(st.split())[:64]
            print(f"  [{i:2}/{len(statements)}] OK  {head}")

        print("\nDDL applied. Seeding a tree that spans the window boundary…")
        cid = run(tx, "INSERT INTO companies (name) VALUES ('VERIFY') RETURNING id"
                  )["records"][0][0]["stringValue"]
        sid = run(tx, f"INSERT INTO sites (company_id, name) "
                      f"VALUES ('{cid}','VERIFY') RETURNING id"
                  )["records"][0][0]["stringValue"]
        pid = run(tx, f"INSERT INTO programmes (site_id, name) "
                      f"VALUES ('{sid}','P') RETURNING id"
                  )["records"][0][0]["stringValue"]

        def task(src, parent, start, end, order):
            p = f"'{parent}'" if parent else "NULL"
            s = f"'{start}'" if start else "NULL"
            e = f"'{end}'" if end else "NULL"
            return run(tx,
                       f"INSERT INTO programme_tasks (programme_id, source_task_id, "
                       f"parent_id, origin, name, start_date, end_date, "
                       f"first_seen_version, sort_order) "
                       f"VALUES ('{pid}','{src}',{p},'imported','{src}',{s},{e},1,{order}) "
                       f"RETURNING id")["records"][0][0]["stringValue"]

        g1 = task("G1", None, None, None, 0)             # root header, no dates
        m1 = task("M1", g1, "2026-01-01", "2026-01-31", 1)   # ancestor OUTSIDE
        l1 = task("L1", m1, "2026-05-01", "2026-05-10", 2)   # inside
        task("L2", m1, "2026-01-05", "2026-01-10", 3)        # outside entirely
        m2 = task("M2", g1, "2026-04-01", "2026-06-30", 4)   # spans the window

        rows = run(tx, WINDOW_SQL.format(pid=pid))["records"]
        got = {r[0]["stringValue"]: r[1]["booleanValue"] for r in rows}
        print(f"  window query returned: {got}")

        expect = {"G1": False, "M1": False, "L1": True, "M2": True}
        assert got == expect, f"expected {expect}, got {got}"
        print("  PASS  ancestors walk to the root, marked as context")
        print("  PASS  a task spanning the window is in it (overlap, not containment)")
        print("  PASS  a task wholly outside is absent")

        print("\nCycle check: pointing the root at its own descendant…")
        run(tx, f"UPDATE programme_tasks SET parent_id = '{l1}' WHERE id = '{g1}'")
        rows = run(tx, WINDOW_SQL.format(pid=pid))["records"]
        print(f"  PASS  query terminated, {len(rows)} rows — UNION stops the cycle")

        print("\nCHECK constraint: an imported row with no source_task_id…")
        ok1 = expect_rejection(
            tx,
            f"INSERT INTO programme_tasks (programme_id, origin, name, "
            f"first_seen_version) VALUES ('{pid}','imported','bad',1)",
            "chk_imported", "constraint", "check")
        print("  PASS  rejected" if ok1 else "  FAIL  the CHECK did not fire")

        print("\nCHECK constraint: a local row carrying a source_task_id…")
        ok2 = expect_rejection(
            tx,
            f"INSERT INTO programme_tasks (programme_id, source_task_id, origin, "
            f"name, first_seen_version) VALUES ('{pid}','X1','local','bad',1)",
            "chk_local", "constraint", "check")
        print("  PASS  rejected" if ok2 else "  FAIL  the CHECK did not fire")

        print("\nPartial unique index: two imported rows sharing a source id…")
        ok3 = expect_rejection(
            tx,
            f"INSERT INTO programme_tasks (programme_id, source_task_id, "
            f"origin, name, first_seen_version) "
            f"VALUES ('{pid}','L1','imported','dupe',1)",
            "uq_source", "unique", "duplicate")
        print("  PASS  rejected" if ok3 else "  FAIL  the unique index did not fire")

        print("\nprogress_pct range check: 150%…")
        ok4 = expect_rejection(
            tx,
            f"INSERT INTO programme_tasks (programme_id, source_task_id, origin, "
            f"name, progress_pct, first_seen_version) "
            f"VALUES ('{pid}','P9','imported','over',150,1)",
            "chk_pct", "constraint", "check")
        print("  PASS  rejected" if ok4 else "  FAIL  the range check did not fire")

        if not all((ok1, ok2, ok3, ok4)):
            return 1

        print("\nPartial index tolerance: many local rows, all NULL source id…")
        for n in range(3):
            run(tx, f"INSERT INTO programme_tasks (programme_id, parent_id, origin, "
                    f"name, first_seen_version) "
                    f"VALUES ('{pid}','{m2}','local','sub{n}',1)")
        print("  PASS  NULLs do not collide")
        return 0
    finally:
        rollback(tx)
        print("\ntransaction rolled back — nothing persisted")


if __name__ == "__main__":
    sys.exit(main())
