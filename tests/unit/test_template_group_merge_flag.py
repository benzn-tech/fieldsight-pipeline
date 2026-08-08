# tests/unit/test_template_group_merge_flag.py
"""Phase C ships behind a flag that is OFF on prod.

Text-level assertions, same approach as test_template_org_api_media_iam.py and
test_template_extract_session_iam.py: the template is full of CFN intrinsics
(!Sub/!Ref/!ImportValue) that a plain YAML loader cannot resolve, and the point
here is the literal parameter text.

The Parameter default matters independently of the workflows: both of them pass
an explicit value, so the default is only ever reached by a path that does NOT
— a manual `sam deploy`. That is not hypothetical; it is the exact path
NormaliseAudio was defaulted `false` to protect against."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "src" / "template.yaml"


def test_parameter_defaults_off():
    text = TEMPLATE.read_text(encoding="utf-8")
    m = re.search(r"\n  EnableGroupMerge:\n(?:.*\n)*?\s*Default: '?(\w+)'?\n", text)
    assert m, "no EnableGroupMerge parameter in the template"
    assert m.group(1) == "false", \
        f"EnableGroupMerge defaults to {m.group(1)} — a manual sam deploy would enable it"


def test_prod_workflow_fallback_is_off_and_test_is_on():
    prod = (ROOT / ".github/workflows/deploy-prod.yml").read_text(encoding="utf-8")
    test = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    assert "EnableGroupMerge=${{ vars.PROD_ENABLE_GROUP_MERGE || 'false' }}" in prod, \
        "prod must fall back to false — the merge deletes members' solo topics"
    assert "EnableGroupMerge=${{ vars.TEST_ENABLE_GROUP_MERGE || 'true' }}" in test


def test_the_functions_that_need_the_flag_receive_it():
    # The scan, the merge, the writer, the nightly defer and the email worker all
    # branch on it. A function missing the env var reads the code default
    # ('false') and silently does nothing, which looks identical to the feature
    # being off on purpose.
    text = TEMPLATE.read_text(encoding="utf-8")
    for fn in ("FinalizeSweepFunction", "ExtractSessionFunction", "ItemWriterFunction",
               "IngestFunction", "SessionFinalizeFunction"):
        start = text.index(f"\n  {fn}:\n")
        nxt = re.search(r"\n  [A-Za-z][A-Za-z0-9]*:\n", text[start + 1:])
        block = text[start:start + 1 + nxt.start()] if nxt else text[start:]
        assert "ENABLE_GROUP_MERGE: !Ref EnableGroupMerge" in block, \
            f"{fn} does not receive ENABLE_GROUP_MERGE"


def test_migration_creates_the_group_table_and_both_indexes():
    migs = sorted((ROOT / "src" / "migrations").glob("*_session_group.sql"))
    assert migs, "no session_group migration"
    sql = migs[-1].read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS session_group" in sql
    # The scan reads only unresolved groups, so a resolved group leaves the
    # candidate set for good and the cost does not grow with history.
    assert "idx_session_group_pending" in sql and "WHERE merge_result IS NULL" in sql
    # "which groups was this user in" — the existing partial index is on
    # group_id and answers the opposite question.
    assert "idx_meeting_session_group_user" in sql
    assert "merged_key" in sql, "the merged artifact key must be persisted, never re-derived"
