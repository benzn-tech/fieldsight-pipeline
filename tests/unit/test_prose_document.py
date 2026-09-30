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
        {"title": "Actions", "paragraphs": ["**Me** - Raise the flooding item - *today*"]},
    ]
    xml = _text(mm.generate_prose_document("Meeting Notes", "Waipuna Rise", sections, ACTIONS))
    assert _heading_count(xml, "Actions") == 1
    assert "Raise the flooding item" in xml, "the table must survive"


def test_the_actions_are_written_once_as_our_table_under_the_plans_heading():
    """The model was asked to write the actions into the Actions section AND the
    renderer added its own table after every section, below the catch-all: each
    action twice, once with literal asterisks (TEST, 2026-09-23, "daily report"
    v10). The record is data; the table is written from it, once, in place."""
    from docx import Document
    sections = SECTIONS[:1] + [
        {"title": "Actions", "paragraphs": ["**Me** - Raise the flooding item - *today*"]},
    ] + SECTIONS[1:]
    buf = mm.generate_prose_document("Meeting Notes", "Waipuna Rise", sections, ACTIONS)
    doc = Document(io.BytesIO(buf.getvalue()))
    order = []
    for el in doc.element.body.iterchildren():
        tag = el.tag.split("}")[1]
        if tag == "tbl":
            order.append("TABLE")
        elif tag == "p":
            t = "".join(x.text or "" for x in el.iter() if x.tag.endswith("}t")).strip()
            if t:
                order.append(t)
    assert order.index("Actions") + 1 == order.index("TABLE") < order.index("Still open")
    assert order.count("TABLE") == 1
    assert _text(buf).count("Raise the flooding item") == 1, "each action once"


def test_markdown_emphasis_is_formatting_not_asterisks():
    from docx import Document
    sections = [{"title": "Notes", "paragraphs": [
        "**Dom** to fix the gate - *Friday*", "- a **bold** point", "2 * 3 = 6 and a*b",
        "Item | Owner", "---|---", "Gate | **no owner recorded**"]}]
    doc = Document(io.BytesIO(mm.generate_prose_document("T", "", sections, []).getvalue()))
    texts = [p.text for p in doc.paragraphs]
    assert "Dom to fix the gate - Friday" in texts and "a bold point" in texts
    assert "2 * 3 = 6 and a*b" in texts, "a lone asterisk is left alone"
    p = next(p for p in doc.paragraphs if p.text.startswith("Dom"))
    assert p.runs[0].bold and p.runs[0].text == "Dom" and p.runs[-1].italic
    assert doc.tables[0].rows[1].cells[1].text == "no owner recorded"


def test_the_renderers_own_actions_heading_still_appears_without_a_prose_section():
    xml = _text(mm.generate_prose_document("Meeting Notes", "Waipuna Rise", SECTIONS, ACTIONS))
    assert _heading_count(xml, "Actions") == 1


def test_the_prompt_no_longer_asks_the_model_to_write_the_actions_out():
    import report_template as rt
    scope = {"folder": "F", "date": "2026-09-30", "from": "00:00", "to": "23:59", "recordings": 1}
    tpl = {"sections": [{"title": "Actions", "purpose": "p"}],
           "catch_all": {"title": "Anything else", "purpose": "Rest."}}
    p = rt.render_prompt(tpl, scope, ACTIONS, "x")
    assert "do NOT write them out yourself" in p
    assert "**Owner** - what they will do" not in p
    assert "Raise the flooding item | owner: Me | when: today" in p, "still given as context"
