"""The minutes layout is fixed by design. A template-shaped record needs headings it
chooses itself, so it gets its own renderer rather than more branches inside that one."""
import io
import zipfile

import pytest

import lambda_meeting_minutes as mm

pytestmark = pytest.mark.skipif(not mm.DOCX_AVAILABLE, reason="python-docx not installed")

SECTIONS = [
    {"title": "What this was", "paragraphs": ["Ben's site meeting at Waipuna Rise."]},
    {"title": "Still open", "paragraphs": ["Plumbing RFI 217 is outstanding. (Grounding)"]},
]
ACTIONS = [
    {"action": "Raise the flooding item", "owner": "Me", "deadline": "today"},
    {"action": "Send roofing prices", "owner": None, "deadline": None},
]


def _text(buf):
    xml = zipfile.ZipFile(io.BytesIO(buf.getvalue())).read("word/document.xml").decode("utf-8")
    return xml


def test_the_sections_are_the_documents_headings_in_order():
    xml = _text(mm.generate_prose_document("Meeting Notes", "Waipuna Rise", SECTIONS, []))
    assert xml.index("What this was") < xml.index("Still open")
    assert "Discussion Topics" not in xml, "this is not the minutes layout"


def test_an_action_without_an_owner_or_date_says_so_rather_than_inventing_one():
    xml = _text(mm.generate_prose_document("Meeting Notes", "Waipuna Rise", SECTIONS, ACTIONS))
    assert "Raise the flooding item" in xml and "Me" in xml and "today" in xml
    assert "Send roofing prices" in xml
    assert "no owner recorded" in xml and "no date" in xml


def test_a_record_with_no_actions_renders_without_an_actions_table():
    xml = _text(mm.generate_prose_document("Meeting Notes", "Waipuna Rise", SECTIONS, []))
    assert "Actions" not in xml


def _heading_count(xml, text):
    """Count paragraphs styled as a heading whose run text is exactly `text`.

    Heading style is `Heading1`/`Heading 1` depending on python-docx version, so this
    matches on the paragraph style prefix rather than the exact style id.
    """
    import re
    count = 0
    for para in re.findall(r"<w:p\b.*?</w:p>", xml, flags=re.S):
        if re.search(r'w:val="Heading1"', para) and f">{text}<" in para:
            count += 1
    return count


def test_a_model_written_actions_section_does_not_duplicate_the_heading():
    """The built-in template asks the model for a section called 'Actions'. The
    renderer must not add its own heading above the table on top of that one --
    confirmed in a real document from TEST as a doubled 'Actions' heading."""
    sections = SECTIONS + [
        {"title": "Actions", "paragraphs": ["Discussed the outstanding items below."]},
    ]
    xml = _text(mm.generate_prose_document("Meeting Notes", "Waipuna Rise", sections, ACTIONS))
    assert _heading_count(xml, "Actions") == 1
    assert "Discussed the outstanding items below." in xml, "the model's prose must survive"
    assert "Raise the flooding item" in xml, "the table must survive"


def test_the_renderers_own_actions_heading_still_appears_without_a_prose_section():
    xml = _text(mm.generate_prose_document("Meeting Notes", "Waipuna Rise", SECTIONS, ACTIONS))
    assert _heading_count(xml, "Actions") == 1
