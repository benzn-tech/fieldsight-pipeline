"""Read a checklist out of a Word form -- python-docx, no model (spec 2026-10-16 section 3).

Free, exact and deterministic. The rule (the spike's truth reader, generalised -- ruling R8):
  * an ITEM row is a row any of whose cells after the first is an answer cell: its text
    is "Yes No N/A" once checkbox glyphs are removed, or it holds a w14 checkbox content
    control. The item text is the first cell's text, whitespace-joined.
  * a SECTION is a non-empty single-cell (merged) row, or the first cell of a header row
    (cell 2 reads "Yes/No/NA" or "COMPLIES ...") -- unless the very next row is itself a
    single-cell section row, in which case the header row is only column headings.
  * tables with no item row at all (project details, signatures, comments) are skipped.
The python-docx layer is cp312 (BUG-24); without it AVAILABLE is False and the worker
answers "upload a PDF" instead of raising ImportError.
"""
import logging
import re

logger = logging.getLogger(__name__)

try:
    import docx  # python-docx, from the docx layer
    AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the worker test via monkeypatch
    docx = None
    AVAILABLE = False

_ANSWER = re.compile(r"^yes\s*no\s*n/?a$", re.IGNORECASE)
_HEADER = re.compile(r"(yes\s*/\s*no|complies)", re.IGNORECASE)
_BOXES = re.compile("[\u2610\u2611\u2612\u2713\u2714\u25a1\u25a0]")
_W14_CHECKBOX = "{http://schemas.microsoft.com/office/word/2010/wordml}checkbox"


def _unique_cells(row):
    """A merged cell appears once per grid column it spans; keep it once."""
    out, prev = [], None
    for cell in row.cells:
        if cell._tc is prev:
            continue
        prev = cell._tc
        out.append(cell)
    return out


def _text(cell):
    return " ".join(_BOXES.sub(" ", cell.text).split())


def _is_answer_cell(cell):
    if _ANSWER.match(_text(cell)):
        return True
    return any(el.tag == _W14_CHECKBOX for el in cell._tc.iter())


def _kind(cells):
    """('item'|'header'|'section'|None, first-cell text)."""
    first = _text(cells[0]) if cells else ""
    if len(cells) > 1 and any(_is_answer_cell(c) for c in cells[1:]):
        return "item", first
    if len(cells) > 1 and _HEADER.search(_text(cells[1])):
        return "header", first
    if len(cells) == 1 and first:
        return "section", first
    return None, first


def read(source):
    """[{title, items: [str]}] in document order; [] when no checklist table is found."""
    document = docx.Document(source)
    sections = []
    for table in document.tables:
        rows = [_kind(_unique_cells(r)) for r in table.rows]
        if not any(kind == "item" for kind, _ in rows):
            continue
        for i, (kind, text) in enumerate(rows):
            if kind == "item":
                if not sections:
                    sections.append({"title": "", "items": []})
                sections[-1]["items"].append(text)
            elif kind == "header":
                next_kind = rows[i + 1][0] if i + 1 < len(rows) else None
                if next_kind != "section" and text:
                    sections.append({"title": text, "items": []})
            elif kind == "section":
                sections.append({"title": text, "items": []})
    return [s for s in sections if s["items"]]
