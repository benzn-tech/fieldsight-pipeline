"""Structure-first chunking for project documents (spec section "Chunking").

A heading of level 1-3 always closes the current chunk and sets the heading path, so a
chunk never spans two top-level headings (or two sub-sections). Within a section,
paragraphs are packed up to TARGET characters; the next chunk of the SAME section starts
with the last ~OVERLAP characters of the previous one. A paragraph longer than TARGET is
cut at a space, a CJK full stop, or -- when there is neither (Chinese) -- hard at TARGET.
No chunk exceeds HARD_CAP. The heading path is prepended to the EMBEDDED text only.
"""

TARGET = 1800
HARD_CAP = 3000
OVERLAP = 150
MAX_PATH_LEVEL = 3
_SEP = "\n\n"


def _split_long(text, size):
    out = []
    while len(text) > size:
        cut = max(text.rfind(" ", 0, size), text.rfind("。", 0, size) + 1)
        if cut < size // 2:
            cut = size
        out.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        out.append(text)
    return out


def _tail(text):
    tail = text[-OVERLAP:]
    sp = tail.find(" ")
    return tail[sp + 1:] if 0 <= sp < 50 else tail


def chunk(blocks):
    out, path = [], []
    buf, pages, has_content, carry = [], [], False, ""

    def flush(keep_overlap):
        nonlocal buf, pages, has_content, carry
        if has_content:
            text = _SEP.join(buf).strip()
            out.append({"ordinal": len(out),
                        "heading": " > ".join(h for _, h in path) or None,
                        "page_from": min(pages) if pages else None,
                        "page_to": max(pages) if pages else None,
                        "chunk_text": text})
            carry = _tail(text) if keep_overlap else ""
        elif not keep_overlap:
            carry = ""
        buf, pages, has_content = [], [], False

    for b in blocks:
        level = b.get("level")
        if b.get("kind") == "heading" and level and level <= MAX_PATH_LEVEL:
            flush(False)
            path = [p for p in path if p[0] < level] + [(level, b["text"].strip())]
            continue
        text = (b.get("text") or "").strip()
        if not text:
            continue
        for piece in _split_long(text, TARGET):
            size = sum(len(x) + len(_SEP) for x in buf)
            if has_content and size + len(piece) > TARGET:
                flush(True)
            if not buf and carry:
                buf.append(carry)
            buf.append(piece)
            has_content = True
            if b.get("page") is not None:
                pages.append(b["page"])
    flush(False)
    return out


def embed_text(c):
    """The text that is embedded: heading path first, so a clause found by its heading is
    found by meaning too. Never stored."""
    return f"{c['heading']}{_SEP}{c['chunk_text']}" if c.get("heading") else c["chunk_text"]
