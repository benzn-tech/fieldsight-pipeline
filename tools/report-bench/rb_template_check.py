"""Is the S3 daily-report template prod serves the git copy (v3.0)? (spec §2.2)

    python rb_template_check.py --debug <out>/inputs/R/<folder>_<date>.debug.json --out DIR

The served prompt is read from the `prompt` field of an OWNER-FOLDER
`reports/<date>/<folder>/daily_report_debug.json` (never `_summary`). The git
template (`config/prompt_templates.json` daily_report.prompt + system_context)
becomes a regex: static text from `string.Formatter().parse` (so `{{` / `}}`
are already `{` / `}`), each `{field}` -> a lazy DOTALL group, joined as
`f"{system_context}\\n\\n{body}"` (lambda_report_generator.build_daily_prompt).

Exit 0 when identical. Exit 2 when it differs: `<out>/r_template_diff.json`
gets the indices of the git static segments the served prompt lacks and
hashes -- no text; R cells then run only with `--r-template <file>` (the
served template, reconstructed by the owner in P1, its sha256 recorded): the
debug copy wins.
"""
from __future__ import annotations

import argparse
import os
import re
import string
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import rb_common  # noqa: E402

GIT_TEMPLATES = os.path.join(rb_common.REPO, "config", "prompt_templates.json")


def static_segments(template: str, system_context: str | None = None) -> list[str]:
    segs = [lit for lit, _f, _s, _c in string.Formatter().parse(template)]
    if system_context is not None:
        segs[0] = f"{system_context}\n\n" + segs[0]
    return segs


def _regex(template, system_context):
    parts = []
    head = f"{system_context}\n\n" if system_context is not None else ""
    for i, (lit, field, _spec, _conv) in enumerate(string.Formatter().parse(template)):
        parts.append(re.escape((head if i == 0 else "") + lit))
        if field is not None:
            parts.append(f"(?P<f{i}>.*?)")
    return re.compile("".join(parts), re.DOTALL)


def compare(template: str, prompt: str, system_context: str | None = None) -> dict:
    identical = _regex(template, system_context).fullmatch(prompt) is not None
    missing, pos = [], 0
    for i, seg in enumerate(static_segments(template, system_context)):
        if not seg:
            continue
        at = prompt.find(seg, pos)
        if at < 0:
            missing.append(i)
        else:
            pos = at + len(seg)
    return {"identical": identical, "missing_static_segments": missing,
            "debug_prompt_sha256": rb_common.sha256_text(prompt)}


def check(debug_json_path: str, git_templates: str = GIT_TEMPLATES) -> dict:
    rec = rb_common.read_json(debug_json_path)
    if rec.get("user_name") in ("_summary", None, ""):
        raise PermissionError("only an owner-folder debug record is read, never _summary")
    tm = rb_common.read_json(git_templates)
    daily = tm["daily_report"]
    out = compare(daily["prompt"], rec["prompt"], daily.get("system_context"))
    out["git_version"] = tm.get("_version")
    out["git_template_sha256"] = rb_common.sha256_text(daily["prompt"])
    out["folder"], out["date"] = rec.get("user_name"), rec.get("target_date")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--git-templates", default=GIT_TEMPLATES)
    a = ap.parse_args(argv)
    out = rb_common.check_out_dir(a.out)
    r = check(a.debug, a.git_templates)
    print("identical" if r["identical"] else "DIFFERS", "git", r["git_version"],
          "missing_segments", r["missing_static_segments"], "debug", r["debug_prompt_sha256"][:12])
    if not r["identical"]:
        rb_common.write_json(os.path.join(out, "r_template_diff.json"), r)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
