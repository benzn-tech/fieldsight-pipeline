"""Text extraction for project documents (spec section "Extraction"): headings, clause
numbers, tables, pages, the scanned-PDF rule and the limits. Fixtures are generated."""
import pytest

pytest.importorskip("docx")
pytest.importorskip("pypdf")

import project_doc_text as t  # noqa: E402
import project_doc_types  # noqa: E402
from tests.unit.doc_fixtures import make_docx, make_pdf  # noqa: E402

BODY = "The contractor shall keep formwork within the stated tolerances at all times. " * 4


def _kinds(blocks):
    return [(b["kind"], b["level"], b["text"]) for b in blocks]


def test_docx_heading_styles_and_short_clause_lines_become_headings():
    def build(d):
        d.add_heading("Concrete", level=1)
        d.add_paragraph("3.2 Formwork")
        d.add_paragraph(BODY)
        d.add_paragraph("3.2.1 The contractor shall provide shop drawings for all formwork.")
    out = t.extract(make_docx(build), "docx")
    blocks = _kinds(out["blocks"])
    assert blocks[0] == ("heading", 1, "Concrete")
    assert blocks[1] == ("heading", 2, "3.2 Formwork")
    assert blocks[2][0] == "para"
    # A clause that is a sentence is content, not a heading -- or a spec of one-sentence
    # clauses would fragment into thousands of chunks.
    assert blocks[3] == ("para", None,
                         "3.2.1 The contractor shall provide shop drawings for all formwork.")
    assert out["pages"] is None and out["chars"] > 0


def test_docx_tables_are_flattened_row_by_row_in_document_order():
    def build(d):
        d.add_paragraph("Before the table")
        table = d.add_table(rows=2, cols=2)
        table.cell(0, 0).text, table.cell(0, 1).text = "Element", "Cover"
        table.cell(1, 0).text, table.cell(1, 1).text = "Slab", "40 mm"
        d.add_paragraph("After the table")
    texts = [b["text"] for b in t.extract(make_docx(build), "docx")["blocks"]]
    assert texts == ["Before the table", "Element | Cover", "Slab | 40 mm", "After the table"]


def test_pdf_text_keeps_page_numbers_and_finds_clause_headings():
    pages = [["1 General", BODY], ["3.2 Formwork", "Tolerance is 5 mm on all faces. " * 8]]
    out = t.extract(make_pdf(pages), "pdf")
    assert out["pages"] == 2
    heads = [(b["level"], b["text"], b["page"]) for b in out["blocks"] if b["kind"] == "heading"]
    assert heads == [(1, "1 General", 1), (2, "3.2 Formwork", 2)]
    assert {b["page"] for b in out["blocks"] if b["kind"] == "para"} == {1, 2}


def test_a_pdf_without_a_text_layer_is_unreadable_as_scanned():
    with pytest.raises(t.Unreadable) as e:
        t.extract(make_pdf([[], [], ["p. 3"]]), "pdf")
    assert e.value.reason == t.MSG_SCANNED and e.value.pages == 3


def test_markdown_hashes_start_a_heading_path():
    out = t.extract("# Spec\n\nIntro text.\n\n## Cover\n\n40 mm.\n".encode(), "md")
    assert _kinds(out["blocks"]) == [("heading", 1, "Spec"), ("para", None, "Intro text."),
                                     ("heading", 2, "Cover"), ("para", None, "40 mm.")]


def test_plain_text_is_paragraphs_as_is():
    out = t.extract(b"First para\nstill first.\n\nSecond.", "txt")
    assert [b["text"] for b in out["blocks"]] == ["First para\nstill first.", "Second."]


@pytest.mark.parametrize("data,ext", [
    (b"this is not a zip", "docx"),
    (b"%PDF-1.4\n1 0 obj\n<<", "pdf"),
])
def test_a_damaged_file_is_unreadable_with_a_reason(data, ext):
    """Review Focus 4."""
    with pytest.raises(t.Unreadable) as e:
        t.extract(data, ext)
    assert e.value.reason == t.MSG_DAMAGED


def test_an_empty_file_is_unreadable():
    with pytest.raises(t.Unreadable) as e:
        t.extract(b"   \n\n", "txt")
    assert e.value.reason == t.MSG_EMPTY


def test_too_many_pages_is_unreadable(monkeypatch):
    monkeypatch.setattr(project_doc_types, "MAX_PAGES", 2)
    with pytest.raises(t.Unreadable) as e:
        t.extract(make_pdf([["a" * 300]] * 3), "pdf")
    assert e.value.reason == t.MSG_TOO_MANY_PAGES


def test_too_much_text_is_unreadable(monkeypatch):
    monkeypatch.setattr(project_doc_types, "MAX_CHARS", 10)
    with pytest.raises(t.Unreadable) as e:
        t.extract(b"x" * 11, "txt")
    assert e.value.reason == t.MSG_TOO_MUCH_TEXT


def test_too_many_bytes_is_unreadable(monkeypatch):
    monkeypatch.setattr(project_doc_types, "MAX_BYTES", 5)
    with pytest.raises(t.Unreadable) as e:
        t.extract(b"123456", "txt")
    assert e.value.reason == t.MSG_TOO_BIG


def test_a_missing_reader_is_not_configured_not_unreadable(monkeypatch):
    monkeypatch.setattr(t, "PDF_AVAILABLE", False)
    with pytest.raises(t.NotConfigured) as e:
        t.extract(make_pdf([["x" * 300]]), "pdf")
    assert e.value.reason == t.MSG_NO_PDF
    monkeypatch.setattr(t, "DOCX_AVAILABLE", False)
    with pytest.raises(t.NotConfigured) as e:
        t.extract(b"PK", "docx")
    assert e.value.reason == t.MSG_NO_DOCX


def test_an_unknown_extension_is_unreadable():
    with pytest.raises(t.Unreadable):
        t.extract(b"x", "xlsx")
