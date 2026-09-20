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


# ------------------------------------------ the prompt builders, driven

def _daily(summary):
    return {"report_date": "2026-09-02", "user_name": "Ben_UCPK2",
            "executive_summary": summary, "topics": []}


def test_the_weekly_prompt_reads_bullets_as_prose_not_as_a_python_list():
    """The old expression put `['First point', 'Second point']` -- brackets,
    quotes and all -- into the text handed to the model. It is legible enough
    that nothing ever failed, which is why it survived six months."""
    prompt = rg.build_weekly_prompt(
        [_daily(["First point", "Second point"])],
        "SB1108", "2026-08-31", "2026-09-06")
    assert "First point Second point" in prompt
    assert "['First point'" not in prompt


def test_the_monthly_prompt_reads_bullets_the_same_way_from_either_source():
    """Two branches, weekly-derived and daily-derived, and both interpolated
    the list."""
    from_weekly = rg.build_monthly_prompt(
        [], [{"period": {"start": "2026-08-31", "end": "2026-09-06"},
              "executive_summary": ["Week point one", "Week point two"]}],
        "SB1108", "2026-08-01", "2026-08-31")
    assert "Week point one Week point two" in from_weekly
    assert "['Week point one'" not in from_weekly

    from_daily = rg.build_monthly_prompt(
        [_daily(["Day point one", "Day point two"])], [],
        "SB1108", "2026-08-01", "2026-08-31")
    assert "Day point one Day point two" in from_daily
    assert "['Day point one'" not in from_daily


# ------------- 2026-09-20: the customer-facing footer must not name the model

def test_the_daily_report_word_footer_does_not_name_the_model():
    """`_finish_document`'s footer is the customer-facing artifact -- the Word
    file a customer opens. It must not say which LLM vendor/model wrote the
    report, even though the JSON `_report_metadata` this footer reads from
    still carries `model` (that field is the internal record, kept
    deliberately -- see test_the_debug_record_names_the_model_that_ran and
    test_report_metadata_json_still_carries_the_model below)."""
    docx = pytest.importorskip("docx", reason="python-docx ships as a Lambda layer")
    report_data = {
        "executive_summary": ["A day happened."],
        "_report_metadata": {
            "generated_at": "2026-09-20T00:00:00Z",
            "model": "meta/muse-spark-1.3-contributor",
            "recordings_processed": 4,
            "version": "v3.5",
        },
    }
    buf = rg.generate_word_document(report_data, "T")
    assert buf is not None
    buf.seek(0)
    full_text = "\n".join(p.text for p in docx.Document(buf).paragraphs)
    assert "meta/muse-spark-1.3-contributor" not in full_text
    assert "Model" not in full_text
    # The rest of the footer is unchanged -- this is a redaction, not a deletion.
    assert "Recordings: 4" in full_text
    assert "Version: v3.5" in full_text


def test_the_meeting_minutes_word_footer_does_not_name_the_model():
    """The second copy of the footer (lambda_meeting_minutes.generate_word_document,
    also used by lambda_session_report's T3 render). Same rule, same reason the
    two copies existed in the first place: miss one and it drifts."""
    docx = pytest.importorskip("docx", reason="python-docx ships as a Lambda layer")
    minutes_data = {
        "_report_metadata": {
            "generated_at": "2026-09-20T00:00:00Z",
            "model": "meta/muse-spark-1.3-contributor",
            "recordings_processed": 2,
            "version": "v1.1",
        },
    }
    buf = mm.generate_word_document(minutes_data, "T")
    assert buf is not None
    buf.seek(0)
    full_text = "\n".join(p.text for p in docx.Document(buf).paragraphs)
    assert "meta/muse-spark-1.3-contributor" not in full_text
    assert "Model" not in full_text
    assert "Recordings: 2" in full_text
    assert "Version: v1.1" in full_text


def test_report_metadata_json_still_carries_the_model():
    """Pinning the OTHER half of the requirement: the footer is redacted, but
    the JSON `_report_metadata` block the footer is built from -- the internal
    record, written to S3 alongside the debug record -- must still carry
    `model`. A later "cleanup" that notices the footer never uses it and
    deletes the field would quietly remove the only trace of which model
    wrote a bad report."""
    report_data = {
        "executive_summary": [],
        "_report_metadata": {"model": "meta/muse-spark-1.3-contributor"},
    }
    # The footer function reads report_data['_report_metadata'] but never
    # mutates it -- confirmed by calling it and re-reading the same dict.
    docx = pytest.importorskip("docx", reason="python-docx ships as a Lambda layer")
    doc = docx.Document()
    rg._finish_document(doc, report_data)
    assert report_data["_report_metadata"]["model"] == "meta/muse-spark-1.3-contributor"


# --------------------------------------- an empty array is not a missing one

def test_an_empty_bullet_array_still_says_something_under_the_heading():
    """`executive_summary: []` is what a failed extraction looks like. Rendering
    the heading over blank space tells the reader nothing went wrong."""
    docx = pytest.importorskip("docx", reason="python-docx ships as a Lambda layer")
    buf = rg.generate_word_document({"executive_summary": []}, "T")
    buf.seek(0)
    texts = [p.text for p in docx.Document(buf).paragraphs]
    assert "No summary available" in texts, texts


# ------------------------------------- every label, in every spelling

@pytest.mark.parametrize("path", [
    "src/lambda_report_generator.py",
    "src/lambda_meeting_minutes.py",
])
def test_the_internal_metadata_model_label_is_the_active_model_or_nothing(path):
    """report_generator/meeting_minutes still stamp `_report_metadata.model` --
    an internal provenance field, kept in the debug record and the stored S3
    JSON so a bad answer can still be traced to the model that wrote it. That
    label must still be the model that actually ran, or None; the previous
    version of this sweep named ONE constant, `CLAUDE_MODEL`, which was wrong
    on a qwen deploy. State the property instead of the forbidden spellings --
    there is always another constant.

    `lambda_ask_agent.py` is deliberately NOT in this list any more: as of
    2026-09-20 its response is customer-facing and must carry no `model`
    label at all (see test_no_ask_response_names_a_model_at_all in
    test_ask_reports_the_model_that_answered.py) -- not even "None".
    """
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[2]
    source = (root / path).read_text(encoding="utf-8")
    labels = re.findall(r"""['"]model['"]\s*:\s*([^,\n]+)""", source)
    assert labels, "no model labels found -- the pattern stopped matching"
    for value in labels:
        assert value.strip() in ("None", "llm_utils.active_model(),", "llm_utils.active_model()"), (
            "%s labels an answer with %r; use llm_utils.active_model()" % (path, value.strip())
        )
