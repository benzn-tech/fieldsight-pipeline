"""Read a project document's text -- pure, no AWS (spec section "Extraction").

DOCX via python-docx (the existing docx layer), PDF via pypdf (its own pure-Python layer,
R13), TXT/MD as is. Output is a flat list of blocks the chunker walks:
  {"kind": "heading" | "para", "level": int | None, "text": str, "page": int | None}

A numbered clause line ("3.2 Formwork") is a heading only when it reads like a title: at
most 100 characters after the number, containing a letter, not ending in "." or ";", not
starting lowercase and not starting with a unit ("20 MPa concrete ..."). A numbered clause
that is a sentence ("3.2.1 The contractor shall ...") is content -- a spec written one
sentence per clause would otherwise fragment into thousands of chunks.

PDF text arrives as wrapped LINES, so a line that merely starts with a number ("20 MPa
concrete for all") must not become a heading: in a PDF a clause line is a heading only at
the start of a page or after a line that ended a sentence or block. A PDF heading's text is
ALSO kept at the start of the following paragraph, so the customer's wording is never only
in the chunk's `heading` column (if the rule is ever wrong, nothing is lost from the body).

Unreadable = the person must change the file (scanned, too big, damaged, empty, protected).
NotConfigured = this deployment cannot read the type (layer missing) -- an error, not the
file's fault.
"""
import codecs
import io
import logging
import re
import zipfile

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
    from pypdf.errors import DependencyError as _PdfDependencyError
    PDF_AVAILABLE = True
except ImportError:  # pragma: no cover
    pypdf = None
    PDF_AVAILABLE = False

    class _PdfDependencyError(Exception):
        pass

SCANNED_MIN_CHARS_PER_PAGE = 200
MAX_REPLACEMENT_RATIO = 0.02

_MB = 1024 * 1024
MSG_SCANNED = "This looks like a scanned PDF -- scanned documents are not read yet."
MSG_TOO_BIG = f"This file is larger than {types.MAX_BYTES // _MB} MB."
MSG_TOO_MANY_PAGES = f"This document has more than {types.MAX_PAGES} pages."
MSG_TOO_MUCH_TEXT = f"This document has more than {types.MAX_CHARS:,} characters of text."
MSG_TOO_BIG_UNPACKED = (f"This Word file unpacks to more than "
                        f"{types.MAX_UNPACKED_BYTES // _MB} MB -- it may be damaged.")
MSG_EMPTY = "No text found in this file."
MSG_PASSWORD = "This PDF is password-protected -- upload an unprotected copy."
# AES-encrypted PDFs need the `cryptography` package, which the layer deliberately does
# not carry (pure Python only, R13).
MSG_PROTECTED = "This PDF is protected — export an unprotected copy and upload that."
MSG_DAMAGED = "This file could not be opened -- it may be damaged."
MSG_ENCODING = "The text encoding could not be read."
MSG_TYPE = types.TYPE_ERROR
MSG_NO_DOCX = "Word reading is not set up on this deployment."
MSG_NO_PDF = "PDF reading is not set up on this deployment."

_CLAUSE = re.compile(r"^(\d{1,3}(?:\.\d{1,3})*)\.?\s+(\S.*)$")
_LETTER = re.compile(r"[^\W\d_]")
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)(?:\s+#+)?\s*$")
_STYLE_HEADING = re.compile(r"^Heading (\d)$")
_CJK = re.compile(r"[　-〿぀-ヿ㐀-䶿一-鿿豈-﫿"
                  r"＀-￯]")
# A number followed by one of these is a quantity in running text, not a clause number.
_UNITS = frozenset({
    "mm", "cm", "m", "km", "m2", "m3", "m²", "m³", "mm2", "mm²", "kg", "g",
    "t", "kN", "kPa", "MPa", "GPa", "N", "L", "l", "mL", "No.", "No", "no.", "nr", "%",
    "°C", "x",
})
_BLOCK_END = (".", ":", ";", "。", "：", "；")


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
    if rest[0].islower() or rest.split()[0].rstrip(",;:") in _UNITS:
        return None
    return m.group(1).count(".") + 1, line.strip()


def _line_block(line, page=None):
    head = _clause_heading(line)
    if head:
        return _block("heading", head[1], level=head[0], page=page)
    return _block("para", line.strip(), page=page)


def _join_lines(lines):
    """Join wrapped lines with a space -- except between two CJK characters, where a line
    break is not a word break."""
    out = ""
    for ln in lines:
        if out and ln and not (_CJK.match(out[-1]) and _CJK.match(ln[0])):
            out += " "
        out += ln
    return out


def _docx(data):
    if not DOCX_AVAILABLE:
        raise NotConfigured(MSG_NO_DOCX)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            unpacked = sum(i.file_size for i in z.infolist())
    except Exception:  # noqa: BLE001 - not a zip
        raise Unreadable(MSG_DAMAGED)
    if unpacked > types.MAX_UNPACKED_BYTES:
        raise Unreadable(MSG_TOO_BIG_UNPACKED)
    try:
        d = docx.Document(io.BytesIO(data))
    except Exception:  # noqa: BLE001 - not a Word file, truncated
        raise Unreadable(MSG_DAMAGED)
    try:
        return _docx_blocks(d), None
    except Unreadable:
        raise
    except Exception:  # noqa: BLE001 - malformed body XML
        logger.warning("project doc: docx body could not be walked", exc_info=True)
        raise Unreadable(MSG_DAMAGED)


def _docx_blocks(d):
    blocks, total = [], 0

    def add(b):
        nonlocal total
        total += len(b["text"])
        if total > types.MAX_CHARS:
            raise Unreadable(MSG_TOO_MUCH_TEXT)
        blocks.append(b)

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
                add(_block("heading", text, level=int(m.group(1)) if m else 1))
            else:
                add(_line_block(text))
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
                    add(_block("para", line))
    return blocks


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
    except _PdfDependencyError:   # AES without `cryptography` -- deliberately not in the layer
        raise Unreadable(MSG_PROTECTED)
    except Exception:  # noqa: BLE001 - truncated or not a PDF
        raise Unreadable(MSG_DAMAGED)
    if n > types.MAX_PAGES:
        raise Unreadable(MSG_TOO_MANY_PAGES, pages=n)
    blocks, total, seeds = [], 0, []
    for i in range(n):
        page = i + 1
        try:
            text = reader.pages[i].extract_text() or ""
        except _PdfDependencyError:
            raise Unreadable(MSG_PROTECTED, pages=n)
        except Exception:  # noqa: BLE001 - one bad page must not lose the document
            logger.warning("project doc: page %d could not be read", page)
            text = ""
        total += len(text.strip())
        if total > types.MAX_CHARS:
            raise Unreadable(MSG_TOO_MUCH_TEXT, pages=n)
        para, block_ended = [], True          # a page start ends a block

        def flush():
            if para:
                blocks.append(_block("para", _join_lines(para), page=page))
                para.clear()

        for line in text.splitlines():
            line = line.strip()
            if not line:
                flush()
                block_ended = True
                continue
            head = _clause_heading(line) if block_ended else None
            if head:
                flush()
                blocks.append(_block("heading", head[1], level=head[0], page=page))
                seeds.append(head[1])         # kept in the body too (see docstring)
                block_ended = True
                continue
            if not para and seeds:
                para.extend(seeds)
                seeds = []
            para.append(line)
            block_ended = line.endswith(_BLOCK_END)
        flush()
    if seeds:
        blocks.append(_block("para", _join_lines(seeds), page=n))
    if n and total / n < SCANNED_MIN_CHARS_PER_PAGE:
        raise Unreadable(MSG_SCANNED, pages=n)
    return blocks, n


def _decode(data):
    """utf-8 (BOM or not), UTF-16 with a BOM, GB18030 (a superset of GBK), then cp1252.
    A result that is still more than 2% replacement characters is not text we can read."""
    text = None
    candidates = ["utf-8-sig"]
    if data[:2] in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE):
        candidates.insert(0, "utf-16")
    candidates.append("gb18030")
    for enc in candidates:
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = data.decode("cp1252", errors="replace")
    if text and text.count("�") > MAX_REPLACEMENT_RATIO * len(text):
        raise Unreadable(MSG_ENCODING)
    return text


def _text(data, markdown):
    text = _decode(data).replace("\r\n", "\n").replace("\r", "\n")
    blocks, total, in_fence = [], 0, False

    def add(b):
        nonlocal total
        total += len(b["text"])
        if total > types.MAX_CHARS:
            raise Unreadable(MSG_TOO_MUCH_TEXT)
        blocks.append(b)

    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if not markdown:
            add(_block("para", para))
            continue
        # A heading line followed directly by text (no blank line) is still a heading.
        buf = []
        for line in para.split("\n"):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
            m = None if in_fence else _MD_HEADING.match(line.strip())
            if m:
                if buf:
                    add(_block("para", "\n".join(buf).strip()))
                    buf = []
                add(_block("heading", m.group(2), level=len(m.group(1))))
            else:
                buf.append(line)
        if buf and "\n".join(buf).strip():
            add(_block("para", "\n".join(buf).strip()))
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
