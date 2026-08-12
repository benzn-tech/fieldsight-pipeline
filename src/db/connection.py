"""Imports psycopg/pgvector. Repositories receive a connection from here."""
import json
import os
import psycopg

# pgvector is optional at import time: the migrate Lambda's slim layer ships
# psycopg only (pgvector hard-requires numpy, which SAM's layer builder can't
# resolve for py3.11). Migrations are plain SQL — no vector binding needed.
# Functions that DO bind vectors (Phase 4/5) must ship pgvector and will get
# registration automatically here.
try:
    from pgvector.psycopg import register_vector
except ImportError:  # slim runtime without pgvector
    register_vector = None


def _dsn_from_secret() -> str | None:
    """Fallback DSN assembly for the in-VPC MigrateFunction.

    The app template cannot build a full DATABASE_URL dynamic reference
    because {{resolve:secretsmanager:...}} can't nest Fn::ImportValue for the
    secret ARN. Instead the template passes DB_SECRET_ARN + DB_HOST (plain
    env vars) and grants secretsmanager:GetSecretValue on that ARN; this
    fetches the RDS-managed master password at runtime and assembles the DSN.
    """
    secret_arn = os.environ.get("DB_SECRET_ARN")
    db_host = os.environ.get("DB_HOST")
    if not (secret_arn and db_host):
        return None
    import boto3  # local import: keep the module-level import list psycopg-only

    db_name = os.environ.get("DB_NAME", "fieldsight")
    client = boto3.client("secretsmanager")
    secret = json.loads(client.get_secret_value(SecretId=secret_arn)["SecretString"])
    from urllib.parse import quote

    # RDS-managed secrets may contain URI-reserved chars (#, ?, %, & ...);
    # unescaped they corrupt the DSN psycopg parses.
    username = quote(secret.get("username", "postgres"), safe="")
    password = quote(secret["password"], safe="")
    return f"postgresql://{username}:{password}@{db_host}:5432/{db_name}"


def get_connection(dsn: str | None = None, autocommit: bool = False):
    """Return a psycopg connection with pgvector registered.

    NOTE: repositories never commit. The caller owns the transaction:
    use `with get_connection() as conn:` (commits on clean exit) or pass
    autocommit=True. A bare get_connection() + close() ROLLS BACK writes.
    """
    dsn = dsn or os.environ.get("DATABASE_URL")
    if dsn is None and os.environ.get("PGHOST"):
        # libpq-standard env vars (PGHOST/PGDATABASE/PGUSER/PGPASSWORD):
        # the in-VPC MigrateFunction uses these — deploy-time injected, so
        # no runtime Secrets Manager call (which would hang without NAT).
        dsn = ""
    if dsn is None:
        dsn = _dsn_from_secret()
    if dsn is None:
        raise RuntimeError(
            "No DSN available: set DATABASE_URL, PGHOST env vars, or "
            "DB_SECRET_ARN + DB_HOST."
        )
    conn = psycopg.connect(dsn, autocommit=autocommit)
    if register_vector is not None:
        register_vector(conn)
    return conn


# Module-level connection reused across warm Lambda invocations. Reconnecting
# to Aurora costs ~1-2s per invoke (TLS handshake + auth) and dominated the
# RAG-search latency; a warm container keeps this open so back-to-back searches
# only pay the query time.
_cached_conn = None


def get_cached_connection():
    """Persistent module-level connection for READ-ONLY, latency-sensitive
    paths (rag-search). autocommit=True — there is no transaction to leave open
    across invokes, and psycopg3's `with conn:` would CLOSE the connection
    (defeating reuse). A cheap SELECT 1 liveness check transparently reconnects
    if the cached connection has died. Do NOT use for writes/transactions —
    use `with get_connection() as conn:` for those."""
    global _cached_conn
    if _cached_conn is not None:
        try:
            if not _cached_conn.closed:
                with _cached_conn.cursor() as cur:
                    cur.execute("SELECT 1")
                return _cached_conn
        except Exception:
            try:
                _cached_conn.close()
            except Exception:
                pass
            _cached_conn = None
    _cached_conn = get_connection(autocommit=True)
    return _cached_conn


def close_cached_connection():
    """Drop the cached connection, giving up warm-invoke reuse on purpose.

    Aurora counts any open user-initiated connection as activity, and a frozen
    Lambda container's socket stays established server-side. So one Search/Ask
    leaves the cluster unable to begin its auto-pause idle countdown until that
    container is reclaimed — an interval we neither control nor observe (Aurora
    scale-to-zero, spec 2026-08-11).

    The cost is the ~1-2s reconnect this cache existed to avoid, paid again on
    the next search. That is the trade the design accepts: the reconnect is
    visible only to someone actively searching, while a held connection silently
    bills the 0.5-ACU floor around the clock.

    Idempotent and never raises — it runs in a `finally`, where an exception
    would replace the handler's real result (or its real error) with this one.
    """
    global _cached_conn
    conn, _cached_conn = _cached_conn, None
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        # Not re-raised, but not silent either: a close that keeps failing is
        # the difference between the cluster sleeping and not.
        import logging

        logging.getLogger().warning("could not close cached connection",
                                    exc_info=True)
