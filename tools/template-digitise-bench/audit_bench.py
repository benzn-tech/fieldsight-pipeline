"""Upload-audit noise floor (spec 2026-11-06 "Measuring it before prod", M3).

The audit emails the owner about every `problems` / `missed`. If it cries wolf on forms
that read correctly, its emails get ignored and it is worse than nothing. So before prod
turns it on, it runs three times on each known-good upload, and three times on each
known-bad one:

  good (pass bar: 0 of 15 runs give problems / missed)
    bicl            BICL site establishment form, rule read
    penina          Penina site inspection form, rule read
    council_docx    council Framing-Extended, the clean DOCX, rule read
    council_pdf     the same form's council original PDF, one muse read (see below)
    wall_framing    the new Wall Framing DOCX, rule read
  bad (pass bar: each caught at least 2 of 3)
    old_wall_framing  the Wall Framing form with one combined "Yes / No / N/A" column,
                      faked as the empty result the reader gave it before R1 -> missed
    bicl_deleted      the BICL draft with 3 rows deleted by hand -> problems
    bicl_signed       the BICL draft with "Signed:" and "Reinspection required?" added
                      -> problems

The old combined-column Wall Framing file is no longer on disk (the form was re-made with
separate Yes / No / N/A cells). Unless --old-wall-framing names a copy, it is rebuilt
here with python-docx: the layout the 2026-11-06 refusal had (`No. | Item | Yes / No /
N/A | Comment`, a merged section row, blank answer cells) around the new file's own rows.

The council PDF's draft is one read by the TEST reader (muse, reasoning low) made before
the audits and reused for all three reps, so M3 measures what runs: gemini auditing a
muse draft. That draft is the reader's, not a person's -- a `problems` on it is checked
by eye against the PDF before it counts as a false alarm. `--pdf-draft docx` uses the
clean DOCX's rule read instead (the same form): no reader call, and a draft known right.

Calls: 8 cases x 3 = 24 audits (each may retry once on a bad answer, so up to 48 HTTP
calls), plus 1 reader call for the PDF draft. Without --yes it prints that and exits,
and nothing is imported that could send anything. With --yes everything runs in one
subprocess (`audit_bench.py --worker <job>`) whose environment is built from nothing, as
run.py's arms are: a parent DASHSCOPE / prod QWEN_API_KEY / fallback chain must not
reach the calls. Dollars go to the `openrouter` pool of --ledger, checked before each
call and charged after (run.py's meter and ledger).

Item text (findings, drafts) lives only under --out, which must be outside the repo and
Dropbox; stdout carries case ids, verdicts, counts, seconds and dollars.

    PY=C:/Users/camil/wt/benchenv/Scripts/python.exe
    $PY tools/template-digitise-bench/audit_bench.py --out <dir>
    $PY tools/template-digitise-bench/audit_bench.py --out <dir> \
        --openrouter-key-file <TEST key file> --yes

Exit codes: 0 done (or the dry run), 2 refused (arguments, missing files, cap), 3 the
OpenRouter pool ran out mid-run.
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import sys
import time
from io import BytesIO

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SRC = os.path.join(REPO, "src")

MULTI_UNIT = "C:/Users/camil/Dropbox/fs-test/multi-unit/"
ADE = "C:/Users/camil/Dropbox/ADE/"
# (case input, env var, default path)
PATHS = (
    ("bicl", "AUDIT_BENCH_BICL", ADE + "12.15 - Site Establishment Checklist (BICL).docx"),
    ("penina", "AUDIT_BENCH_PENINA", ADE + "Penina Site Inspection Checklist.docx"),
    ("council_docx", "AUDIT_BENCH_COUNCIL_DOCX",
     MULTI_UNIT + "Framing Extended Inspection Checklist.docx"),
    ("council_pdf", "AUDIT_BENCH_COUNCIL_PDF",
     MULTI_UNIT + "Residential Framing Extended Inspection Checklist (council original).pdf"),
    ("wall_framing", "AUDIT_BENCH_WALL_FRAMING",
     MULTI_UNIT + "Wall Framing Inspection Checklist.docx"),
    ("old_wall_framing", "AUDIT_BENCH_OLD_WALL_FRAMING", None),   # None: rebuilt
)

REPS = 3
GOOD = ("bicl", "penina", "council_docx", "council_pdf", "wall_framing")
# case -> the verdict that counts as caught
BAD = {"old_wall_framing": "missed", "bicl_deleted": "problems", "bicl_signed": "problems"}
FALSE_ALARMS = ("problems", "missed")
DELETE_ROWS = 3
ADDED_ROWS = ("Signed:", "Reinspection required?")

READER_MODEL = "meta/muse-spark-1.3-contributor"   # TEST's reader (TEMPLATE_DIGITISE_MODEL)
READER_REASONING = "low"
AUDIT_MODEL = "google/gemini-3.8-flash"            # TEST defaults (deploy.yml ||)
AUDIT_ALT_MODEL = "meta/muse-spark-1.3-contributor"
AUDIT_ATTEMPTS = 2                                 # template_audit.run: one JSON retry
AUDIT_ANSWER_TOKENS = 32000                        # template_audit.ANSWER_TOKENS
PROMPT_TOKENS = 1200                               # template_audit.PROMPT, with headroom
CHARS_PER_TOKEN = 3                                # a pessimistic text estimate
# Worker-side copy of the empty result the reader gave a combined-column Word file
# before R1 (lambda_template_digitise._empty + MSG_WORD_NO_TABLE). Copied, not imported:
# the worker module pulls in boto3, and the bench must not touch AWS.
MSG_WORD_NO_TABLE = ("No checklist table found in the Word file — upload a PDF export "
                     "of it instead.")


def _load(name, path):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


def _run_py():
    """run.py: its env discipline, meter, spawn and path guards are reused, not copied."""
    return _load("tdb_run", os.path.join(HERE, "run.py"))


def _src():
    if SRC not in sys.path:
        sys.path.insert(0, SRC)


# ---------------------------------------------------------------- cases (pure, no model)
def paths(argv_paths=None, env=None):
    env = os.environ if env is None else env
    out = {}
    for name, var, default in PATHS:
        out[name] = (argv_paths or {}).get(name) or env.get(var) or default
    return out


def _empty_unreadable():
    return {"status": "unreadable", "sections": [], "counts": {"sections": 0, "items": 0},
            "flags": [], "route": "docx", "model": None, "pages": None,
            "reason": MSG_WORD_NO_TABLE}


def _counted(result):
    """Counts recomputed after a hand edit: the audit reads the sections, the table the
    counts, and both must agree."""
    secs = [s for s in result["sections"] if s.get("items")]
    result["sections"] = secs
    result["counts"] = {"sections": len(secs), "items": sum(len(s["items"]) for s in secs)}
    return result


def rule_read(path):
    """(rule-read result, render_text) for a Word file, as the worker's docx branch."""
    _src()
    import template_docx
    import template_draft
    with open(path, "rb") as f:
        body = f.read()
    draft = template_draft.clean(template_docx.read(BytesIO(body)))
    result = {"status": "done", **draft, "route": "docx", "model": None, "pages": None}
    return result, template_docx.render_text(BytesIO(body))


def rebuild_combined(result):
    """The old Wall Framing layout around `result`'s rows (see the module docstring)."""
    import docx
    d = docx.Document()
    t = d.add_table(rows=0, cols=4)

    def row(*cells, merge=False):
        r = t.add_row()
        if merge:
            r.cells[0].merge(r.cells[-1]).text = cells[0]
            return
        for c, text in zip(r.cells, cells):
            c.text = text

    row("No.", "Item", "Yes / No / N/A", "Comment")
    n = 0
    for section in result["sections"]:
        row(section["title"], merge=True)
        for item in section["items"]:
            n += 1
            row(str(n), item["text"], "", "")
    buf = BytesIO()
    d.save(buf)
    return buf.getvalue()


def delete_rows(result, k=DELETE_ROWS):
    """k rows deleted, spread across the draft (never a whole section's only row)."""
    out = copy.deepcopy(result)
    flat = [(si, ii) for si, s in enumerate(out["sections"]) for ii in range(len(s["items"]))
            if len(s["items"]) > 1]
    step = max(1, len(flat) // (k + 1))
    picks = [flat[step * (j + 1)] for j in range(k) if step * (j + 1) < len(flat)]
    for si, ii in sorted(picks, reverse=True):
        del out["sections"][si]["items"][ii]
    return _counted(out), len(picks)


def add_rows(result, rows=ADDED_ROWS):
    out = copy.deepcopy(result)
    out["sections"][-1]["items"].extend({"text": r, "flags": []} for r in rows)
    return _counted(out)


def build_cases(p, pdf_draft="read"):
    """{case: {source, result | None (the PDF's read, made in the worker), ...}} and a list
    of problems (missing files). Reads Word files with the rule reader only: free, local."""
    problems, cases = [], {}
    for name in ("bicl", "penina", "council_docx", "council_pdf", "wall_framing"):
        if not (p[name] and os.path.isfile(p[name])):
            problems.append(f"{name}: no file at {p[name]}")
    if p["old_wall_framing"] and not os.path.isfile(p["old_wall_framing"]):
        problems.append(f"old_wall_framing: no file at {p['old_wall_framing']}")
    if problems:
        return cases, problems
    reads = {n: rule_read(p[n]) for n in ("bicl", "penina", "council_docx", "wall_framing")}
    for n, (result, text) in reads.items():
        if not result["counts"]["items"]:
            problems.append(f"{n}: the rule reader found no rows (a known-good case must)")
        cases[n] = {"kind": "docx", "text": text, "result": result, "expect": "good"}
    cases["council_pdf"] = {"kind": "pdf", "pdf": p["council_pdf"], "expect": "good",
                            "result": (copy.deepcopy(reads["council_docx"][0])
                                       if pdf_draft == "docx" else None)}
    if p["old_wall_framing"]:
        _src()
        import template_docx
        with open(p["old_wall_framing"], "rb") as f:
            old_text = template_docx.render_text(BytesIO(f.read()))
        rebuilt = False
    else:
        _src()
        import template_docx
        old_text = template_docx.render_text(BytesIO(rebuild_combined(reads["wall_framing"][0])))
        rebuilt = True
    cases["old_wall_framing"] = {"kind": "docx", "text": old_text, "result": _empty_unreadable(),
                                 "expect": BAD["old_wall_framing"], "rebuilt": rebuilt}
    bicl, bicl_text = reads["bicl"]
    deleted, n_deleted = delete_rows(bicl)
    if n_deleted != DELETE_ROWS:
        problems.append(f"bicl_deleted: only {n_deleted} rows could be deleted")
    cases["bicl_deleted"] = {"kind": "docx", "text": bicl_text, "result": deleted,
                             "expect": BAD["bicl_deleted"]}
    cases["bicl_signed"] = {"kind": "docx", "text": bicl_text, "result": add_rows(bicl),
                            "expect": BAD["bicl_signed"]}
    return cases, problems


def plan(cases, pdf_draft="read"):
    """{audits, audit_http_upper, reader_calls, usd_upper, per_case}."""
    W = _run_py()._load_web("ledger")
    per_case, usd = {}, 0.0
    for name, c in cases.items():
        if c["kind"] == "docx":
            src_tokens = len(c["text"] or "") // CHARS_PER_TOKEN
        else:
            src_tokens = _run_py().UPPER_TOKENS_PER_PAGE * 15          # template_vision's page cap
        draft_tokens = len(json.dumps(c.get("result") or cases["council_docx"]["result"],
                                      ensure_ascii=False)) // CHARS_PER_TOKEN
        one = W.or_upper(AUDIT_MODEL, PROMPT_TOKENS + src_tokens + draft_tokens,
                         AUDIT_ANSWER_TOKENS)
        per_case[name] = {"reps": REPS, "usd_upper": AUDIT_ATTEMPTS * one * REPS}
        usd += per_case[name]["usd_upper"]
    reader = 1 if pdf_draft == "read" else 0
    if reader:
        rp = _run_py()
        usd += rp.read_upper(READER_MODEL, 15, READER_REASONING)
    audits = REPS * len(cases)
    return {"audits": audits, "audit_http_upper": AUDIT_ATTEMPTS * audits,
            "reader_calls": reader, "calls": audits + reader,
            "http_upper": AUDIT_ATTEMPTS * audits + rp_attempts(reader),
            "usd_upper": usd, "per_case": per_case}


def rp_attempts(reader):
    return reader * _run_py().READ_ATTEMPTS


# ---------------------------------------------------------------- worker (live only)
def bench_env(key, base=None):
    """The worker's whole environment, built from nothing (run.arm_env) plus the audit
    variables at TEST's values."""
    rp = _run_py()
    env = rp.arm_env({"model": READER_MODEL, "reasoning": READER_REASONING, "provider": ""},
                     key, base=base)
    env.update({"TEMPLATE_DIGITISE_AUDIT": "true",
                "TEMPLATE_DIGITISE_AUDIT_MODEL": AUDIT_MODEL,
                "TEMPLATE_DIGITISE_AUDIT_ALT_MODEL": AUDIT_ALT_MODEL,
                "TEMPLATE_DIGITISE_AUDIT_FALLBACK_MODELS": ""})
    return env


def worker(job_path):
    import logging
    rp = _run_py()
    with open(job_path, encoding="utf-8") as f:
        job = json.load(f)
    _src()
    import llm_utils                                     # env set by the parent
    W = rp._load_web("ledger")
    calls = []
    real = llm_utils.urllib3
    llm_utils.urllib3 = rp._Urllib3Meter(real, calls)
    # template_audit / template_vision log answer text on a bad answer: nothing to stderr.
    logging.disable(logging.CRITICAL)
    try:
        return _worker_runs(job, W, calls, rp)
    finally:
        logging.disable(logging.NOTSET)
        llm_utils.urllib3 = real


def _charge(W, job, calls, model, est_in, est_out, rp):
    cost = rp.call_cost(model, calls, est_in, est_out)
    if cost:
        W.add_openrouter_spend(job["ledger"], cost)
    return cost


def _worker_runs(job, W, calls, rp):
    import template_audit
    import template_draft
    import template_vision
    out = job["out"]
    os.makedirs(os.path.join(out, "audits"), exist_ok=True)
    cases = job["cases"]
    pdf = cases["council_pdf"]
    with open(pdf["pdf"], "rb") as f:
        pages = [{"body": f.read(), "content_type": "application/pdf",
                  "name": os.path.basename(pdf["pdf"])}]
    if pdf["result"] is None:
        if W.openrouter_room(job["ledger"], job["cap"]) <= 1e-9:
            print("OpenRouter pool spent before the PDF read; stopping", flush=True)
            return 3
        del calls[:]
        started = time.monotonic()
        raw, err = template_vision.read(pages, deadline=rp.READ_DEADLINE_S)
        cost = _charge(W, job, calls, READER_MODEL, rp.UPPER_TOKENS_PER_PAGE * 15,
                       AUDIT_ANSWER_TOKENS, rp)
        draft = template_draft.clean(raw) if not err else None
        print(f"pdf read: {'ok' if draft else 'error'} {time.monotonic() - started:.1f}s "
              f"items={draft['counts']['items'] if draft else 0} calls={len(calls)} "
              f"${cost:.4f}", flush=True)
        if not draft or not draft["counts"]["items"]:
            print("pdf read gave no draft; council_pdf is skipped", flush=True)
            cases.pop("council_pdf")
        else:
            pdf["result"] = {"status": "done", **draft, "route": "vision",
                             "model": template_vision.model_for(pages), "pages": None}
            with open(os.path.join(out, "council_pdf_draft.json"), "w", encoding="utf-8",
                      newline="") as f:
                json.dump(pdf["result"], f, ensure_ascii=False)
    for rep in range(1, REPS + 1):
        for name, c in cases.items():
            if W.openrouter_room(job["ledger"], job["cap"]) <= 1e-9:
                print("OpenRouter pool spent; stopping", flush=True)
                return 3
            source = ({"kind": "docx", "text": c["text"]} if c["kind"] == "docx"
                      else {"kind": "pdf", "pages": pages})
            del calls[:]
            record = template_audit.run(source, c["result"], deadline=rp.READ_DEADLINE_S)
            est_in = PROMPT_TOKENS + (len(c.get("text") or "") // CHARS_PER_TOKEN
                                      if c["kind"] == "docx" else rp.UPPER_TOKENS_PER_PAGE * 15)
            cost = _charge(W, job, calls, record.get("model") or AUDIT_MODEL, est_in,
                           AUDIT_ANSWER_TOKENS, rp)
            record.update(case=name, rep=rep, expect=c["expect"], calls=len(calls),
                          cost_usd=round(cost, 8),
                          draft_items=(c["result"].get("counts") or {}).get("items"))
            with open(os.path.join(out, "audits", f"{name}_rep{rep}.json"), "w",
                      encoding="utf-8", newline="") as f:
                json.dump(record, f, ensure_ascii=False)
            print(f"{name} rep{rep} {record['verdict']} {record.get('elapsed_s')}s "
                  f"calls={len(calls)} ${cost:.4f}", flush=True)
    return 0


# ---------------------------------------------------------------- summary
def summarize(records):
    """The table rows and the two pass bars, from the stored audit records."""
    rows = []
    for r in sorted(records, key=lambda r: (r["expect"] != "good", r["case"], r["rep"])):
        rows.append({"case": r["case"], "rep": r["rep"], "expect": r["expect"],
                     "verdict": r["verdict"], "draft_items": r.get("draft_items"),
                     "source_items": r.get("source_items"),
                     "missing": len(r.get("missing") or []), "extra": len(r.get("extra") or []),
                     "reworded": len(r.get("reworded") or []),
                     "split": len(r.get("split_or_merged") or []),
                     "dropped": len(r.get("dropped_claims") or []),
                     "seconds": r.get("elapsed_s"), "usd": r.get("cost_usd")})
    good = [x for x in rows if x["expect"] == "good"]
    alarms = sum(1 for x in good if x["verdict"] in FALSE_ALARMS)
    unsure = sum(1 for x in rows if x["verdict"] == "unsure")
    caught = {}
    for case, want in BAD.items():
        runs = [x for x in rows if x["case"] == case]
        caught[case] = (sum(1 for x in runs if x["verdict"] == want), len(runs))
    return {"rows": rows, "false_alarms": alarms, "good_runs": len(good), "unsure": unsure,
            "caught": caught,
            "pass_good": alarms == 0 and len(good) == REPS * len(GOOD),
            "pass_bad": all(n >= 2 for n, _ in caught.values())}


def print_table(s, stdout=sys.stdout):
    cols = ("case", "rep", "expect", "verdict", "draft_items", "source_items", "missing",
            "extra", "reworded", "split", "dropped", "seconds", "usd")
    print("| " + " | ".join(cols) + " |", file=stdout)
    print("|" + "---|" * len(cols), file=stdout)
    for r in s["rows"]:
        print("| " + " | ".join(str(r[c]) for c in cols) + " |", file=stdout)
    print(f"\nfalse alarms on known-good: {s['false_alarms']} of {s['good_runs']} "
          f"(bar: 0 of {REPS * len(GOOD)}) -> {'PASS' if s['pass_good'] else 'FAIL'}",
          file=stdout)
    for case, (n, of) in s["caught"].items():
        print(f"{case}: caught {n} of {of} (bar: 2 of {REPS}, as {BAD[case]})", file=stdout)
    print(f"known-bad -> {'PASS' if s['pass_bad'] else 'FAIL'}; unsure runs: {s['unsure']} "
          "(neither a pass nor a false alarm; counted, never emailed)", file=stdout)


def load_records(out):
    d = os.path.join(out, "audits")
    recs = []
    for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if name.endswith(".json"):
            with open(os.path.join(d, name), encoding="utf-8") as f:
                recs.append(json.load(f))
    return recs


# ---------------------------------------------------------------- parent
def validate(argv, repo_root=REPO):
    rp = _run_py()
    ap = argparse.ArgumentParser(prog="audit-bench")
    ap.add_argument("--out", required=True, help="outside the repo and Dropbox (item text)")
    for name, var, _ in PATHS:
        ap.add_argument("--" + name.replace("_", "-"), dest="path_" + name,
                        help=f"default: ${var}, else the built-in path")
    ap.add_argument("--pdf-draft", choices=("read", "docx"), default="read",
                    help="read: one muse read of the PDF (default); docx: the clean DOCX's "
                         "rule read, no reader call")
    ap.add_argument("--openrouter-key-file")
    ap.add_argument("--openrouter-cap", type=float, default=None)
    ap.add_argument("--ledger", help="the benches' shared anthropic_ledger.json "
                    "(default <out>/anthropic_ledger.json)")
    ap.add_argument("--yes", action="store_true", help="make the real calls")
    a = ap.parse_args(argv)
    W = rp._load_web("ledger")
    if a.openrouter_cap is None:
        a.openrouter_cap = W.OPENROUTER_CAP_USD
    for root, what in ((repo_root, "the repository"), (rp.DROPBOX, "Dropbox")):
        if rp._inside(a.out, root):
            rp._refuse(f"--out must be outside {what}")
    if a.yes and not a.openrouter_key_file:
        # No env fallback: QWEN_API_KEY in this shell is the prod key.
        rp._refuse("--openrouter-key-file is required with --yes")
    a.out = os.path.abspath(a.out)
    a.ledger = os.path.abspath(a.ledger or os.path.join(a.out, "anthropic_ledger.json"))
    a.paths = paths({n: getattr(a, "path_" + n) for n, _, _ in PATHS})
    return a


def run(args, *, spawn=None, key=None, stdout=sys.stdout):
    rp = _run_py()
    W = rp._load_web("ledger")
    cases, problems = build_cases(args.paths, args.pdf_draft)
    if problems:
        for msg in problems:
            print("refused: " + msg, file=stdout)
        return 2
    p = plan(cases, args.pdf_draft)
    room = W.openrouter_room(args.ledger, args.openrouter_cap)
    rebuilt = " (old_wall_framing rebuilt from the new file's rows)" if cases[
        "old_wall_framing"].get("rebuilt") else ""
    print(f"audit bench plan: {len(cases)} cases x {REPS} = {p['audits']} audits "
          f"(up to {p['audit_http_upper']} HTTP calls with the JSON retry) + "
          f"{p['reader_calls']} reader call for the PDF draft = {p['calls']} calls "
          f"(HTTP upper {p['http_upper']}); upper ${p['usd_upper']:.2f} at pinned prices"
          + rebuilt, file=stdout)
    for name, c in cases.items():
        items = (c["result"] or {}).get("counts", {}).get("items", "read")
        print(f"  {name:17} {c['kind']:4} expect={c['expect']:8} draft_items={items} "
              f"${p['per_case'][name]['usd_upper']:.3f}", file=stdout)
    print(f"openrouter pool ${W.openrouter_spent(args.ledger):.2f} of "
          f"${args.openrouter_cap:g}, room ${room:.2f}", file=stdout)
    if p["usd_upper"] > room + 1e-9:
        print(f"refused: upper {p['usd_upper']:.2f} > room {room:.2f} "
              "(raise --openrouter-cap)", file=stdout)
        return 2
    if not args.yes:
        print(f"dry run: {p['calls']} calls planned, none sent (pass --yes to run)",
              file=stdout)
        return 0
    if key is None:
        key, _ = W.load_or_key(args.openrouter_key_file, "QWEN_API_KEY")
    os.makedirs(args.out, exist_ok=True)
    job = {"out": args.out, "ledger": args.ledger, "cap": args.openrouter_cap,
           "cases": cases}
    job_path = os.path.join(args.out, "audit_job.json")
    with open(job_path, "w", encoding="utf-8", newline="") as f:
        json.dump(job, f, ensure_ascii=False)
    spawn = spawn or rp._spawn
    code = spawn([sys.executable, os.path.abspath(__file__), "--worker", job_path],
                 bench_env(key), stdout=stdout)
    s = summarize(load_records(args.out))
    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8", newline="") as f:
        json.dump(s, f, indent=1)
    print_table(s, stdout=stdout)
    return 3 if code == 3 else (0 if code == 0 else 2)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--worker"]:
        return worker(argv[1])
    return run(validate(argv))


if __name__ == "__main__":
    sys.exit(main())
