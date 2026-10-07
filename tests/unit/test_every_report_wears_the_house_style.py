"""Every Word report wears one house style (owner, 2026-10-08): running header
and footer with page numbers, navy headings, styled tables, coloured checklist
answers, a shaded label column on Report Details. Read back from the .docx."""
import io

import pytest

docx = pytest.importorskip("docx")
mm = pytest.importorskip("lambda_meeting_minutes")
import report_style  # noqa: E402

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _shd(cell):
    el = cell._tc.tcPr.find(W + "shd") if cell._tc.tcPr is not None else None
    return el.get(W + "fill") if el is not None else None


SECTIONS = [
    {"title": "Inspection Details", "facts": True,
     "paragraphs": ["Project | Level 2 Slab", "---|---", "Date | Tuesday 6 October 2026"],
     "line_refs": [[], [], []]},
    {"title": "A. Documents & Approvals",
     "paragraphs": ["| Item | Answer | Comment |", "|---|---|---|",
                    "| Drawings on site | Yes | Rev C |", "| Engineer released | No | Wednesday |",
                    "| PT inspected | N/A | none |"],
     "line_refs": [[]] * 5},
]


@pytest.fixture
def doc():
    buf = mm.generate_prose_document("Concrete Pre-pour Inspection Checklist",
                                     "Tuesday 6 October 2026", SECTIONS,
                                     [{"action": "Fix cover", "owner": "Steel fixers", "deadline": "tomorrow"}],
                                     header_left="Level 2 Slab")
    return docx.Document(io.BytesIO(buf.getvalue()))


def test_THE_every_page_carries_the_project_title_and_page_x_of_y(doc):
    sec = doc.sections[0]
    head = sec.header.paragraphs[0].text
    assert head.startswith("Level 2 Slab") and "Concrete Pre-pour Inspection Checklist" in head
    foot = sec.footer.paragraphs[0]
    assert foot.text.startswith("Prepared with FieldSight")
    codes = " ".join(x.text for x in foot._p.iter(W + "instrText"))
    assert "PAGE" in codes and "NUMPAGES" in codes
    assert round(sec.page_width.cm, 1) == 21.0 and round(sec.left_margin.cm, 1) == 2.0


def test_the_title_and_headings_are_the_house_type(doc):
    assert doc.paragraphs[0].style.name == "Title"
    assert doc.paragraphs[0].text == "Concrete Pre-pour Inspection Checklist"
    assert str(doc.styles["Heading 1"].font.color.rgb) == report_style.NAVY
    assert doc.styles["Normal"].font.name == "Calibri"


def test_report_details_shade_the_labels_and_have_no_header_row(doc):
    facts = doc.tables[0]
    assert _shd(facts.cell(0, 0)) == report_style.LABEL
    assert _shd(facts.cell(0, 1)) is None


def test_a_checklist_answer_is_coloured_and_the_header_is_navy(doc):
    chk = doc.tables[1]
    assert _shd(chk.cell(0, 0)) == report_style.NAVY
    assert _shd(chk.cell(1, 1)) == report_style.ANSWER_STYLE["yes"][0]
    assert _shd(chk.cell(2, 1)) == report_style.ANSWER_STYLE["no"][0]
    assert _shd(chk.cell(3, 1)) == report_style.ANSWER_STYLE["n/a"][0]
    header_flag = chk.rows[0]._tr.trPr.find(W + "tblHeader")
    assert header_flag is not None, "the header repeats on every page"


def test_every_table_is_styled_once(doc):
    assert len(doc.tables) == 3
    for t in doc.tables:
        assert report_style._styled(t)


def test_the_session_report_and_the_daily_report_wear_it_too():
    src_mm = open(mm.__file__, encoding="utf-8").read()
    assert src_mm.count("report_style.setup(doc, title") == 2
    assert src_mm.count("report_style.style_all(doc)") == 2
    rg = open(mm.__file__.replace("lambda_meeting_minutes", "lambda_report_generator"),
              encoding="utf-8").read()
    assert "report_style.setup(doc, title" in rg and "report_style.style_all(doc)" in rg
