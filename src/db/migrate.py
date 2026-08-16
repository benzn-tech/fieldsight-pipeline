"""Versioned .sql migration runner. No ORM; no psycopg import at module top."""
import os


def parse_version(filename: str) -> int:
    return int(filename.split("_", 1)[0])


def pending_versions(all_files: list[str], applied: set[str]) -> list[str]:
    """Pending migrations, in the order they must run.

    The filename is the tie-break, and it is there because two files can share a version
    number. `schema_migrations` is keyed on the full filename, so a duplicate number does
    NOT make one of them silently skip — both apply, which is the important half and was
    already true. What was missing is an ORDER: `sorted` is stable, so among equal versions
    the sequence was whatever `os.listdir` returned in `apply_migrations`, which is not
    defined and can differ between environments.

    Two collisions have already shipped (0041_user_deletion / 0041_turn_name_display, then
    0044_chunk_archive / 0044_speaker_name_rejections; all four are recorded as applied on
    TEST). They were harmless because each pair touches unrelated tables. The next pair
    might not be, and that failure would appear on one environment and not another, with
    nothing in the code to point at.

    The version still decides — the name is only consulted for a tie."""
    todo = [f for f in all_files if f.endswith(".sql") and f not in applied]
    return sorted(todo, key=lambda f: (parse_version(f), f))


def applied_versions(conn) -> set[str]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {r[0] for r in rows}


def apply_migrations(conn, migrations_dir: str) -> list[str]:
    done = applied_versions(conn)
    all_files = os.listdir(migrations_dir)
    applied_now: list[str] = []
    for fname in pending_versions(all_files, done):
        with open(os.path.join(migrations_dir, fname), "r", encoding="utf-8") as fh:
            sql = fh.read()
        with conn.transaction():  # atomic: file DDL + version row commit together
            conn.execute(sql)  # no params -> simple query protocol -> multi-statement OK
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (fname,))
        applied_now.append(fname)
    return applied_now
