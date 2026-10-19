"""What a checklist form may be uploaded as (template digitise spec 2026-10-16).

One list, read by org-api (upload-url, request) and by the worker, so the two cannot
disagree about what is accepted.
"""

EXT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
}
IMAGE_EXTS = ("jpg", "jpeg", "png")
MAX_BYTES = 20 * 1024 * 1024
MAX_IMAGES = 10
MAX_PAGES = 15
# Set by the Task 0 probe of OpenRouter's `file` content part (2026-10-16). False means
# a PDF is refused with "export the pages as images" -- there is no PDF renderer in any
# layer to fall back on.
PDF_ACCEPTED = True

SHAPE_ERROR = "Upload one Word file, one PDF, or up to 10 photos."


def ext_of(name):
    name = (name or "").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def accepted_exts():
    return [e for e in EXT_TYPES if PDF_ACCEPTED or e != "pdf"]


def source_shape_error(exts):
    """None when these extensions make one readable request, else why not."""
    exts = list(exts or [])
    if not exts or any(e not in EXT_TYPES for e in exts):
        return SHAPE_ERROR
    if exts in (["docx"], ["pdf"]):
        return None
    if all(e in IMAGE_EXTS for e in exts) and len(exts) <= MAX_IMAGES:
        return None
    return SHAPE_ERROR
