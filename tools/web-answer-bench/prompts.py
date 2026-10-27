"""Prompt texts of the matrix, one file each, pinned to where they came from.

Spec: docs/superpowers/specs/2026-10-27-claude-web-answer-bench-design.md §4.4.

  draft_v1   DRAFT_PROMPT, 518900a8 (unchanged since; == web_answer.DRAFT_PROMPT)
  verify_v1  VERIFY_PROMPT, 518900a8 ("colleague")
  verify_v2  VERIFY_PROMPT, b8c96342 (shipped; == web_answer.VERIFY_PROMPT)
  web_v1     WEB_PROMPT, 518900a8^
  merged_v1  new: draft_v1's rules + verify_v2's output rules, no draft reference

The files are `str.format` templates (`{question}`, and `{draft}` for verify).
A test pins draft_v1 / verify_v2 to the shipped constants, so a prompt edit in
src/ that is not mirrored here fails CI instead of silently benching old text.
"""
from __future__ import annotations

import hashlib
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(HERE, "prompts")

# id -> step. Not every file is a candidate: grader_v1 lives here too.
CANDIDATES = {
    "draft_v1": "draft",
    "verify_v1": "verify",
    "verify_v2": "verify",
    "web_v1": "single",
    "merged_v1": "single",
}

_ID = re.compile(r"^[a-z0-9_]+$")


def load(pid):
    """The text of prompts/<pid>.txt, exactly as stored (no newline translation)."""
    path = os.path.join(DIR, pid + ".txt")
    if not _ID.match(pid or "") or not os.path.exists(path):
        raise KeyError(pid)
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def sha(pid):
    return hashlib.sha256(load(pid).encode("utf-8")).hexdigest()
