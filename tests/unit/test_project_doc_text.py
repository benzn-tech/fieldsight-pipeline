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


def _rc4_encrypted_pdf():
    import io

    import pypdf
    w = pypdf.PdfWriter(clone_from=pypdf.PdfReader(io.BytesIO(make_pdf([["x" * 300]]))))
    w.encrypt("pw", algorithm="RC4-128")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


@pytest.mark.parametrize("data,ext,reason", [
    (b"this is not a zip", "docx", "MSG_DAMAGED"),
    (b"%PDF-1.4\n1 0 obj\n<<", "pdf", "MSG_DAMAGED"),
    (None, "pdf", "MSG_PASSWORD"),          # RC4 user password: pure-Python pypdf reads it
])
def test_a_damaged_file_is_unreadable_with_a_reason(data, ext, reason):
    """Review Focus 4."""
    data = _rc4_encrypted_pdf() if data is None else data
    with pytest.raises(t.Unreadable) as e:
        t.extract(data, ext)
    assert e.value.reason == getattr(t, reason)


def test_an_aes_pdf_without_cryptography_is_protected_not_damaged(monkeypatch):
    """AES needs `cryptography`, which the layer does not carry: pypdf raises
    DependencyError, and that is the file's protection, not damage."""
    import pypdf
    from pypdf.errors import DependencyError

    def boom(*a, **k):
        raise DependencyError("cryptography is required for AES")
    monkeypatch.setattr(pypdf, "PdfReader", boom)
    with pytest.raises(t.Unreadable) as e:
        t.extract(make_pdf([["x" * 300]]), "pdf")
    assert e.value.reason == t.MSG_PROTECTED


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


# --- Fix round 1 --------------------------------------------------------------------

def test_a_wrapped_pdf_line_starting_with_a_number_is_not_a_heading():
    lines = ["The contractor shall use", "20 MPa concrete for all", "footings."]
    out = t.extract(make_pdf([lines + ["x" * 250]]), "pdf")
    assert [b for b in out["blocks"] if b["kind"] == "heading"] == []
    assert out["blocks"][0]["text"].startswith(
        "The contractor shall use 20 MPa concrete for all footings.")


def test_a_real_pdf_clause_heading_after_a_sentence_is_still_found():
    out = t.extract(make_pdf([["General requirements apply.", "3.2 Formwork",
                               "Tolerance is 5 mm on all faces. " * 8]]), "pdf")
    heads = [(b["level"], b["text"]) for b in out["blocks"] if b["kind"] == "heading"]
    assert heads == [(2, "3.2 Formwork")]


@pytest.mark.parametrize("line", ["20 MPa concrete", "3 no. footings", "12 mm bars",
                                  "4 the contractor"])
def test_a_quantity_or_lowercase_continuation_is_never_a_clause_heading(line):
    assert t._clause_heading(line) is None


def test_a_pdf_heading_also_stays_in_the_body():
    out = t.extract(make_pdf([["3.2 Formwork", "Tolerance is 5 mm on all faces. " * 8]]),
                    "pdf")
    para = next(b for b in out["blocks"] if b["kind"] == "para")
    assert para["text"].startswith("3.2 Formwork Tolerance")


@pytest.mark.parametrize("n,scanned", [(199, True), (200, False)])
def test_the_scanned_threshold_is_an_average_of_200_chars_per_page(n, scanned):
    data = make_pdf([["a" * n], ["b" * n]])
    if scanned:
        with pytest.raises(t.Unreadable) as e:
            t.extract(data, "pdf")
        assert e.value.reason == t.MSG_SCANNED
    else:
        assert t.extract(data, "pdf")["pages"] == 2


def test_cjk_lines_join_without_a_space_and_latin_lines_with_one():
    assert t._join_lines(["混凝土", "保护层"]) == "混凝土保护层"
    assert t._join_lines(["concrete", "cover"]) == "concrete cover"
    assert t._join_lines(["厚度", "40 mm"]) == "厚度 40 mm"


def test_gbk_text_is_read():
    out = t.extract("混凝土保护层厚度 40 mm".encode("gbk"), "txt")
    assert out["blocks"][0]["text"] == "混凝土保护层厚度 40 mm"


def test_utf16_text_with_a_bom_is_read():
    out = t.extract("Cover 40 mm\n\n保护层".encode("utf-16"), "txt")
    assert [b["text"] for b in out["blocks"]] == ["Cover 40 mm", "保护层"]


def test_undecodable_text_is_unreadable():
    with pytest.raises(t.Unreadable) as e:
        t.extract(b"\x80\x81" * 50, "txt")
    assert e.value.reason == t.MSG_ENCODING


def test_a_markdown_heading_directly_followed_by_text_is_a_heading():
    out = t.extract(b"# Spec\nIntro text.\n## C# usage ##\nBody", "md")
    assert _kinds(out["blocks"]) == [("heading", 1, "Spec"), ("para", None, "Intro text."),
                                     ("heading", 2, "C# usage"), ("para", None, "Body")]


def test_a_hash_inside_a_code_fence_is_not_a_heading():
    out = t.extract(b"```\n# not a heading\n```", "md")
    assert [b["kind"] for b in out["blocks"]] == ["para"]


def test_too_much_pdf_text_stops_early(monkeypatch):
    monkeypatch.setattr(project_doc_types, "MAX_CHARS", 400)
    with pytest.raises(t.Unreadable) as e:
        t.extract(make_pdf([["a" * 300], ["b" * 300], ["c" * 300]]), "pdf")
    assert e.value.reason == t.MSG_TOO_MUCH_TEXT and e.value.pages == 3


def test_a_docx_that_unpacks_too_large_is_refused_before_parsing(monkeypatch):
    data = make_docx(lambda d: d.add_paragraph("hello"))
    monkeypatch.setattr(project_doc_types, "MAX_UNPACKED_BYTES", 100)
    with pytest.raises(t.Unreadable) as e:
        t.extract(data, "docx")
    assert e.value.reason == t.MSG_TOO_BIG_UNPACKED


def test_a_docx_body_that_cannot_be_walked_is_damaged(monkeypatch):
    def broken(d):
        raise ValueError("bad xml")
    monkeypatch.setattr(t, "_docx_blocks", broken)
    with pytest.raises(t.Unreadable) as e:
        t.extract(make_docx(lambda d: d.add_paragraph("hello")), "docx")
    assert e.value.reason == t.MSG_DAMAGED


def test_the_limits_in_the_messages_come_from_the_constants():
    assert f"{project_doc_types.MAX_BYTES // (1024 * 1024)} MB" in t.MSG_TOO_BIG
    assert str(project_doc_types.MAX_PAGES) in t.MSG_TOO_MANY_PAGES
    assert f"{project_doc_types.MAX_CHARS:,}" in t.MSG_TOO_MUCH_TEXT


def test_a_title_cased_wrapped_line_mid_sentence_is_not_a_heading():
    """Rule (b) on its own: the line reads like a title, but the previous line did not end
    a sentence, so it is a wrapped continuation."""
    out = t.extract(make_pdf([["The walls shall be built from", "40 Series Masonry Blocks",
                               "x" * 250]]), "pdf")
    assert [b for b in out["blocks"] if b["kind"] == "heading"] == []


# --- Fix round 2 --------------------------------------------------------------------

@pytest.mark.parametrize("original", ["Pour at 20°C minimum.", "Señor naïve"])
def test_cp1252_text_is_not_mis_decoded_as_gb18030(original):
    """GB18030 decodes these bytes without error into "20癈" / "Se駉r na飗e"."""
    out = t.extract(original.encode("cp1252"), "txt")
    assert out["blocks"][0]["text"] == original


@pytest.mark.parametrize("original", ["混凝土强度", "混凝土强度 C30，坍落度 “180 mm” — 见 3.2 条"])
def test_short_or_mixed_gbk_text_still_decodes_to_chinese(original):
    assert t.extract(original.encode("gbk"), "txt")["blocks"][0]["text"] == original


def test_a_deep_pdf_heading_is_not_seeded_into_the_body_twice():
    """Level > MAX_PATH_LEVEL headings are already body text for the chunker."""
    out = t.extract(make_pdf([["Intro sentence.", "1.2.3.4 Note",
                               "Tolerance is 5 mm on all faces. " * 8]]), "pdf")
    assert [b["level"] for b in out["blocks"] if b["kind"] == "heading"] == [4]
    para = [b for b in out["blocks"] if b["kind"] == "para"][-1]
    assert para["text"].startswith("Tolerance")
