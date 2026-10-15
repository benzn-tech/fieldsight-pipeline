"""Read a checklist form's pages with a vision model (template digitise spec 2026-10-16).

The uploaded document is DATA to this prompt; everything decided about it is decided in
code (template_draft.clean). The prompt is the 2026-10-16 spike's, verbatim -- it scored
recall 1.00 / wording 0.99-1.00 on both fixture forms and is not to be tuned without
re-running that measurement.
"""
import base64
import logging
import os
import re

import llm_utils
import report_photos
import template_types

logger = logging.getLogger(__name__)

PROMPT = """These page images are a construction site checklist form (one document, pages in order).
Digitise it into a reusable checklist TEMPLATE.
Return STRICT JSON only: {"sections":[{"title":"...","items":["...", "..."]}]}
Rules:
- An item is one checklist row/question that the inspector answers (e.g. with Yes/No/N/A boxes).
- A section is the heading that groups those rows (a section header row or table heading). Keep sections in page order.
- Copy every item's wording EXACTLY as printed (same words, spelling, punctuation, capitalisation, including typos). Join lines that wrap inside one cell with a single space.
- Drop only the leading list label (like "1." or "a)") from titles and items.
- Preserve the original order. Do not invent, merge, split, paraphrase or reorder items.
- Do NOT include: column headers (Yes/No/NA, Comment, Due by...), answers/ticks, filled-in comments, names/dates, signature blocks, page footers, the project details box, or free-text observation registers.
- A table repeated across a page break with the same heading is ONE section."""

DEFAULT_MODEL = "google/gemini-3.8-flash"
FALLBACK_ENV = "TEMPLATE_DIGITISE_FALLBACK_MODELS"
IMAGE_EDGE = 2000       # px, long edge: ruling R6
ANSWER_TOKENS = 16000   # the 115-item form answered in ~2,500

MSG_PDF_REFUSED = "PDF files cannot be read yet — export the pages as images and upload those."
MSG_TOO_MANY_PAGES = "This form has more than 15 pages — upload the checklist pages only."
MSG_BAD_ANSWER = "The reader's answer could not be used — try again."
MSG_UNAVAILABLE = "The reader is not answering right now — try again in a few minutes."

_PAGE_OBJ = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")


def model():
    return os.environ.get("TEMPLATE_DIGITISE_MODEL", "").strip() or DEFAULT_MODEL


def pdf_page_count(body):
    """Best effort (ruling R4): page objects written in the clear. 0 when the page tree
    sits in a compressed object stream -- then the count is unknown, not zero pages."""
    return len(_PAGE_OBJ.findall(body or b""))


def check(pages):
    """An input problem worded for the person, or None. Runs before any model call."""
    pdfs = [p for p in pages if p["content_type"] == "application/pdf"]
    if pdfs and not template_types.PDF_ACCEPTED:
        return MSG_PDF_REFUSED
    count = pdf_page_count(pdfs[0]["body"]) if pdfs else len(pages)
    if count > template_types.MAX_PAGES:
        return MSG_TOO_MANY_PAGES
    return None


def _b64(body):
    return base64.b64encode(body).decode("ascii")


def _image_part(page):
    small = report_photos.shrink(page["body"], IMAGE_EDGE)
    # shrink() hands back the original bytes when it cannot read or improve them, and
    # a re-encoded image is always JPEG.
    mime = page["content_type"] if small is page["body"] else "image/jpeg"
    return {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (mime, _b64(small))}}


def _pdf_part(page):
    return {"type": "file", "file": {
        "filename": page.get("name") or "form.pdf",
        "file_data": "data:application/pdf;base64," + _b64(page["body"])}}


def build_parts(pages):
    parts = [{"type": "text", "text": PROMPT}]
    for page in pages:
        if page["content_type"] == "application/pdf":
            parts.append(_pdf_part(page))
        else:
            parts.append(_image_part(page))
    return parts


def read(pages, deadline=None):
    """(raw_sections, None) or (None, message for the person). One retry when the answer
    is not the JSON asked for; a transport failure is not retried here (the chain in
    llm_utils already has)."""
    parts = build_parts(pages)
    for attempt in range(2):
        text, err = llm_utils.call_llm_parts(
            parts, model=model(), max_tokens=ANSWER_TOKENS, fallback_env=FALLBACK_ENV,
            deadline=deadline, caller="template_digitise")
        if err is not None:
            logger.error("template vision call failed: %s", err)
            return None, MSG_UNAVAILABLE
        data = llm_utils.extract_json(text or "")
        if isinstance(data, dict) and isinstance(data.get("sections"), list):
            return data["sections"], None
        logger.warning("template vision answer was not {sections: [...]} (attempt %d): %.300r",
                       attempt + 1, text)
    return None, MSG_BAD_ANSWER
