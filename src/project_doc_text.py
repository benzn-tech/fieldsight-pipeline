"""Read a project document's text -- pure, no AWS (spec section "Extraction").

DOCX via python-docx (the existing docx layer), PDF via pypdf (its own pure-Python layer,
R13), TXT/MD as is. Output is a flat list of blocks the chunker walks:
  {"kind": "heading" | "para", "level": int | None, "text": str, "page": int | None}

A numbered clause line ("3.2 Formwork") is a heading only when it reads like a title: at
most 100 characters after the number, containing a letter, not ending in "." or ";". A
numbered clause that is a sentence ("3.2.1 The contractor shall ...") is content -- a spec
written one sentence per clause would otherwise fragment into thousands of chunks.

Unreadable = the person must change the file (scanned, too big, damaged, empty).
NotConfigured = this deployment cannot read the type (layer missing) -- an error, not the
file's fault.
"""
import io
import logging
import re

import project_doc_types as types

logger = logging.getLogger(__name__)

try:
    import docx  # python-docx, from the docx layer (cp312, BUG-24)
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    DOCX_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by monkeypatching the flag
    docx = Table = Paragraph = None
    DOCX_AVAILABLE = False

try:
    import pypdf  # from the fieldsight-pypdf layer (R13)
    PDF_AVAILABLE = True
except ImportError:  # pragma: no cover
    pypdf = None
    PDF_AVAILABLE = False

SCANNED_MIN_CHARS_PER_PAGE = 200

MSG_SCANNED = "This looks like a scanned PDF -- scanned documents are not read yet."
MSG_TOO_BIG = "This file is larger than 50 MB."
MSG_TOO_MANY_PAGES = "This document has more than 500 pages."
MSG_TOO_MUCH_TEXT = "This document has more than 2 million characters of text."
MSG_EMPTY = "No text found in this file."
MSG_PASSWORD = "This PDF is password-protected -- upload an unprotected copy."
MSG_DAMAGED = "This file could not be opened -- it may be damaged."
MSG_TYPE = types.TYPE_ERROR
MSG_NO_DOCX = "Word reading is not set up on this deployment."
MSG_NO_PDF = "PDF reading is not set up on this deployment."

_CLAUSE = re.compile(r"^(\d{1,3}(?:\.\d{1,3})*)\.?\s+(\S.*)$")
_LETTER = re.compile(r"[^\W\d_]")
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_STYLE_HEADING = re.compile(r"^Heading (\d)$")


class Unreadable(Exception):
    def __init__(self, reason, pages=None):
        super().__init__(reason)
        self.reason, self.pages = reason, pages


class NotConfigured(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _block(kind, text, level=None, page=None):
    return {"kind": kind, "level": level, "text": text, "page": page}


def _clause_heading(line):
    """(level, text) when the line is a short numbered title, else None."""
    m = _CLAUSE.match(line.strip())
    if not m:
        return None
    rest = m.group(2).rstrip()
    if len(rest) > 100 or rest.endswith((".", ";")) or not _LETTER.search(rest):
        return None
    return m.group(1).count(".") + 1, line.strip()


def _line_block(line, page=None):
    head = _clause_heading(line)
    if head:
        return _block("heading", head[1], level=head[0], page=page)
    return _block("para", line.strip(), page=page)


def _docx(data):
    if not DOCX_AVAILABLE:
        raise NotConfigured(MSG_NO_DOCX)
    try:
        d = docx.Document(io.BytesIO(data))
    except Exception:  # noqa: BLE001 - not a zip, not a Word file, truncated
        raise Unreadable(MSG_DAMAGED)
    blocks = []
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, d)
            text = (p.text or "").strip()
            if not text:
                continue
            style = (p.style.name if p.style is not None else "") or ""
            m = _STYLE_HEADING.match(style)
            if m or style == "Title":
                blocks.append(_block("heading", text, level=int(m.group(1)) if m else 1))
            else:
                blocks.append(_line_block(text))
        elif tag == "tbl":
            for row in Table(child, d).rows:
                cells, prev = [], None
                for cell in row.cells:
                    if cell._tc is prev:      # a merged cell appears once per grid column
                        continue
                    prev = cell._tc
                    cells.append(" ".join(cell.text.split()))
                line = " | ".join(x for x in cells if x)
                if line:
                    blocks.append(_block("para", line))
    return blocks, None


def _pdf(data):
    if not PDF_AVAILABLE:
        raise NotConfigured(MSG_NO_PDF)
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise Unreadable(MSG_PASSWORD)
        n = len(reader.pages)
    except Unreadable:
        raise
    except Exception:  # noqa: BLE001 - truncated or not a PDF
        raise Unreadable(MSG_DAMAGED)
    if n > types.MAX_PAGES:
        raise Unreadable(MSG_TOO_MANY_PAGES, pages=n)
    blocks, total = [], 0
    for i in range(n):
        try:
            text = reader.pages[i].extract_text() or ""
        except Exception:  # noqa: BLE001 - one bad page must not lose the document
            logger.warning("project doc: page %d could not be read", i + 1)
            text = ""
        total += len(text.strip())
        para = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                if para:
                    blocks.append(_block("para", " ".join(para), page=i + 1))
                    para = []
                continue
            head = _clause_heading(line)
            if head:
                if para:
                    blocks.append(_block("para", " ".join(para), page=i + 1))
                    para = []
                blocks.append(_block("heading", head[1], level=head[0], page=i + 1))
            else:
                para.append(line)
        if para:
            blocks.append(_block("para", " ".join(para), page=i + 1))
    if n and total / n < SCANNED_MIN_CHARS_PER_PAGE:
        raise Unreadable(MSG_SCANNED, pages=n)
    return blocks, n


def _decode(data):
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def _text(data, markdown):
    blocks = []
    for para in re.split(r"\n\s*\n", _decode(data).replace("\r\n", "\n")):
        para = para.strip()
        if not para:
            continue
        m = _MD_HEADING.match(para) if markdown and "\n" not in para else None
        if m:
            blocks.append(_block("heading", m.group(2), level=len(m.group(1))))
        else:
            blocks.append(_block("para", para))
    return blocks, None


def extract(data, ext):
    """{"blocks", "pages", "chars"} or raises Unreadable / NotConfigured."""
    if len(data) > types.MAX_BYTES:
        raise Unreadable(MSG_TOO_BIG)
    if ext == "docx":
        blocks, pages = _docx(data)
    elif ext == "pdf":
        blocks, pages = _pdf(data)
    elif ext in ("txt", "md"):
        blocks, pages = _text(data, markdown=ext == "md")
    else:
        raise Unreadable(MSG_TYPE)
    chars = sum(len(b["text"]) for b in blocks)
    if chars == 0:
        raise Unreadable(MSG_EMPTY, pages=pages)
    if chars > types.MAX_CHARS:
        raise Unreadable(MSG_TOO_MUCH_TEXT, pages=pages)
    return {"blocks": blocks, "pages": pages, "chars": chars}
