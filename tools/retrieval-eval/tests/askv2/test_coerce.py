import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from askv2.adapter import coerce_rows

U = "4326dbc7-14a2-4251-8381-9d0067595a85"


def col(label, type_name):
    return {"label": label, "name": label, "typeName": type_name}


META = [col("id", "uuid"), col("report_date", "date"), col("metadata", "jsonb"),
        col("distance", "float8"), col("created_at", "timestamptz"), col("n", "int8"),
        col("score", "numeric"), col("lexical_hit", "bool"), col("chunk_text", "text"),
        col("ids", "_uuid"), col("tags", "_text"), col("ts", "timestamp")]
REC = [{"stringValue": U}, {"stringValue": "2026-09-02"}, {"stringValue": '{"window_span": "09:00"}'},
       {"isNull": True}, {"stringValue": "2026-09-02 01:02:03.12345"}, {"longValue": 7},
       {"stringValue": "1.50"}, {"booleanValue": True}, {"stringValue": "pour"},
       {"arrayValue": {"stringValues": [U]}}, {"arrayValue": {"stringValues": ["a", "b"]}},
       {"stringValue": "2026-09-02 01:02:03"}]


def test_typed_dict_rows():
    warnings = []
    rows = coerce_rows(META, [REC], as_dict=True, warnings=warnings)
    r = rows[0]
    assert r["id"] == uuid.UUID(U)
    assert r["report_date"] == date(2026, 9, 2)
    assert r["metadata"] == {"window_span": "09:00"}
    assert r["distance"] is None
    assert r["created_at"] == datetime(2026, 9, 2, 1, 2, 3, 123450, tzinfo=timezone.utc)
    assert r["n"] == 7 and r["score"] == Decimal("1.50") and r["lexical_hit"] is True
    assert r["chunk_text"] == "pour"
    assert r["ids"] == [uuid.UUID(U)] and r["tags"] == ["a", "b"]
    assert r["ts"] == datetime(2026, 9, 2, 1, 2, 3) and r["ts"].tzinfo is None
    assert warnings == []


def test_tuple_rows_for_an_unlabelled_aggregate():
    rows = coerce_rows([col("max", "date")], [[{"stringValue": "2026-09-10"}]], as_dict=False,
                       warnings=[])
    assert rows == [(date(2026, 9, 10),)]
    assert rows[0][0].isoformat() == "2026-09-10"


def test_floats_from_double_and_from_string():
    rows = coerce_rows([col("a", "float8"), col("b", "float4")],
                       [[{"doubleValue": 0.25}, {"stringValue": "0.5"}]], as_dict=False,
                       warnings=[])
    assert rows == [(0.25, 0.5)]


def test_unknown_type_is_raw_with_a_warning():
    warnings = []
    rows = coerce_rows([col("role", "membership_role")], [[{"stringValue": "pm"}]],
                       as_dict=True, warnings=warnings)
    assert rows == [{"role": "pm"}]
    assert warnings == ["unknown typeName membership_role (column role)"]
