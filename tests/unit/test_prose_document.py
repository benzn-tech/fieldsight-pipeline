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
