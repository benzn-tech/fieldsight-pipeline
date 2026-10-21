"""psycopg template -> SQL literal for the Data API, and the statement allow/deny list.

The Data API has no array parameters, so values are rendered client-side (spec §2.2).
A list of strings becomes an UNTYPED '{...}' literal, so Postgres infers the array type
from context exactly as it does for psycopg's unknown-typed dumps; a float vector
becomes '[...]' in run.py's %.7f (review S3: repr() would push build_search_sql past the
Data API's 65,536-char cap). Proof of fidelity is the local-Postgres parity test
(tests/askv2/test_render_parity_pg.py), not this docstring.

Every template is checked before it is sent (review B3): first keyword SELECT/WITH and
no write/lock/session token outside literals and comments. The read-only transaction is
the real guarantee; this is the second wall.
"""
import json
import math
import re
import uuid
from datetime import date, datetime
from decimal import Decimal

MAX_SQL_CHARS = 60_000           # Data API caps `sql` at 65,536
MIN_VECTOR_LEN = 64              # a float list shorter than this is not an embedding

_PLACEHOLDER = re.compile(r"%%|%(?:\((\w+)\))?([sbt])")


class EvalAdapterError(Exception):
    """The eval seam refused or failed. Never a retrieval miss: the turn is invalid."""


def _quote(text):
    if "\x00" in text:
        raise EvalAdapterError("NUL in a string value")
    return "'" + text.replace("'", "''") + "'"


def _array_elem(v):
    if v is None:
        return "NULL"
    if isinstance(v, uuid.UUID):
        v = str(v)
    if not isinstance(v, str):
        raise EvalAdapterError("array element of type %s" % type(v).__name__)
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _literal(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            raise EvalAdapterError("non-finite float")
        return repr(v)
    if isinstance(v, Decimal):
        if not v.is_finite():
            raise EvalAdapterError("non-finite decimal")
        return str(v)
    if isinstance(v, str):
        return _quote(v)
    if isinstance(v, uuid.UUID):
        return _quote(str(v))
    if isinstance(v, (date, datetime)):           # datetime is a date subclass
        return _quote(v.isoformat())
    if isinstance(v, dict):
        return _quote(json.dumps(v, default=str))
    if isinstance(v, list):
        if v and all(isinstance(x, float) for x in v):
            if len(v) < MIN_VECTOR_LEN:
                raise EvalAdapterError("short float list (%d): not an embedding" % len(v))
            if not all(math.isfinite(x) for x in v):
                raise EvalAdapterError("non-finite float in vector")
            return _quote("[" + ",".join("%.7f" % x for x in v) + "]")
        return _quote("{" + ",".join(_array_elem(x) for x in v) + "}")
    # set (psycopg does not adapt it either: review N2), tuple, bytes, anything else.
    raise EvalAdapterError("cannot render a %s parameter" % type(v).__name__)


def render(sql, params):
    """`sql` with psycopg placeholders replaced by literals. No params -> unchanged
    (psycopg leaves `%%` alone then too)."""
    if params is None:
        return sql
    named = isinstance(params, dict)
    if not named and not isinstance(params, (list, tuple)):
        raise EvalAdapterError("params must be a dict, list or tuple")
    pos = iter(params) if not named else None
    used = [0]

    def sub(m):
        if m.group(0) == "%%":
            return "%"
        name = m.group(1)
        if named:
            if name is None or name not in params:
                raise EvalAdapterError("placeholder %r not in params" % (name or "%s"))
            return _literal(params[name])
        if name is not None:
            raise EvalAdapterError("named placeholder with positional params")
        try:
            v = next(pos)
        except StopIteration:
            raise EvalAdapterError("more placeholders than params") from None
        used[0] += 1
        return _literal(v)

    out = _PLACEHOLDER.sub(sub, sql)
    if not named and used[0] != len(params):
        raise EvalAdapterError("more params than placeholders")
    if len(out) >= MAX_SQL_CHARS:
        raise EvalAdapterError("statement of %d chars is over the %d cap"
                               % (len(out), MAX_SQL_CHARS))
    return out


# --- allow/deny ------------------------------------------------------------

_LITERALS = re.compile(
    r"'(?:[^']|'')*'"                 # string literal
    r"|\"(?:[^\"]|\"\")*\""           # quoted identifier
    r"|\$(\w*)\$.*?\$\1\$"            # dollar-quoted body
    r"|--[^\n]*"                      # line comment
    r"|/\*.*?\*/", re.S)              # block comment
_FIRST = re.compile(r"^[\s(]*(\w+)")
_DENY = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|COPY|CALL|DO|"
    r"LOCK|SET|LISTEN|NOTIFY|VACUUM|ANALYZE|REFRESH|PREPARE|EXECUTE)\b"
    r"|\bFOR\s+(?:NO\s+KEY\s+)?(?:UPDATE|SHARE|KEY\s+SHARE)\b"
    r"|\b(?:nextval|setval|pg_advisory\w*|pg_try_advisory\w*|pg_terminate_backend|"
    r"pg_cancel_backend|pg_sleep\w*|set_config|dblink\w*|lo_\w+|pg_read\w*|pg_write\w*)\b"
    r"|;", re.I)


def check_template(sql):
    """Refuse anything but one read-only SELECT/WITH statement (review B3.3)."""
    bare = _LITERALS.sub(" ", sql)
    first = _FIRST.match(bare)
    if not first or first.group(1).upper() not in ("SELECT", "WITH"):
        raise EvalAdapterError("statement does not start with SELECT/WITH")
    hit = _DENY.search(bare.rstrip().rstrip(";"))
    if hit:
        raise EvalAdapterError("denied token %r" % hit.group(0))
