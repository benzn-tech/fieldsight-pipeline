# src/reindex.py
"""Per-topic RAG re-index (spec §5.3, D6). Split across the VPC boundary:
  - enqueue_topic_reindex  (in-VPC, org-api): reads the CORRECTED topic from
    Aurora, renders + chunks its 'topic' chunk texts, loads active aliases,
    and writes a request artifact to S3. It NEVER calls DashScope (BUG-36).
  - lambda_embed_report    (non-VPC): S3-event on the request -> embeds the
    topic chunks + this topic's alias-normalized transcript windows, writes a
    vectors result artifact. (Transcript S3 object is read, never written -- D4.)
  - apply_vectors          (in-VPC, ingest): S3-event on the vectors result ->
    delete_chunks_for_topic + insert_chunk with the durable topic_id.

The request/vectors handoff mirrors the existing embed->ingest sidecar
contract, just triggered by an edit instead of the nightly report and sourced
from corrected Aurora rows instead of daily_report.json."""
import json

from psycopg.rows import dict_row

from repositories import aliases, chunks, redactions, topics
from chunking import chunk_report

REQUEST_PREFIX = "reindex_requests/"
# Vectors go under a SEPARATE top-level prefix (not reindex_requests/*.vectors
# .json): S3 rejects two bucket-notification rules that share a prefix with
# overlapping suffixes ("Configuration is ambiguously defined") — a
# .vectors.json object matches both the embed rule (reindex_requests/ + .json)
# and the ingest rule. Distinct prefixes keep the embed (request) and ingest
# (vectors) triggers unambiguous, and the embed's own output never re-triggers
# it.
VECTORS_PREFIX = "reindex_vectors/"


def request_key(date, folder, topic_id):
    return f"{REQUEST_PREFIX}{date}/{folder}/{topic_id}.json"


def vectors_key(date, folder, topic_id):
    return f"{VECTORS_PREFIX}{date}/{folder}/{topic_id}.json"


def _company_id_for_site(conn, site_id):
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT company_id FROM sites WHERE id=%s", (site_id,)).fetchone()
    return row["company_id"] if row else None


def enqueue_topic_reindex(s3_client, bucket, conn, topic_id, folder, date):
    """Build + write the reindex request for one corrected topic. Returns the
    S3 key, or None if the topic vanished (a concurrent supersession) -- the
    caller treats None as 'nothing to re-index', never an error (spec §6:
    re-index never rolls back the edit)."""
    # Imported here (not at module top) to avoid a circular import: reindex is
    # imported by lambda_org_api, which defines render_report_shape.
    from lambda_org_api import render_report_shape

    t = topics.get_topic_full(conn, topic_id)
    if t is None:
        return None

    # Life-conversation separation (spec §6): a redacted or non_work topic is
    # removed from RAG. Write a DELETE-ONLY request (no topic_chunks) so
    # apply_vectors deletes its existing vectors and inserts nothing.
    if t.get("work_class") == "non_work" or redactions.is_topic_redacted(conn, topic_id):
        key = request_key(date, folder, topic_id)
        s3_client.put_object(Bucket=bucket, Key=key, ContentType="application/json",
            Body=json.dumps({
                "topic_id": str(topic_id), "site_id": str(t["site_id"]),
                "user_id": str(t["user_id"]) if t.get("user_id") is not None else None,
                "report_date": str(t["report_date"]),
                "source_s3_key": t["source_s3_key"], "report_key": None, "topic_seq": None,
                "folder": folder, "date": date, "aliases": [], "topic_chunks": [],
                "delete_only": True,
            }))
        return key

    site_id = str(t["site_id"])
    company_id = _company_id_for_site(conn, t["site_id"])
    active = aliases.list_active(conn, company_id, site_ids=[site_id]) if company_id else []
    alias_pairs = [{"wrong_term": a["wrong_term"], "right_term": a["right_term"]}
                   for a in active]

    shaped = render_report_shape([t], None, date, folder, conn=conn)
    topic_chunks = chunk_report(shaped)             # chunk_type='topic' only

    request = {
        "topic_id": str(topic_id),
        "site_id": site_id,
        "user_id": str(t["user_id"]) if t.get("user_id") is not None else None,
        "report_date": str(t["report_date"]),
        "source_s3_key": t["source_s3_key"],
        # report_key + topic_seq let the non-VPC embed lambda rebuild THIS
        # topic's transcript windows from the immutable daily_report.json.
        "report_key": t["source_s3_key"] if str(t["source_s3_key"]).startswith("reports/") else None,
        "topic_seq": shaped["topics"][0]["topic_id"] if shaped["topics"] else None,
        "folder": folder,
        "date": date,
        "aliases": alias_pairs,
        "topic_chunks": [{"chunk_type": c["chunk_type"], "chunk_text": c["chunk_text"],
                          "metadata": c["metadata"]} for c in topic_chunks],
    }
    key = request_key(date, folder, topic_id)
    s3_client.put_object(Bucket=bucket, Key=key,
                         Body=json.dumps(request), ContentType="application/json")
    return key


def apply_vectors(conn, result) -> int:
    """In-VPC: replace the topic's chunks with the freshly-embedded ones
    (D6 delete-and-replace). result['chunks'][*] each carry chunk_type,
    chunk_text, metadata, embedding (1024 floats)."""
    topic_id = result["topic_id"]
    chunks.delete_chunks_for_topic(conn, topic_id)
    n = 0
    for c in result.get("chunks", []):
        chunks.insert_chunk(
            conn, result["site_id"], result["report_date"],
            c["chunk_type"], c["chunk_text"], c["embedding"],
            user_id=result.get("user_id"),
            source_s3_key=result.get("source_s3_key"),
            topic_id=topic_id, metadata=c.get("metadata") or {},
        )
        n += 1
    return n
