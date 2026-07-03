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
    dsn = dsn or os.environ.get("DATABASE_URL") or _dsn_from_secret()
    if not dsn:
        raise RuntimeError(
            "No DSN available: set DATABASE_URL, or DB_SECRET_ARN + DB_HOST."
        )
    conn = psycopg.connect(dsn, autocommit=autocommit)
    if register_vector is not None:
        register_vector(conn)
    return conn
