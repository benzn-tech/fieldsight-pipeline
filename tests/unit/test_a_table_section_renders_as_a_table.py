"""Choosing "Table" produces a table, or the choice is not offered.

`kind` reached the editor, the body, the database and the worker, and
`render_prompt` never read it -- while the editor's own Test render DID honour
it. So a person who chose "List" saw a list in the preview and got prose in the
document, and the only way to get the format they had already asked for was to
write it into the description in words. That free text then went into the prompt
beside our rules, which is the thing the rest of this change is about.

Wiring `kind` up is therefore not a nicety; it is the supply side. But wiring it
only as far as the PROMPT would have moved the lie rather than removed it for
"Table": this renderer knows headings, bullets and paragraphs, so a model asked
for a markdown table produced pipe characters, and pipe characters in a Word
document are worse than the prose they replaced.

THE test is `pipe rows become a table and not a paragraph of pipes`.

`photos` went the other way and is refused at the door: nothing in this path
inserts an image, so there is no version of it that is not a heading with a
sentence underneath. Refusing is the honest half of the same rule.
"""
import pytest

lmm = pytest.importorskip("lambda_meeting_minutes")

if not getattr(lmm, "DOCX_AVAILABLE", False):
    pytest.skip("python-docx is not installed here (it is in CI via the layer)",
                allow_module_level=True)

ROWS = [
    "| Item | Owner | When |",
    "|---|---|---|",
    "| Crane pad | Sam | Friday |",
    "| Site power | Brad | next week |",
]


def _doc(paragraphs):
    return lmm.generate_prose_document(
        "Report", "2026-09-23", [{"title": "Deliveries", "paragraphs": paragraphs}], [])


def _rendered(paragraphs):
    from docx import Document
    import io as _io
    buf = _doc(paragraphs)
    return Document(_io.BytesIO(buf.getvalue()))


# ---- THE test ---------------------------------------------------------------

def test_THE_test_pipe_rows_become_a_table_and_not_a_paragraph_of_pipes():
    doc = _rendered(ROWS)
    assert len(doc.tables) == 1
    table = doc.tables[0]
    assert [c.text for c in table.rows[0].cells] == ["Item", "Owner", "When"]
    assert [c.text for c in table.rows[1].cells] == ["Crane pad", "Sam", "Friday"]
    assert len(table.rows) == 3, "the |---| rule is a rule, not a row"

    for p in doc.paragraphs:
        assert "|" not in p.text, "a pipe left in the prose is the old behaviour"


def test_text_around_a_table_still_reads_as_text():
    doc = _rendered(["Three deliveries landed."] + ROWS + ["Nothing else moved."])
    texts = [p.text for p in doc.paragraphs]
    assert "Three deliveries landed." in texts
    assert "Nothing else moved." in texts
    assert len(doc.tables) == 1


def test_a_ragged_table_does_not_lose_a_cell_or_raise():
    """The model writes these; it will not always write them square."""
    doc = _rendered(["| A | B | C |", "| only one |"])
    table = doc.tables[0]
    assert len(table.columns) == 3
    assert [c.text for c in table.rows[1].cells] == ["only one", "", ""]


def test_a_sentence_with_a_pipe_in_it_is_not_a_table():
    doc = _rendered(["The board reads Gate A | Gate B."])
    assert not doc.tables
    assert "The board reads Gate A | Gate B." in [p.text for p in doc.paragraphs]


def test_bullets_and_prose_are_unchanged():
    """Nothing above may cost the two things this renderer already did."""
    doc = _rendered(["- first", "* second", "a sentence"])
    styles = [(p.text, p.style.name) for p in doc.paragraphs if p.text]
    assert ("first", "List Bullet") in styles
    assert ("second", "List Bullet") in styles
    assert ("a sentence", "Normal") in styles
