"""Run tests/integration/test_programme_window.py's assertions against the
REAL deployed schema on fieldsight_test, inside a transaction that is rolled
back. Nothing persists.

Why this exists alongside the pytest file: the test cluster is not publicly
accessible (`PubliclyAccessible: false`), so `TEST_DATABASE_URL` cannot be
reached from outside the VPC and pytest's integration suite cannot run here.
The Data API can. These are the same assertions, driven the only way this
environment allows.

scripts/verify_programme_schema.py is the sibling that validated the DDL
before migration 0027 was applied. It no longer runs, by design: the tables
now exist, so its CREATE statements fail with 42P07. Keep it for the record
of that validation; use this one from now on.

Run:  python scripts/verify_programme_window.py
"""
import json
import subprocess
import sys

CLUSTER = ("arn:aws:rds:ap-southeast-2:509194952652:cluster:"
           "fieldsight-db-test-dbcluster-hywiixu8ihi9")
SECRET = ("arn:aws:secretsmanager:ap-southeast-2:509194952652:secret:"
          "rds!cluster-1757a281-ee31-460d-b56e-950817921010-Ansbey")
DB = "fieldsight_test"


def aws(*args):
    r = subprocess.run(["aws"] + list(args), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:2000])
    return json.loads(r.stdout) if r.stdout.strip() else {}


def begin():
    return aws("rds-data", "begin-transaction", "--resource-arn", CLUSTER,
               "--secret-arn", SECRET, "--database", DB,
               "--output", "json")["transactionId"]


def run(tx, sql):
    return aws("rds-data", "execute-statement", "--resource-arn", CLUSTER,
               "--secret-arn", SECRET, "--database", DB,
               "--transaction-id", tx, "--sql", sql, "--output", "json")


def rollback(tx):
    return aws("rds-data", "rollback-transaction", "--resource-arn", CLUSTER,
               "--secret-arn", SECRET, "--transaction-id", tx, "--output", "json")


def one(res):
    return res["records"][0][0]["stringValue"]


# The query under test, verbatim from repositories/programme_window.py apart
# from the parameter substitution the Data API needs here.
def window_sql(pid, assignee=None):
    clause = ""
    if assignee is not None:
        clause = ("       AND EXISTS (SELECT 1 FROM programme_task_assignees a "
                  f"                  WHERE a.task_id = t.id AND a.assignee = '{assignee}')")
    return f"""
WITH RECURSIVE matched AS (
    SELECT t.id, t.parent_id
      FROM programme_tasks t
     WHERE t.programme_id = '{pid}'
       AND t.removed_in_version IS NULL
       AND t.start_date <= '2026-05-31'
       AND t.end_date   >= '2026-05-01'
{clause}
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


def rows(tx, pid, assignee=None):
    res = run(tx, window_sql(pid, assignee))
    return {r[0]["stringValue"]: r[1]["booleanValue"] for r in res["records"]}


CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


def main():
    tx = begin()
    print(f"transaction {tx[:14]}… opened — everything below is rolled back\n")
    failures = 0
    try:
        cid = one(run(tx, "INSERT INTO companies (name) VALUES ('VERIFY') RETURNING id"))
        sid = one(run(tx, f"INSERT INTO sites (company_id, name) "
                          f"VALUES ('{cid}','VERIFY') RETURNING id"))
        pid = one(run(tx, f"INSERT INTO programmes (site_id, name) "
                          f"VALUES ('{sid}','P') RETURNING id"))

        def task(src, parent, start, end, order):
            p = f"'{parent}'" if parent else "NULL"
            s = f"'{start}'" if start else "NULL"
            e = f"'{end}'" if end else "NULL"
            return one(run(tx,
                f"INSERT INTO programme_tasks (programme_id, source_task_id, "
                f"parent_id, origin, name, start_date, end_date, "
                f"first_seen_version, sort_order) "
                f"VALUES ('{pid}','{src}',{p},'imported','{src}',{s},{e},1,{order}) "
                f"RETURNING id"))

        ids = {}
        ids["G1"] = task("G1", None, None, None, 0)            # root, no dates
        ids["M1"] = task("M1", ids["G1"], "2026-01-01", "2026-01-31", 1)  # ancestor OUTSIDE
        ids["L1"] = task("L1", ids["M1"], "2026-05-01", "2026-05-10", 2)  # inside
        ids["L2"] = task("L2", ids["M1"], "2026-01-05", "2026-01-10", 3)  # outside
        ids["M2"] = task("M2", ids["G1"], "2026-04-01", "2026-06-30", 4)  # spans window

        ctx = {"tx": tx, "pid": pid, "ids": ids}
        for name, fn in CASES:
            try:
                fn(ctx)
                print(f"  PASS  {name}")
            except AssertionError as e:
                failures += 1
                print(f"  FAIL  {name}\n        {e}")
        return 1 if failures else 0
    finally:
        rollback(tx)
        print("\ntransaction rolled back — nothing persisted")


@case("the recursive ancestor walk reaches the root")
def _(c):
    got = rows(c["tx"], c["pid"])
    for k in ("L1", "M1", "G1"):
        assert k in got, f"{k} missing from {sorted(got)}"


@case("ancestors come back as context, matches as content")
def _(c):
    got = rows(c["tx"], c["pid"])
    assert got["L1"] is True and got["M1"] is False and got["G1"] is False, got


@case("a task spanning the whole window is in it (overlap, not containment)")
def _(c):
    got = rows(c["tx"], c["pid"])
    assert got.get("M2") is True, got


@case("a task wholly outside the window is not returned")
def _(c):
    assert "L2" not in rows(c["tx"], c["pid"])


@case("a row reached both ways counts as in_window (bool_or)")
def _(c):
    tx, pid, ids = c["tx"], c["pid"], c["ids"]
    run(tx, "SAVEPOINT sp_boolor")
    run(tx, f"INSERT INTO programme_tasks (programme_id, source_task_id, parent_id, "
            f"origin, name, start_date, end_date, first_seen_version, sort_order) "
            f"VALUES ('{pid}','M2C','{ids['M2']}','imported','child',"
            f"'2026-05-02','2026-05-03',1,5)")
    assert rows(tx, pid)["M2"] is True
    run(tx, "ROLLBACK TO SAVEPOINT sp_boolor")


@case("the assignee filter narrows to that person")
def _(c):
    tx, pid, ids = c["tx"], c["pid"], c["ids"]
    run(tx, "SAVEPOINT sp_assignee")
    run(tx, f"INSERT INTO programme_task_assignees (task_id, assignee) "
            f"VALUES ('{ids['L1']}','Sam_SM')")
    mine = rows(tx, pid, assignee="Sam_SM")
    assert "L1" in mine, mine
    assert "M2" not in mine, f"M2 is in the window but assigned to nobody: {mine}"
    # unfiltered still returns everyone
    assert "M2" in rows(tx, pid)
    run(tx, "ROLLBACK TO SAVEPOINT sp_assignee")


@case("an assignee with nothing assigned gets EMPTY, not the whole programme")
def _(c):
    assert rows(c["tx"], c["pid"], assignee="Nobody_Home") == {}


@case("soft-removed rows are excluded even as ancestors")
def _(c):
    tx, pid, ids = c["tx"], c["pid"], c["ids"]
    run(tx, "SAVEPOINT sp_removed")
    run(tx, f"UPDATE programme_tasks SET removed_in_version = 2 "
            f"WHERE id = '{ids['M1']}'")
    got = rows(tx, pid)
    assert "M1" not in got, got
    assert "L1" in got, "the child still matches; only its removed ancestor drops"
    run(tx, "ROLLBACK TO SAVEPOINT sp_removed")


@case("a cycle in parent_id does not hang the query")
def _(c):
    tx, pid, ids = c["tx"], c["pid"], c["ids"]
    run(tx, "SAVEPOINT sp_cycle")
    run(tx, f"UPDATE programme_tasks SET parent_id = '{ids['L1']}' "
            f"WHERE id = '{ids['G1']}'")
    assert rows(tx, pid), "the query must terminate and return something"
    run(tx, "ROLLBACK TO SAVEPOINT sp_cycle")


if __name__ == "__main__":
    sys.exit(main())
