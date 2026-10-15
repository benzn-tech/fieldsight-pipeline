"""What a project document may be uploaded as (spec 2026-10-16-project-documents-design).

One list, read by org-api (upload-url, create), the worker and the extractor, so they
cannot disagree about what is accepted. Limits are module attributes (tests patch them).
"""

EXT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain",
    "md": "text/markdown",
}
MAX_BYTES = 50 * 1024 * 1024
MAX_PAGES = 500
MAX_CHARS = 2_000_000
MAX_CHUNKS = 1000

TYPE_ERROR = "Upload a Word file (.docx), a PDF, or a text file (.txt, .md)."


def ext_of(name):
    name = (name or "").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""
