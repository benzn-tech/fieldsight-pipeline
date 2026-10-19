"""Ingest's apply route for project documents (in-VPC; spec Flow + Error handling + R9).

One transaction per artifact: `get_connection()` gives a fresh NON-autocommit connection
(commit on a clean exit) and `apply_result` runs inside `conn.transaction()`, which the
series advisory lock and FOR UPDATE need.

Never stuck pending (R9): when the artifact cannot be read, disagrees with its own key, or
its apply raises, the document named by the KEY is set to `error` on a fresh connection --
but only while it is still pending (`project_docs.fail_pending`), so a failed re-apply never
takes an indexed document out of search. Only when that mark itself fails (the database is
unreachable) does the error re-raise, so S3 re-delivers the event.

Imported LAZILY by lambda_ingest: lambda_item_writer imports lambda_ingest at module level,
and the extraction pipeline must never import the documents repository (R14).
"""
import json
import logging
import re

from db.connection import get_connection
from repositories import project_docs

logger = logging.getLogger()

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
KEY_RE = re.compile(rf"^project_doc_vectors/(?P<company>{_UUID})/(?P<id>{_UUID})\.json$")


def _fail(key, company_id, doc_id):
    """Mark the key's document error if it is still pending. Raises if the database does."""
    with get_connection() as conn:
        with conn.transaction():
            changed = project_docs.fail_pending(conn, doc_id, company_id)
    logger.warning("project doc apply: %s -> error mark %s", key,
                   "set" if changed else "not needed (not pending)")


def apply_key(key, client, bucket):
    m = KEY_RE.match(key)
    if not m:
        logger.warning("project doc apply: ignoring %s", key)
        return {"project_doc": key, "outcome": "ignored"}
    company_id, doc_id = m.group("company"), m.group("id")
    try:
        result = json.loads(
            client.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("artifact is not a JSON object")
    except Exception:
        logger.exception("project doc apply: cannot read %s", key)
        _fail(key, company_id, doc_id)
        return {"project_doc": key, "outcome": "unreadable_artifact"}
    if (str(result.get("document_id")) != doc_id
            or str(result.get("company_id")) != company_id):
        logger.error("project doc apply: %s does not match its own key -- not applied", key)
        _fail(key, company_id, doc_id)
        return {"project_doc": key, "outcome": "mismatch"}
    try:
        with get_connection() as conn:
            with conn.transaction():
                outcome = project_docs.apply_result(conn, result)
    except Exception:
        logger.exception("project doc apply failed: %s", key)
        _fail(key, company_id, doc_id)
        return {"project_doc": key, "outcome": "error"}
    logger.info("project doc apply %s outcome=%s chunks=%d", key, outcome,
                len(result.get("chunks") or []))
    return {"project_doc": key, "outcome": outcome}
