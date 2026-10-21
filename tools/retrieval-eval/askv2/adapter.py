"""Data API read-only connection adapter (spec §2.2, review B3).

The repositories on the Ask path take a psycopg connection. This class gives them the
same surface over the RDS Data API, typed like psycopg's results (`str(uuid)`,
`date.isoformat()` are called on them), inside ONE read-only transaction per fake
rag-search invoke.
"""
import atexit
import hashlib
import json
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal

from askv2.render import EvalAdapterError, check_template, render

_INTS = {"int2", "int4", "int8", "serial", "bigserial", "oid"}
_FLOATS = {"float4", "float8"}
_TEXT = {"text", "varchar", "bpchar", "char", "name", "citext"}


def _scalar(type_name, field, label, warnings):
    if field.get("isNull"):
        return None
    if "arrayValue" in field:
        arr = field["arrayValue"]
        values = next((v for k, v in arr.items() if k.endswith("Values")), [])
        return [_scalar(type_name.lstrip("_"), _wrap(v), label, warnings) for v in values]
    if "longValue" in field:
        return int(field["longValue"])
    if "doubleValue" in field:
        return float(field["doubleValue"])
    if "booleanValue" in field:
        return bool(field["booleanValue"])
    raw = field.get("stringValue")
    if raw is None:
        warnings.append("unhandled field shape %s (column %s)" % (sorted(field), label))
        return field.get("blobValue")
    t = type_name.lower()
    if t == "uuid":
        return uuid.UUID(raw)
    if t == "date":
        return date.fromisoformat(raw)
    if t in ("timestamp", "timestamptz"):
        dt = datetime.fromisoformat(raw)
        # The Data API hands timestamptz back in UTC without an offset; psycopg returns
        # it aware.
        return dt.replace(tzinfo=timezone.utc) if t == "timestamptz" and dt.tzinfo is None else dt
    if t in ("json", "jsonb"):
        return json.loads(raw)
    if t == "numeric":
        return Decimal(raw)
    if t in _FLOATS:
        return float(raw)
    if t in _INTS:
        return int(raw)
    if t == "bool":
        return raw.lower() in ("t", "true")
    if t in _TEXT:
        return raw
    warnings.append("unknown typeName %s (column %s)" % (type_name, label))
    return raw


def _wrap(v):
    """An arrayValue element as the field shape `_scalar` reads."""
    if v is None:
        return {"isNull": True}
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"longValue": v}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, dict):
        return {"arrayValue": v}
    return {"stringValue": v}


def coerce_rows(column_metadata, records, *, as_dict, warnings):
    """Data API `records` -> psycopg-like rows: dicts keyed by column label for
    `dict_row`, tuples otherwise. Unknown type names come back raw, with a warning."""
    labels = [c.get("label") or c.get("name") for c in column_metadata]
    types = [c.get("typeName") or "" for c in column_metadata]
    out = []
    for rec in records:
        vals = [_scalar(types[i], f, labels[i], warnings) for i, f in enumerate(rec)]
        out.append(dict(zip(labels, vals)) if as_dict else tuple(vals))
    return out


# --- the connection ----------------------------------------------------------

# Open transactions, so a KeyboardInterrupt between questions still rolls back
# (review B3.4). The Data API also rolls back a transaction idle for 3 minutes.
_OPEN = {}
_OPEN_LOCK = threading.Lock()


def rollback_open():
    """Roll back every transaction this process still holds. Idempotent."""
    with _OPEN_LOCK:
        pending = list(_OPEN.values())
        _OPEN.clear()
    for conn in pending:
        conn.close()


atexit.register(rollback_open)


def _captures_evidence(rows):
    """The `sql` stage (review S1): raw rows, before alias normalisation and any
    dispatcher filter, of every result that carries dated record text."""
    return bool(rows) and "report_date" in rows[0] and (
        "chunk_text" in rows[0] or "summary" in rows[0])


class AdapterLog:
    """What every adapter of one turn saw: per-statement records, errors, sql-stage rows."""

    def __init__(self):
        self.statements = []      # {sha256, head, ms, rows, bytes}
        self.errors = []          # "<ExcType>: <message head>"
        self.warnings = []
        self.sql_rows = []        # JSON-plain dict rows
        self.largest_response = 0
        self.transactions = 0


class DataApiCursor:
    def __init__(self, conn, as_dict):
        self._conn, self._as_dict = conn, as_dict
        self._rows, self.rowcount = [], -1

    def execute(self, sql, params=None):
        self._rows = self._conn._run(sql, params, self._as_dict)
        self.rowcount = len(self._rows)
        return self

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def __iter__(self):
        while self._rows:
            yield self._rows.pop(0)

    def close(self):
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class DataApiConnection:
    """psycopg-shaped connection over one READ ONLY Data API transaction.

    Opened: begin_transaction -> SET TRANSACTION READ ONLY -> SHOW must say `on`
    (review B3.1). Closed: rollback_transaction. There is no other way out."""

    def __init__(self, client, *, cluster, secret, database, log):
        self._client, self._log = client, log
        self._target = {"resourceArn": cluster, "secretArn": secret, "database": database}
        self._sp = 0
        self.closed = False
        self._tx = client.begin_transaction(**self._target)["transactionId"]
        log.transactions += 1
        with _OPEN_LOCK:
            _OPEN[self._tx] = self
        try:
            self._internal("SET TRANSACTION READ ONLY")
            show = self._internal("SHOW transaction_read_only")
            value = show["records"][0][0].get("stringValue") if show.get("records") else None
            if value != "on":
                raise EvalAdapterError("transaction is not read-only (SHOW said %r)" % value)
        except BaseException:
            self.close()
            raise

    # -- the only path for adapter-internal statements; templates never reach it.
    def _internal(self, sql):
        return self._client.execute_statement(sql=sql, transactionId=self._tx,
                                              includeResultMetadata=True, **self._target)

    def _run(self, template, params, as_dict):
        if self.closed:
            raise EvalAdapterError("connection closed")
        try:
            check_template(template)
            sql = render(template, params)
            t0 = time.perf_counter()
            resp = self._client.execute_statement(sql=sql, transactionId=self._tx,
                                                  includeResultMetadata=True, **self._target)
            ms = (time.perf_counter() - t0) * 1000
            rows = coerce_rows(resp.get("columnMetadata") or [], resp.get("records") or [],
                               as_dict=as_dict, warnings=self._log.warnings)
        except BaseException as exc:
            self._log.errors.append("%s: %s" % (type(exc).__name__, str(exc)[:300]))
            raise
        size = len(json.dumps(resp, default=str))
        self._log.largest_response = max(self._log.largest_response, size)
        self._log.statements.append({
            "sha256": hashlib.sha256(template.encode("utf-8")).hexdigest(),
            "head": " ".join(template.split())[:80], "ms": round(ms, 1),
            "rows": len(rows), "bytes": size})
        if as_dict and _captures_evidence(rows):
            self._log.sql_rows.extend(json.loads(json.dumps(rows, default=str)))
        return rows

    def cursor(self, row_factory=None):
        if row_factory is None:
            return DataApiCursor(self, as_dict=False)
        if getattr(row_factory, "__name__", "") == "dict_row":
            return DataApiCursor(self, as_dict=True)
        raise EvalAdapterError("unsupported row_factory %r" % row_factory)

    def execute(self, sql, params=None):
        return self.cursor().execute(sql, params)

    @contextmanager
    def transaction(self):
        """A SAVEPOINT, as psycopg's transaction() is inside an open transaction:
        the dispatcher's per-call isolation behaves as in prod."""
        self._sp += 1
        name = "eval_%d" % self._sp
        self._internal("SAVEPOINT " + name)
        try:
            yield self
        except BaseException:
            self._internal("ROLLBACK TO SAVEPOINT " + name)
            raise
        self._internal("RELEASE SAVEPOINT " + name)

    def close(self):
        if self.closed:
            return
        self.closed = True
        with _OPEN_LOCK:
            _OPEN.pop(self._tx, None)
        self._client.rollback_transaction(transactionId=self._tx, **{
            k: v for k, v in self._target.items() if k != "database"})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
