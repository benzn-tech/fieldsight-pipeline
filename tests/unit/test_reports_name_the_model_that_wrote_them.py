"""A report must name the model that wrote it, and its schema must not leak.

Two defects, both surfaced by the move to a new chat vendor on 2026-09-07 and
both invisible before it.

1. `_report_metadata.model`. The same bug was found and fixed on the Ask path
   (`test_ask_reports_the_model_that_answered.py`) and the sweep stopped there:
   report-generator kept six `CLAUDE_MODEL` sites and meeting-minutes two. On
   TEST, a report demonstrably written by `meta/muse-spark-1.3-contributor`
   -- the log line reads `qwen call: model=meta/muse-spark-1.3-contributor` --
   was stamped `claude-sonnet-4-6`. That label is what a reader uses to decide
   which model produced a bad report, so a wrong one sends them at the wrong
   model. `llm_utils.active_model()` already answers this question and returns
   None rather than guessing for a provider it does not know.

2. The JSON schema example. `MEETING_MINUTES_SCHEMA` showed the shape with
   `"Bullet 1: Meeting purpose and context"`. Older models read that as a
   placeholder; muse-spark copied the prefix, and TEST minutes came out with
   `"Bullet 1: Morning subcontractor coordination meeting ..."` -- rendered
   verbatim as an `<li>` in the customer's report. A placeholder must not be
   shaped like text that survives being copied.
"""
import json

import pytest

llm_utils = pytest.importorskip("llm_utils")
rg = pytest.importorskip("lambda_report_generator", reason="requires boto3")
mm = pytest.importorskip("lambda_meeting_minutes", reason="requires boto3")


class _CapturingS3:
    """Enough of an S3 client to read back what a writer decided to write."""

    def __init__(self):
        self.puts = []

    def put_object(self, **kw):
        self.puts.append(kw)
        return {}


def _debug_body(client):
    assert client.puts, "nothing was written, so nothing was measured"
    return json.loads(client.puts[-1]["Body"])


# ------------------------------------------- the label follows the provider

@pytest.mark.parametrize("module", [rg, mm], ids=["report_generator", "meeting_minutes"])
def test_the_debug_record_names_the_model_that_ran(module, monkeypatch):
    """Driven, not grepped: call the writer and read what it wrote."""
    monkeypatch.setattr(llm_utils, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(llm_utils, "QWEN_MODEL", "meta/muse-spark-1.3-contributor")
    monkeypatch.setattr(llm_utils, "QWEN_ENABLE_THINKING", True)
    monkeypatch.setattr(llm_utils, "CLAUDE_MODEL", "claude-sonnet-4-6")

    client = _CapturingS3()
    monkeypatch.setattr(module, "s3_client", client)
    module.save_debug_record(
        "bucket", "2026-09-02", "Ben_UCPK2",
        prompt="p", raw_response="{}", parsed_json={}, parse_success=True,
        input_stats={},
    )

    assert _debug_body(client)["model"] == "meta/muse-spark-1.3-contributor"


@pytest.mark.parametrize("module", [rg, mm], ids=["report_generator", "meeting_minutes"])
def test_an_unknown_provider_is_named_nothing_rather_than_guessed(module, monkeypatch):
    """A wrong name is worse than no name: the reader cannot tell it is wrong."""
    monkeypatch.setattr(llm_utils, "LLM_PROVIDER", "something-new")
    monkeypatch.setattr(llm_utils, "CLAUDE_MODEL", "claude-sonnet-4-6")

    client = _CapturingS3()
    monkeypatch.setattr(module, "s3_client", client)
    module.save_debug_record(
        "bucket", "2026-09-02", "Ben_UCPK2",
        prompt="p", raw_response="{}", parsed_json={}, parse_success=True,
        input_stats={},
    )

    assert _debug_body(client)["model"] is None


# ---------------------------------------------------------------- the sweep

@pytest.mark.parametrize("path", [
    "src/lambda_report_generator.py",
    "src/lambda_meeting_minutes.py",
])
def test_no_reporting_site_still_reads_claude_model(path):
    """Wiring, not behaviour -- and that is all this claims. The two functions
    above prove the behaviour at the one site a unit test can drive; the other
    six sit inside `generate_daily_report` / `generate_periodic_report` /
    `generate_meeting_minutes`, which each need S3, a corpus and a model. This
    pins that the sweep reached them, because the last time this bug was fixed
    it was fixed in one file and left standing in two.
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    source = (root / path).read_text(encoding="utf-8")
    assert "'model': CLAUDE_MODEL" not in source
    assert '"model": CLAUDE_MODEL' not in source


# ------------------------------------------------- the schema does not leak

def test_the_schema_placeholder_cannot_survive_being_copied():
    """`"Bullet 1: ..."` is indistinguishable, to the model, from output it is
    being shown. Angle brackets are not: copying them leaves a visible artefact
    a reader reports, instead of a plausible-looking prefix nobody questions.
    """
    schema = json.loads(mm.MEETING_MINUTES_SCHEMA)
    for bullet in schema["executive_summary"]:
        assert not bullet.lower().startswith("bullet"), (
            "a placeholder that reads like a numbered label gets copied verbatim: %r"
            % bullet
        )


# ------------------------------- executive_summary has been an array since v3.0

def test_the_word_export_does_not_run_the_bullets_together():
    """`doc.add_paragraph(list)` does not raise -- python-docx concatenates the
    items with no separator, so five bullets become one unreadable run:
    'Documented Level 2 progress with site photosDocumented Level 3 ...'.

    `config/prompt_templates.json` made `executive_summary` an array at v3.0
    (2026-03-18) and four consumers here were written against the string it used
    to be. Nothing raised, nothing logged, and the Word export has been shipping
    that paragraph ever since.
    """
    assert rg.summary_text({"executive_summary": ["First point", "Second point"]}) == (
        "First point Second point"
    )


def test_a_string_summary_is_left_alone():
    """The in-code fallback schema still emits a string when S3 config is absent
    -- which is exactly the state TEST was in until 2026-09-07."""
    assert rg.summary_text({"executive_summary": "One flowing sentence."}) == (
        "One flowing sentence."
    )


def test_a_missing_summary_says_so_rather_than_rendering_empty():
    assert rg.summary_text({}) == "No summary available"
    assert rg.summary_text({"summary": "legacy field"}) == "legacy field"


def test_the_word_export_keeps_bullets_as_bullets():
    """`summary_text` is the right shape for a PROMPT, where the summary is one
    line of context. A document is not a prompt: meeting-minutes already renders
    an array as `List Bullet` paragraphs, and the daily report now matches it.
    """
    docx = pytest.importorskip("docx", reason="python-docx ships as a Lambda layer")
    buf = rg.generate_word_document(
        {"executive_summary": ["First point", "Second point"]}, "T")
    assert buf is not None, "python-docx is available, so a document was expected"
    buf.seek(0)
    texts = [p.text for p in docx.Document(buf).paragraphs]
    assert "First point" in texts and "Second point" in texts, texts
    assert "First pointSecond point" not in texts
    assert "First point Second point" not in texts
