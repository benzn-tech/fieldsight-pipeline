# tests/unit/test_template_pgdatabase.py
import re
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "src" / "template.yaml"


def _text():
    return TEMPLATE.read_text(encoding="utf-8")


def test_pgdatabase_param_and_condition_declared():
    t = _text()
    assert re.search(r"^\s{2}PgDatabase:\s*$", t, re.M), "PgDatabase parameter missing"
    assert "HasPgDatabaseOverride:" in t, "HasPgDatabaseOverride condition missing"
    assert '!Not [!Equals [!Ref PgDatabase, ""]]' in t or \
           "!Not [!Equals [!Ref PgDatabase, '']]" in t, "condition body wrong"


def test_all_pgdatabase_values_are_guarded_by_the_condition():
    t = _text()
    # Every PGDATABASE must now be an !If over the override; none may remain a
    # bare !ImportValue (that would be an un-switched function).
    guarded = len(re.findall(r"PGDATABASE:\s*!If \[HasPgDatabaseOverride", t))
    bare = len(re.findall(r"PGDATABASE:\s*!ImportValue", t))
    # 17 since life-conversation phase 2 added the in-VPC NonWorkExpiryFunction
    # (16 with the device ledger's DeviceLedgerFunction; 15 when the chunk-driven
    # session lifecycle added SessionActivityFunction; 14 when the Tier-0 finalize
    # plan added FinalizeSweepFunction; 13 before the video-keyframe
    # KeyframeFunction). Its PGDATABASE is correctly !If-guarded like the rest.
    assert guarded == 17, f"expected 17 guarded PGDATABASE, found {guarded}"
    assert bare == 0, f"found {bare} un-switched bare PGDATABASE !ImportValue"
