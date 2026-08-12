"""The agent-turn filter is only as good as its grants, and a missing one fails silently.

Two shapes this repo has already shipped:

- **403 masquerading as 404.** S3 answers 403 for a key that does not exist unless the caller
  holds `ListBucket` for that prefix. A reader without it sees "no sidecar" and filters nothing,
  while another reader that does hold it filters normally.
- **A grant added for the code that needs it, on one function only.** `ExtractSessionFunction`
  gained `GetObject` on `extractions/*` without the matching `ListBucket` and every first live
  pass silently stood down.

Here the second shape is worse than silent. `embed_from_sidecar` looks vectors up by
`sha256(chunk_text)`, and embed-report and ingest each rebuild that text. If one filters and the
other does not, every transcript-window hash misses and the **whole report fails to ingest**.
So the grants are asserted, not reviewed.
"""
import pathlib
import re

TEMPLATE = pathlib.Path(__file__).resolve().parents[2] / "src" / "template.yaml"

# Every function whose code path reaches agent_turn_filter, directly or through a shared loader.
READERS = [
    "ExtractSessionFunction",    # assemble_session_turns
    "RollingSummaryFunction",    # assemble_deduped_turns
    "SessionFinalizeFunction",   # assemble_deduped_turns
    "IngestFunction",            # _load_turns
    "EmbedReportFunction",       # calls _load_turns
]


def _list_prefixes(block):
    """The prefixes under a ListBucket condition.

    Comment lines are skipped rather than terminating the list: a YAML sequence is not broken by
    a `#` line, and a parser that stops at one reports a grant as missing when it is present --
    which is how the first version of this test failed against a correct template.
    """
    lines = block.split("\n")
    for i, line in enumerate(lines):
        if line.strip() != "s3:prefix:":
            continue
        prefixes = []
        for nxt in lines[i + 1:]:
            stripped = nxt.strip()
            if stripped.startswith("#"):
                continue          # a comment does not end a YAML sequence
            if stripped.startswith("- "):
                prefixes.append(stripped[2:].strip())
                continue
            break
        return prefixes
    return None


def _blocks():
    text = TEMPLATE.read_text(encoding="utf-8")
    starts = [(m.group(1), m.start()) for m in re.finditer(r"^  ([A-Za-z0-9]+Function):", text, re.M)]
    out = {}
    for i, (name, pos) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(text)
        out[name] = text[pos:end]
    return out


def test_every_reader_can_get_the_sidecar_objects():
    blocks = _blocks()
    for fn in READERS:
        assert "/voice_ask/*" in blocks[fn], f"{fn} cannot read the agent-answer sidecars"


def test_every_reader_can_list_the_sidecar_prefix():
    """Without ListBucket, a missing sidecar answers 403 and reads as a denial -- which the
    loader cannot tell from 'this user never asked anything today'."""
    blocks = _blocks()
    for fn in READERS:
        prefixes = _list_prefixes(blocks[fn])
        assert prefixes, f"{fn} has no ListBucket prefix condition at all"
        assert "voice_ask/*" in prefixes, f"{fn} cannot LIST voice_ask/ (has {prefixes})"


def test_the_writer_can_write_and_knows_the_bucket():
    """voice-audit is the only function that can write these: it is in-VPC, so it can resolve
    caller_sub -> folder_name, which the non-VPC ask lambda cannot."""
    block = _blocks()["VoiceAuditFunction"]
    assert "s3:PutObject" in block, "voice-audit cannot write the sidecar"
    assert "/voice_ask/*" in block
    assert "S3_BUCKET" in block, "voice-audit has no bucket name, so it writes nowhere"


def test_ingest_and_embed_report_have_identical_sidecar_access():
    """The pair that must never disagree. They rebuild chunk_text independently and the hash is
    the contract between them; asymmetric access is a failed report, not a smaller filter."""
    blocks = _blocks()
    def grants(fn):
        b = blocks[fn]
        return ("/voice_ask/*" in b, "voice_ask/*" in (_list_prefixes(b) or []))
    assert grants("IngestFunction") == grants("EmbedReportFunction")
