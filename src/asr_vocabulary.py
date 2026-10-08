"""The platform's ASR keyterms, kept as data so they can be watched and changed.

The list used to live only in config/custom_vocabulary_construction_nz.txt, so
every word added or taken out was a PR and a deploy, and nothing recorded WHY a
word was there -- which recording lost it, what it was heard as. The owner
(2026-10-08) asked for a table that collects them, to keep an eye on and update.

One DynamoDB row per term, keyed by the term exactly as it is sent:

    term          "Hirepool"
    status        "active"     sent to the ASR on every request
                  "candidate"  recorded (heard wrong somewhere), NOT sent yet
                  "retired"    never sent -- also removes the same word from the file
    misheard_as   ["Hiab", "hair pool"]
    evidence      [{"date", "folder", "session", "heard"}]
    category, note, added_by, added_at, updated_at

The file stays: it is the seed and the fallback. A table that cannot be read
costs the table's additions for that request, never the transcription.
"""
import logging
import time

import elevenlabs_utils

logger = logging.getLogger(__name__)

STATUSES = ("active", "candidate", "retired")
CACHE_SECONDS = 300          # a container re-reads the table at most every 5 minutes

_cache = {"at": None, "rows": None}


def read_rows(table_name, dynamodb=None):
    """Every row of the vocabulary table (it is small; a scan is the whole read)."""
    import boto3
    table = (dynamodb or boto3.resource("dynamodb")).Table(table_name)
    rows, kwargs = [], {}
    while True:
        page = table.scan(**kwargs)
        rows.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return rows
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def merge(file_terms, rows):
    """The file's terms, then the table's active ones; a retired row removes
    the word wherever it came from. Case-insensitive, first spelling wins,
    capped to scribe_v2's limits."""
    rows = [r for r in rows or [] if isinstance(r, dict) and str(r.get("term") or "").strip()]
    retired = {str(r["term"]).strip().lower() for r in rows if r.get("status") == "retired"}
    active = sorted(str(r["term"]).strip() for r in rows if r.get("status") == "active")
    out, seen = [], set()
    for term in list(file_terms or []) + active:
        key = term.lower()
        if key in seen or key in retired:
            continue
        seen.add(key)
        out.append(term[:elevenlabs_utils.MAX_KEYTERM_LEN])
    return out[:elevenlabs_utils.MAX_KEYTERMS]


def keyterms(vocab_path, table_name=None, *, now=None, read=None):
    """What to send the ASR: the file merged with the table.

    No table configured -> the file alone, exactly as before. The table is
    cached per container; when a re-read fails the last good read is kept, and
    with none the file is used alone -- logged, because a table nobody can read
    looks exactly like a table that changed nothing."""
    file_terms = elevenlabs_utils.load_keyterms(vocab_path)
    if not table_name:
        return file_terms
    now = time.time() if now is None else now
    if _cache["at"] is None or now - _cache["at"] >= CACHE_SECONDS:
        try:
            _cache["rows"] = (read or read_rows)(table_name)
        except Exception as e:  # noqa: BLE001 -- the vocabulary must never stop a transcription
            logger.warning("asr vocabulary: table %s unreadable (%s); using %s", table_name, e,
                           "the last good read" if _cache["rows"] is not None else "the file only")
        _cache["at"] = now
    return merge(file_terms, _cache["rows"] or [])
