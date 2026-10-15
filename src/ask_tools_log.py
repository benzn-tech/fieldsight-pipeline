"""Log-shape helpers shared by both sides of the Ask tools seam: `ask_tools` (the
out-of-VPC executor) and `rag_tools_dispatch` (in-VPC). Stdlib only, on purpose:
rag-search must not inherit ask_tools' lazy imports of the ask-agent (the lake
ownership guard follows every import, function-level ones included).

Logs carry argument NAMES and a caller hash, never a value (spec §3.4).
"""
from __future__ import annotations

import hashlib
from datetime import date

# Argument NAMES that may appear in logs (never values). Anything else is "?".
LOGGABLE_FIELDS = frozenset(("date_from", "date_to", "on_or_before", "site", "person",
                             "query", "k", "name", "metric"))


def sub_hash(sub) -> str:
    """Same as ask_voice_turn.sub_hash (sha256, 12 hex chars), so one caller's lines
    join across the voice worker, ask-agent and rag-search."""
    return hashlib.sha256(str(sub).encode("utf-8")).hexdigest()[:12]


def arg_shape(args):
    """(field names set, range length in days). Never a value."""
    args = args if isinstance(args, dict) else {}
    fields = ",".join(sorted(f if f in LOGGABLE_FIELDS else "?" for f in args))
    days = "-"
    try:
        if args.get("date_from"):
            days = str((date.fromisoformat(args.get("date_to") or args["date_from"])
                        - date.fromisoformat(args["date_from"])).days + 1)
    except (TypeError, ValueError):
        days = "?"
    return fields, days
