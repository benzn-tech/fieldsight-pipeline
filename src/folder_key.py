"""The one rule that mints a person's recording-folder key (users.folder_name).

The folder key is the S3 path segment every recording, transcript and extraction
is written under (users/{key}/...), and the join key the item-writer uses to find
the directory row. It is therefore minted once, here, from a display name -- never
re-derived later by a second cleanser that might disagree (prod, 2026-10-05:
`Deandre'_Alberts` was stored, the upload route wrote `Deandre__Alberts`, and 45
minutes of recording had no directory row).

Output alphabet is exactly what the upload route's `_safe_seg` keeps:
[A-Za-z0-9._-]. Display names are never altered by this -- only the key is.
"""
import re
import unicodedata

_DROPPED = {"'", "’"}        # apostrophes vanish: O'Brien -> OBrien
_OUTSIDE = re.compile(r"[^A-Za-z0-9._-]")
_HAS_ALNUM = re.compile(r"[A-Za-z0-9]")
KEY_RE = re.compile(r"[A-Za-z0-9._-]+")   # use .fullmatch (never .match: no `$`)


def folder_key(display_name):
    """A folder key for `display_name`, or None if nothing usable survives
    (e.g. a CJK-only name) -- the caller then falls back to `u_<user_id[:8]>`.

    Idempotent: folder_key(folder_key(x)) == folder_key(x)."""
    text = unicodedata.normalize("NFKD", (display_name or "").strip())
    text = "".join(c for c in text
                   if not unicodedata.combining(c) and c not in _DROPPED)
    # Leading underscores are what a dropped leading character (a CJK first name)
    # leaves behind; trailing (`Ben_`, a NULL last name) and inner (`A__B`) stay.
    key = _OUTSIDE.sub("_", text).lstrip("_")
    return key if _HAS_ALNUM.search(key) else None


def fallback_key(user_id):
    """The folder a person gets when their name yields no key."""
    return f"u_{str(user_id)[:8]}" if user_id else None
