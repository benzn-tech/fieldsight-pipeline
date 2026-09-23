"""What the browser calls the file it just saved.

A generated report lives at `session_reports/{folder}/{date}/{segment}/{requestId}.docx`
-- a key built for storage, where a uuid is exactly right: it never collides and
it never has to be re-derived. The browser, though, takes the last path segment
of the presigned URL as the filename, so every report anybody downloaded landed
in their Downloads folder as a uuid. Thirty of those and the folder is unusable.

The key does not move. Only `Content-Disposition` is added to the presign, which
is a header the object is served WITH, not a property of the object -- so the
storage layout, the deletion mirror and every key already written stay exactly
as they are.

NON-LATIN NAMES SURVIVE THIS MODULE. That is not a nicety; ASCII normalisation
has already erased Chinese names once in this codebase, and an all-English test
suite noticed nothing. RFC 6266 exists precisely so we do not have to choose:
`filename*=UTF-8''...` carries the real name, and the plain `filename=` beside
it is a fallback for clients too old to read the first. The fallback is built by
REPLACING non-ASCII runs, never by dropping them, and if that leaves nothing
usable it becomes a generic name rather than an empty string -- an empty
`filename=` is worse than no fallback at all, because some clients honour it.
"""
import re
import unicodedata
from urllib.parse import quote

# Anything that has no business in a filename on Windows, macOS or Linux, plus
# the quote and backslash that would break out of the quoted `filename=` value
# and the CR/LF that would break out of the header itself.
_FORBIDDEN = re.compile(r'[\\/:*?"<>|\r\n\t\x00-\x1f\x7f]+')
_RUNS = re.compile(r"[\s_]+")
_NON_ASCII_RUN = re.compile(r"[^\x20-\x7e]+")
# A fallback made only of separators and the extension carries no information.
_EMPTY_STEM = re.compile(r"^[\s_.\-]*$")

_MAX_STEM = 120          # keeps the whole header well inside any client's limit


_UUIDISH = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def template_label(template_id, template_version, template_name=None):
    """`site-daily-v3`, or `report` when no template was named.

    THE NAME, NOT THE ID. A template that ships as a file in this repo has a
    slug for an id -- `personal-meeting` -- so using the id read perfectly well
    for as long as those were the only templates. A template written in the
    Library has a uuid, and the first report generated from one came out as

        Ben_UCPK2_df153e1c-fef4-4c0a-bdb3-84fbb507c2ec-v3_2026-08-12.docx

    which is not a filename, it is a database key with a date on the end. The
    id identifies the template to the system; the NAME is what the person who
    made it called it, and the filename is for them.

    Falls back to the id when no name travelled -- an old artifact, or the file
    templates, whose ids are readable anyway. And if a name reduces to nothing
    printable (it may be written entirely in a script this slug rule cannot
    represent), the id is better than an empty segment: display_name carries
    the real name through Content-Disposition regardless.

    The assembled path names no template -- it is not a generation at all --
    and calling that file `...-None-vNone...` would state something false about
    how it was made. It is just the report.
    """
    if not template_id:
        return "report"
    label = _tidy(template_name) if template_name else ""
    if not label:
        label = str(template_id)
    elif _UUIDISH.match(label):
        # A template somebody named after its own id. Unlikely, and harmless,
        # but the point of this function is not to print a uuid.
        label = str(template_id)
    if template_version is None:
        return label
    return "%s-v%s" % (label, template_version)


def _tidy(text):
    """Strip what no filesystem accepts, collapse runs, trim separators.

    NFC first, so a name typed with combining characters and the same name
    typed precomposed produce one filename rather than two that look identical.
    """
    text = unicodedata.normalize("NFC", str(text or ""))
    text = _FORBIDDEN.sub("_", text)
    text = _RUNS.sub("_", text)
    return text.strip("_. ")


def display_name(folder, template_id, template_version, date, ext=".docx",
                 template_name=None):
    """`{who}_{template}_{when}{ext}` -- the name a person would have given it.

    `folder` is the recording folder, which is how this system already spells a
    person (`Ben_UCPK2`), and `date` is the day the report is ABOUT, not the
    moment it was generated: two people comparing notes on the 10th should hold
    files with the same date in the name, whichever day each pressed the button.
    Regenerating therefore reproduces the same name, and the browser's own
    ` (1)` suffix says a second copy arrived -- which is true and is what the
    person expects to see.
    """
    parts = [_tidy(p) for p in
             (folder, template_label(template_id, template_version, template_name), date)]
    stem = "_".join(p for p in parts if p)
    if _EMPTY_STEM.match(stem):
        stem = "report"
    return stem[:_MAX_STEM] + ext


def _ascii_fallback(name):
    """The same name for a client that cannot read `filename*`.

    Every non-ASCII run becomes a single `_`. It is lossy and it is meant to
    be: the point is that a Chinese name degrades to `_ _2026-09-10.docx`
    rather than to nothing, and that the modern clients reading `filename*`
    beside it still get the real thing.

    Never returns an empty stem. A name that is entirely non-ASCII would
    otherwise collapse to just the extension, and a client honouring that would
    save a file called `.docx`.
    """
    fallback = _NON_ASCII_RUN.sub("_", name)
    stem, _, ext = fallback.rpartition(".")
    if not stem:                       # no dot at all
        stem, ext = fallback, ""
    stem = _RUNS.sub("_", stem).strip("_. ")
    if _EMPTY_STEM.match(stem):
        stem = "report"
    return stem + ("." + ext if ext else "")


def content_disposition(name):
    """An RFC 6266 header carrying `name`, readable by old and new clients.

    Both parameters are always emitted, even for a pure-ASCII name. Sending
    only one of them makes the behaviour depend on what the name happens to
    contain, and a path that only runs for Chinese names is a path nobody
    tests. `quote` percent-encodes to UTF-8, so the header itself is ASCII
    whatever the name is -- there is nothing here that can break a header.
    """
    ascii_name = _ascii_fallback(name)
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (
        ascii_name, quote(name, safe=""))
