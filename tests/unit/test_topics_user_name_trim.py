"""user_name must be built with TRIM/CONCAT_WS, never a bare `||` concat.

Regression lock for a bug that bit three separate times in production:

`(u.first_name || ' ' || u.last_name)` is wrong in two distinct ways.

1. **Empty last_name -> trailing space.** An account like Ben_UCPK
   (first_name='Ben_UCPK', last_name='') yields "Ben_UCPK ". The frontend
   derives an S3 folder from the display name by replacing whitespace with
   underscores, so that became the folder "Ben_UCPK_", which matches
   nothing. Observed consequences: every photo presign 403'd
   (users/Ben_UCPK_/pictures/...), and the viewer's own unassigned tasks
   silently landed in the Team bucket instead of Mine because the derived
   owner folder never equalled the real one.

2. **NULL last_name -> the WHOLE expression is NULL.** In Postgres,
   'x' || ' ' || NULL evaluates to NULL, so such an account has no display
   name at all rather than just a first name.

`NULLIF(TRIM(CONCAT_WS(' ', first, last)), '')` fixes both: CONCAT_WS skips
NULLs, TRIM removes the stray separator, NULLIF keeps "no name at all"
representable as NULL. This is the pattern repositories/content_edits.py
already used correctly.

The frontend also trims defensively in folderName(), but the name should be
clean at its source so every consumer -- timeline, safety, quality,
evidence, and anything added later -- gets it right without knowing about
this trap.
"""

import io
import pathlib
import re

import pytest

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "repositories" / "topics.py"


@pytest.fixture(scope="module")
def source() -> str:
    return io.open(SRC, encoding="utf-8").read()


def test_no_naive_concat_for_user_name(source: str) -> None:
    """The bare `||` concat must not come back."""
    naive = re.findall(r"first_name\s*\|\|\s*' '\s*\|\|\s*u?\.?last_name", source)
    assert naive == [], (
        "user_name is being built with a bare `||` concat again. An empty "
        "last_name yields a trailing space (-> folder 'Name_'), and a NULL "
        "last_name makes the whole expression NULL. Use "
        "NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), '')."
    )


def test_user_name_uses_trim_concat_ws(source: str) -> None:
    """Every user_name projection uses the safe form."""
    safe = re.findall(
        r"NULLIF\(TRIM\(CONCAT_WS\(' ', u\.first_name, u\.last_name\)\), ''\) AS user_name",
        source,
    )
    assert len(safe) == 3, (
        f"expected 3 safe user_name projections, found {len(safe)}. If a query "
        "was added or removed, update this count deliberately -- do not switch "
        "the new one back to a bare concat."
    )


def test_every_user_name_alias_is_the_safe_form(source: str) -> None:
    """No user_name alias anywhere may use an unguarded expression."""
    for line in source.splitlines():
        if "AS user_name" not in line:
            continue
        assert "CONCAT_WS" in line, (
            f"a user_name projection does not use CONCAT_WS: {line.strip()}"
        )
