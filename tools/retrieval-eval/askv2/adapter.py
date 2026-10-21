"""Data API read-only connection adapter (spec §2.2, review B3).

The repositories on the Ask path take a psycopg connection. This class gives them the
same surface over the RDS Data API, typed like psycopg's results (`str(uuid)`,
`date.isoformat()` are called on them), inside ONE read-only transaction per fake
rag-search invoke.
"""
import json
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

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
