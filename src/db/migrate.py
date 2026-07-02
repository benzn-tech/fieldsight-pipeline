"""Versioned .sql migration runner. No ORM; no psycopg import at module top."""
import os


def parse_version(filename: str) -> int:
    return int(filename.split("_", 1)[0])


def pending_versions(all_files: list[str], applied: set[str]) -> list[str]:
    todo = [f for f in all_files if f.endswith(".sql") and f not in applied]
    return sorted(todo, key=parse_version)


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
        conn.execute(sql)  # no params -> simple query protocol -> multi-statement OK
        conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (fname,))
        applied_now.append(fname)
    return applied_now
