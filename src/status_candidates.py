"""The shared candidate rule for status suggestions and pick cards.

One place decides whether a re-read action item may still be offered for a suggested status:
the suggestion writer uses it when it writes, and org-api uses it when a person decides, so
the two can never disagree about which candidates are live."""
import uuid

TERMINAL = frozenset({"done", "closed"})


def canon_uuid(value):
    """Canonical string form of a uuid (any case, with or without braces/dashes), or None
    when `value` is missing or malformed -- never raises."""
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def drop_reason(row, site_id, suggested_status):
    """Why a re-read item must NOT be offered for `suggested_status` on `site_id`, or None
    when it may. Shared by status suggestions and pick-card candidates. `site_id` is
    already canonical (None when the event's was malformed, which never matches)."""
    if row is None:
        return "gone"
    if site_id is None or canon_uuid(row["site_id"]) != site_id:
        return "moved_site"
    if row["status"] in TERMINAL:
        return "terminal"
    if row["status"] == suggested_status:
        return "same"
    return None
