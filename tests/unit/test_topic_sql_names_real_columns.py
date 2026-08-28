"""Unit: every `t.<column>` in the topics repository names a column `topics` actually has.

`list_expired_non_work` selected `t.company_id`. **`topics` has no such column** — it never
did; the tenant is reached through `users`, which is why every other query in this file joins
`users u ON u.id = t.user_id` and reads `u.company_id`.

The sweep that runs it therefore raises `UndefinedColumn` the moment it finds a row to expire,
inside a scheduled lambda nobody watches. Two things kept it invisible. The unit suite drives a
connection double, and **a double records SQL, it does not parse it** — so the statement was
asserted on, character by character, by a test that could not know the column was imaginary.
And the sweep only executes the query's tail when there is expired non-work content to find,
which on TEST there has not yet been.

So this is not a test for one typo. It is the check that the double cannot perform: the
columns come out of the migrations, and any `t.` reference that names something not among them
is red before it reaches a database.

The alias is deliberately narrow. `u.` and `r.` references belong to other tables, and
widening this to every alias in the file would mean parsing every join — more machinery than
the question needs, and machinery that fails open when it drifts.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIGRATIONS = os.path.join(ROOT, "src", "migrations")
REPO = os.path.join(ROOT, "src", "repositories", "topics.py")

# Columns that exist on a query's output rather than on the table: an alias introduced by the
# query itself is not a schema question. Listed rather than pattern-matched, so adding one is
# a decision somebody makes on purpose.
NOT_SCHEMA = set()


def _topics_columns():
    """Every column `topics` has, from CREATE TABLE plus later ADD COLUMNs."""
    cols = set()
    for f in sorted(os.listdir(MIGRATIONS)):
        if not f.endswith(".sql"):
            continue
        sql = open(os.path.join(MIGRATIONS, f), encoding="utf-8").read()
        m = re.search(r"CREATE TABLE (?:IF NOT EXISTS )?topics\s*\((.*?)\n\);", sql, re.S)
        if m:
            for line in m.group(1).splitlines():
                line = line.strip()
                cm = re.match(r"([a-z_][a-z0-9_]*)\s+[a-z]", line, re.I)
                # Skip table-level constraints, which also start at column 0.
                if cm and cm.group(1).upper() not in (
                        "PRIMARY", "UNIQUE", "FOREIGN", "CONSTRAINT", "CHECK"):
                    cols.add(cm.group(1))
        for am in re.finditer(
                r"ALTER TABLE (?:IF EXISTS )?topics\s+ADD COLUMN (?:IF NOT EXISTS )?"
                r"([a-z_][a-z0-9_]*)", sql, re.I):
            cols.add(am.group(1))
    return cols


def test_the_migrations_are_being_read_at_all():
    """The guard above fails OPEN if the parse returns nothing: an empty column set makes
    every reference an offender, which is loud, but an empty set of REFERENCES would make the
    real test vacuously green. Both ends are pinned here so a rename of the migrations
    directory cannot silently retire the check."""
    cols = _topics_columns()
    assert {"id", "user_id", "source_s3_key", "report_date"} <= cols, sorted(cols)
    assert "company_id" not in cols, (
        "topics gained a company_id column. That is allowed — but the sweep and every other "
        "query in this file reach the tenant through users, and two routes to one fact is "
        "the shape this repository keeps removing. Decide deliberately, then delete this "
        "assertion.")


def test_every_t_dot_reference_names_a_real_topics_column():
    src = open(REPO, encoding="utf-8").read()
    cols = _topics_columns()
    bad = sorted({m.group(1) for m in re.finditer(r"\bt\.([a-z_][a-z0-9_]*)", src)}
                 - cols - NOT_SCHEMA)
    assert not bad, (
        f"these are selected or filtered as topics columns and topics has none of them: "
        f"{bad}. A connection double records the SQL without parsing it, so this fails "
        f"first at runtime, in a background lambda, as UndefinedColumn.")
