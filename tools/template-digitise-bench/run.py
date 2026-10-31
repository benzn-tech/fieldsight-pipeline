"""Template-digitise model bench: muse / DeepSeek against the shipped gemini (screen).

Spec: docs/superpowers/specs/2026-10-31-model-scope-and-config-update-design.md Part B1.

Owner-run, from benchenv (Pillow for template_vision's shrink; the key is the bench-only
OpenRouter key, never the prod one):

    PY=C:/Users/camil/wt/benchenv/Scripts/python.exe
    $PY tools/template-digitise-bench/run.py --inputs <dir> --out <dir> --dry-run
    $PY tools/template-digitise-bench/run.py --inputs <dir> --out <dir> \
        --openrouter-key-file <file> --confirm-calls <reads it printed>

<inputs> holds bicl.pdf / penina.pdf (convert.ps1) and their pages
<doc>_p<i>_clean.png / <doc>_p<i>_photo.jpg (raster.py). Both dirs must be outside the
repo and Dropbox.

Arms (spec B1): muse low, muse medium, deepseek@coreweave/fp8 off, deepseek@coreweave/fp8
low on the 4 image inputs x 3 reps; gemini as shipped, 1 rep (baseline); muse low on the
two PDFs x 3 reps. 4 x 12 + 4 + 6 = 58 reads. Step 0 first: one 1-page image read per
model / provider (3); an arm whose probe fails (refused, error, or an answer matching no
truth item: it ignored the image) is skipped.

Each arm runs in its own subprocess (`run.py --worker <job>`) with an environment built
from nothing: LLM_PROVIDER=qwen, QWEN_BASE_URL=OpenRouter, QWEN_API_KEY = the key file's,
TEMPLATE_DIGITISE_MODEL / _REASONING / _PROVIDER, no fallbacks -- set before llm_utils is
imported (its import-time constants). The worker calls `template_vision.read()`, the prod
prompt, shrink and JSON retry, unchanged; it only watches `llm_utils._post_with_retry` to
read each response's usage. OpenRouter dollars go to the `openrouter` pool of --ledger
(the benches' shared anthropic_ledger.json; default <out>/anthropic_ledger.json): checked
before every read, charged after it (usage.cost, else the pinned upper, never $0).

PR-2 gate: `TEMPLATE_DIGITISE_REASONING` / `_PROVIDER` are read by src only after PR-2
(call_llm_parts(reasoning=, provider=) + template_vision reading them). Without them every
arm but the shipped gemini would silently run as shipped, so those arms are refused
("needs PR-2") and only the baseline is runnable.

Exit codes: 0 done, 2 refused (arguments, cap, PR-2 for every arm), 3 the OpenRouter pool
ran out mid-run. Item text lives only in <out>/arms/; stdout carries ids and numbers.
"""
from __future__ import annotations

import argparse
import ast
import glob
import importlib.util
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SRC = os.path.join(REPO, "src")
WEB_BENCH = os.path.join(REPO, "tools", "web-answer-bench")
DROPBOX = "C:/Users/camil/Dropbox"

MUSE = "meta/muse-spark-1.3-contributor"
DEEPSEEK = "deepseek/deepseek-v4.1-flash"
GEMINI = "google/gemini-3.8-flash"
COREWEAVE = "coreweave/fp8"
OPENROUTER_URL = "https://openrouter.ai/api/v1"

IMAGE_INPUTS = ("bicl_clean", "bicl_photo", "penina_clean", "penina_photo")
PDF_INPUTS = ("bicl_pdf", "penina_pdf")
PROBE_INPUT = "bicl_clean"              # page 1 only
REPS = 3

# needs: the PR-2 capabilities an arm's env is read through.
ARMS = (
    {"id": "muse_low", "model": MUSE, "reasoning": "low", "provider": "",
     "inputs": IMAGE_INPUTS, "reps": REPS, "needs": ("reasoning",)},
    {"id": "muse_medium", "model": MUSE, "reasoning": "medium", "provider": "",
     "inputs": IMAGE_INPUTS, "reps": REPS, "needs": ("reasoning",)},
    {"id": "deepseek_off", "model": DEEPSEEK, "reasoning": "off", "provider": COREWEAVE,
     "inputs": IMAGE_INPUTS, "reps": REPS, "needs": ("reasoning", "provider")},
    {"id": "deepseek_low", "model": DEEPSEEK, "reasoning": "low", "provider": COREWEAVE,
     "inputs": IMAGE_INPUTS, "reps": REPS, "needs": ("reasoning", "provider")},
    {"id": "gemini_shipped", "model": GEMINI, "reasoning": "", "provider": "",
     "inputs": IMAGE_INPUTS, "reps": 1, "needs": ()},
    {"id": "muse_pdf_low", "model": MUSE, "reasoning": "low", "provider": "",
     "inputs": PDF_INPUTS, "reps": REPS, "needs": ("reasoning",)},
)

# Upper token estimate of one read, for the $ upper (pinned prices): a page image or PDF
# page at <= 2000 px is well under 2 500 input tokens on these models; the prompt ~600.
UPPER_TOKENS_PER_PAGE = 2500
UPPER_PROMPT_TOKENS = 600
ANSWER_TOKENS = 32000                   # template_vision.ANSWER_TOKENS
HEADROOM_TOKENS = 8000                  # PR-2 adds REASONING_HEADROOM_TOKENS with reasoning
READ_ATTEMPTS = 2                       # template_vision.read: one JSON retry
READ_DEADLINE_S = 300.0
WORKER_TIMEOUT_S = 3600

_PASSTHROUGH = ("SYSTEMROOT", "PATH", "TEMP", "TMP")
_PAGE = re.compile(r"_p(\d+)_")


def _load_web(name):
    key = "web_" + name
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, os.path.join(WEB_BENCH, name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    return sys.modules[key]


def _load_score():
    key = "tdb_score"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, os.path.join(HERE, "score.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    return sys.modules[key]


def _refuse(msg):
    print("template-digitise-bench: " + msg, file=sys.stderr)
    raise SystemExit(2)


def _inside(path, root):
    p = os.path.normcase(os.path.abspath(path)).rstrip("\\/")
    r = os.path.normcase(os.path.abspath(root)).rstrip("\\/")
    return p == r or p.startswith(r + os.sep)


# ---------------------------------------------------------------- PR-2 capability
def capabilities(src_dir=SRC):
    """What src can send, read from the source (nothing imported): `reasoning` /
    `provider` = call_llm_parts takes the keyword AND template_vision reads the env."""
    caps = {"reasoning": False, "provider": False}
    try:
        with open(os.path.join(src_dir, "llm_utils.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        with open(os.path.join(src_dir, "template_vision.py"), encoding="utf-8") as f:
            tv = f.read()
    except (OSError, SyntaxError):
        return caps
    params = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "call_llm_parts":
            params = {a.arg for a in node.args.args + node.args.kwonlyargs}
    caps["reasoning"] = "reasoning" in params and "TEMPLATE_DIGITISE_REASONING" in tv
    caps["provider"] = "provider" in params and "TEMPLATE_DIGITISE_PROVIDER" in tv
    return caps


def runnable(arm, caps):
    return all(caps.get(n) for n in arm["needs"])


# ---------------------------------------------------------------- inputs
def pages_of(inputs_dir, name):
    """[(path, content_type)] for an input name like bicl_photo / penina_pdf."""
    doc, variant = name.split("_", 1)
    if variant == "pdf":
        p = os.path.join(inputs_dir, doc + ".pdf")
        return [(p, "application/pdf")] if os.path.exists(p) else []
    ext, ctype = ("png", "image/png") if variant == "clean" else ("jpg", "image/jpeg")
    found = glob.glob(os.path.join(inputs_dir, f"{doc}_p*_{variant}.{ext}"))
    found.sort(key=lambda s: int(_PAGE.search(os.path.basename(s)).group(1)))
    return [(p, ctype) for p in found]


# The fixture forms' page counts (spike calls.json), for the $ upper before the
# pages exist.
KNOWN_PAGES = {"bicl": 3, "penina": 10}


def page_count(inputs_dir, name):
    doc = name.split("_")[0]
    # A PDF's pages = its clean images, as raster.py made them.
    n = len(pages_of(inputs_dir, doc + "_clean" if name.endswith("_pdf") else name))
    return n or KNOWN_PAGES.get(doc, 1)


def read_upper(model, pages, reasoning):
    W = _load_web("ledger")
    out = ANSWER_TOKENS + (HEADROOM_TOKENS if reasoning else 0)
    return READ_ATTEMPTS * W.or_upper(model, UPPER_PROMPT_TOKENS +
                                      UPPER_TOKENS_PER_PAGE * max(1, pages), out)


def plan(inputs_dir, caps):
    """{arms, probes, reads, runnable_reads, runnable_probes, usd_upper, runnable_usd_upper}."""
    arms, probes = [], {}
    for a in ARMS:
        ok = runnable(a, caps)
        upper = sum(read_upper(a["model"], page_count(inputs_dir, i), a["reasoning"]) * a["reps"]
                    for i in a["inputs"])
        arms.append(dict(a, inputs=list(a["inputs"]), needs=list(a["needs"]),
                         runnable=ok, reads=len(a["inputs"]) * a["reps"], usd_upper=upper))
        pk = probe_key(a)
        if pk not in probes:
            probes[pk] = {"key": pk, "arm": a["id"], "runnable": ok,
                          "usd_upper": read_upper(a["model"], 1, a["reasoning"])}
        else:
            probes[pk]["runnable"] = probes[pk]["runnable"] or ok
    p = {"arms": arms, "probes": list(probes.values())}
    p["reads"] = sum(a["reads"] for a in arms)
    p["runnable_reads"] = sum(a["reads"] for a in arms if a["runnable"])
    p["runnable_probes"] = sum(1 for x in p["probes"] if x["runnable"])
    p["usd_upper"] = sum(a["usd_upper"] for a in arms) + sum(x["usd_upper"] for x in p["probes"])
    p["runnable_usd_upper"] = (sum(a["usd_upper"] for a in arms if a["runnable"])
                               + sum(x["usd_upper"] for x in p["probes"] if x["runnable"]))
    return p


def probe_key(arm):
    return arm["model"] + ("@" + arm["provider"] if arm["provider"] else "")


# ---------------------------------------------------------------- subprocess env
def arm_env(arm, key, base=None):
    """The worker's whole environment. Never inherited: a parent DASHSCOPE_API_KEY,
    LLM_FALLBACK_MODELS or prod QWEN_API_KEY must not reach the read."""
    base = os.environ if base is None else base
    env = {k: base[k] for k in _PASSTHROUGH if base.get(k) is not None}
    env.update({
        "LLM_PROVIDER": "qwen",
        "QWEN_BASE_URL": OPENROUTER_URL,
        "QWEN_API_KEY": key,
        "TEMPLATE_DIGITISE_MODEL": arm["model"],
        "TEMPLATE_DIGITISE_REASONING": arm["reasoning"],
        "TEMPLATE_DIGITISE_PROVIDER": arm["provider"],
        "TEMPLATE_DIGITISE_PDF_MODEL": "",
        "TEMPLATE_DIGITISE_FALLBACK_MODELS": "",
        "PYTHONIOENCODING": "utf-8",
    })
    return env


def _spawn(cmd, env):
    return subprocess.run(cmd, env=env, timeout=WORKER_TIMEOUT_S, check=False).returncode


# ---------------------------------------------------------------- worker side
def _numeric(d):
    if not isinstance(d, dict):
        return {}
    out = {}
    for k, v in d.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = v
        elif isinstance(v, dict):
            out[k] = _numeric(v)
    return out


def call_cost(model, calls, est_in, est_out):
    """$ of one read's HTTP calls: usage.cost, else the pinned upper of its tokens; a
    call with no response at all (timeout, transport) at the estimate; non-200 free."""
    W = _load_web("ledger")
    usd = 0.0
    for c in calls:
        st, u = c.get("status"), c.get("usage") or {}
        if st is None:
            usd += W.or_upper(model, est_in, est_out)
        elif st == 200:
            tok = (u.get("prompt_tokens"), u.get("completion_tokens"))
            usd += W.or_cost({"usage": u}, model,
                             *(tok if any(tok) else (est_in, est_out)))
    return usd


def worker(job_path):
    """Runs in the arm's subprocess (env already set). Reads, writes one result file
    per read, charges the ledger. Never prints item text."""
    with open(job_path, encoding="utf-8") as f:
        job = json.load(f)
    sys.path.insert(0, SRC)
    import llm_utils                                     # env set by the parent
    import template_vision
    W = _load_web("ledger")
    calls = []
    real_post = llm_utils._post_with_retry

    def watched(url, body, headers, **kw):
        resp, err = real_post(url, body, headers, **kw)
        rec = {"status": getattr(resp, "status", None), "usage": {}}
        if resp is not None:
            try:
                rec["usage"] = _numeric(json.loads(resp.data.decode("utf-8")).get("usage"))
            except Exception:                            # noqa: BLE001 - counts only
                pass
        calls.append(rec)
        return resp, err

    llm_utils._post_with_retry = watched
    arm = job["arm"]
    out_dir = job["out_dir"]
    os.makedirs(out_dir, exist_ok=True)
    cap_hit = False
    for item in job["reads"]:
        if W.openrouter_room(job["ledger"], job["cap"]) <= 1e-9:
            cap_hit = True
            print(f"{arm['id']}: OpenRouter pool spent, stopping", flush=True)
            break
        pages = []
        for path, ctype in item["pages"]:
            with open(path, "rb") as f:
                pages.append({"body": f.read(), "content_type": ctype,
                              "name": os.path.basename(path)})
        del calls[:]
        started = time.monotonic()
        sections, msg = template_vision.read(pages, deadline=READ_DEADLINE_S)
        latency = time.monotonic() - started
        est_in = UPPER_PROMPT_TOKENS + UPPER_TOKENS_PER_PAGE * max(1, item["page_count"])
        cost = call_cost(arm["model"], calls, est_in,
                         ANSWER_TOKENS + (HEADROOM_TOKENS if arm["reasoning"] else 0))
        if cost:
            W.add_openrouter_spend(job["ledger"], cost)
        n_items = sum(len(s.get("items") or []) for s in sections or [] if isinstance(s, dict))
        res = {"arm": arm["id"], "model": arm["model"], "input": item["input"],
               "doc": item["input"].split("_")[0], "rep": item["rep"],
               "probe": bool(job.get("probe")), "ok": sections is not None,
               "error": msg, "cut_off": msg == template_vision.MSG_TOO_LONG,
               "latency_s": round(latency, 2), "pages": len(pages), "calls": list(calls),
               "cost_usd": round(cost, 8), "sections": sections}
        name = f"{item['input']}_rep{item['rep']}.json"
        with open(os.path.join(out_dir, name), "w", encoding="utf-8", newline="") as f:
            json.dump(res, f, ensure_ascii=False)
        print(f"{arm['id']} {item['input']} rep{item['rep']} "
              f"{'ok' if res['ok'] else 'error'} {latency:.1f}s items={n_items} "
              f"calls={len(calls)} ${cost:.4f}", flush=True)
    return 3 if cap_hit else 0


# ---------------------------------------------------------------- parent side
def validate(argv, repo_root=REPO):
    ap = argparse.ArgumentParser(prog="template-digitise-bench")
    ap.add_argument("--inputs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--openrouter-key-file")
    ap.add_argument("--openrouter-cap", type=float, default=None)
    ap.add_argument("--ledger", help="the benches' shared anthropic_ledger.json "
                    "(default <out>/anthropic_ledger.json)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--confirm-calls", type=int)
    a = ap.parse_args(argv)
    W = _load_web("ledger")
    if a.openrouter_cap is None:
        a.openrouter_cap = W.OPENROUTER_CAP_USD
    for flag, path in (("--inputs", a.inputs), ("--out", a.out)):
        for root, what in ((repo_root, "the repository"), (DROPBOX, "Dropbox")):
            if _inside(path, root):
                _refuse(f"{flag} must be outside {what}")
    if not os.path.isdir(a.inputs):
        _refuse("--inputs is not a directory")
    if a.openrouter_cap < 0:
        _refuse("--openrouter-cap must be >= 0")
    if not a.dry_run and not a.openrouter_key_file:
        # No env fallback here: QWEN_API_KEY in this shell is the prod key.
        _refuse("--openrouter-key-file is required for a live run")
    a.inputs, a.out = os.path.abspath(a.inputs), os.path.abspath(a.out)
    a.ledger = os.path.abspath(a.ledger or os.path.join(a.out, "anthropic_ledger.json"))
    return a


def _reads(arm, inputs_dir, probe=False):
    if probe:
        pages = pages_of(inputs_dir, PROBE_INPUT)[:1]
        return [{"input": PROBE_INPUT, "rep": 0, "pages": pages, "page_count": 1}]
    return [{"input": i, "rep": r, "pages": pages_of(inputs_dir, i),
             "page_count": page_count(inputs_dir, i)}
            for r in range(1, arm["reps"] + 1) for i in arm["inputs"]]


def _job(args, arm, out_dir, probe=False):
    job = {"arm": {k: arm[k] for k in ("id", "model", "reasoning", "provider")},
           "reads": _reads(arm, args.inputs, probe), "out_dir": out_dir, "probe": probe,
           "ledger": args.ledger, "cap": args.openrouter_cap}
    os.makedirs(os.path.join(args.out, "jobs"), exist_ok=True)
    path = os.path.join(args.out, "jobs", ("probe_" if probe else "") + arm["id"] + ".json")
    with open(path, "w", encoding="utf-8", newline="") as f:
        json.dump(job, f)
    return path


def _probe_passed(out_dir, truths):
    S = _load_score()
    path = os.path.join(out_dir, f"{PROBE_INPUT}_rep0.json")
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as f:
        res = json.load(f)
    return bool(res.get("ok")) and S.matches_any(truths["bicl"], res.get("sections") or [])


def run(args, *, caps=None, spawn=_spawn, key=None, stdout=sys.stdout, truths=None):
    W = _load_web("ledger")
    S = _load_score()
    caps = capabilities() if caps is None else caps
    p = plan(args.inputs, caps)
    room = W.openrouter_room(args.ledger, args.openrouter_cap)
    print(f"template-digitise plan: {len(p['arms'])} arms, {p['reads']} reads "
          f"(+{len(p['probes'])} step-0 probes), upper ${p['usd_upper']:.2f} at pinned "
          f"prices", file=stdout)
    for a in p["arms"]:
        print(f"  {a['id']:16} {a['model']}{'@' + a['provider'] if a['provider'] else ''} "
              f"reasoning={a['reasoning'] or '-'} reads={a['reads']} "
              f"${a['usd_upper']:.3f}{'' if a['runnable'] else '  needs PR-2'}", file=stdout)
    print(f"runnable now: {p['runnable_reads']} reads (+{p['runnable_probes']} probes), "
          f"upper ${p['runnable_usd_upper']:.2f}; openrouter pool "
          f"${W.openrouter_spent(args.ledger):.2f} of ${args.openrouter_cap:g}, "
          f"room ${room:.2f}", file=stdout)
    if p["runnable_usd_upper"] > room + 1e-9:
        print(f"refused: openrouter upper {p['runnable_usd_upper']:.2f} > room {room:.2f} "
              "(raise --openrouter-cap)", file=stdout)
        return 2
    if args.dry_run:
        return 0
    if args.confirm_calls != p["runnable_reads"]:
        print(f"refused: --confirm-calls {args.confirm_calls} != {p['runnable_reads']} "
              "runnable reads; nothing sent", file=stdout)
        return 2
    missing = [i for a in p["arms"] if a["runnable"] for i in a["inputs"]
               if not pages_of(args.inputs, i)]
    if missing:
        print("refused: no pages for " + ", ".join(sorted(set(missing))), file=stdout)
        return 2
    if key is None:
        key, _ = W.load_or_key(args.openrouter_key_file, "QWEN_API_KEY")
    truths = truths or S.load_truths()
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "plan.json"), "w", encoding="utf-8", newline="") as f:
        json.dump({k: v for k, v in p.items()}, f, indent=1)
    me = os.path.abspath(__file__)
    probes = {}
    for pr in p["probes"]:
        if not pr["runnable"]:
            continue
        arm = next(a for a in p["arms"] if a["id"] == pr["arm"])
        pdir = os.path.join(args.out, "probes", pr["arm"])
        code = spawn([sys.executable, me, "--worker", _job(args, arm, pdir, probe=True)],
                     arm_env(arm, key))
        probes[pr["key"]] = code == 0 and _probe_passed(pdir, truths)
        print(f"probe {pr['key']}: {'ok' if probes[pr['key']] else 'FAILED'}", file=stdout)
    with open(os.path.join(args.out, "probes.json"), "w", encoding="utf-8", newline="") as f:
        json.dump(probes, f, indent=1)
    cap_hit = False
    for arm in p["arms"]:
        if not arm["runnable"]:
            print(f"{arm['id']}: skipped, needs PR-2", file=stdout)
            continue
        if not probes.get(probe_key(arm)):
            print(f"{arm['id']}: skipped, step-0 probe failed", file=stdout)
            continue
        adir = os.path.join(args.out, "arms", arm["id"])
        code = spawn([sys.executable, me, "--worker", _job(args, arm, adir)],
                     arm_env(arm, key))
        if code == 3:
            cap_hit = True
            print(f"{arm['id']}: OpenRouter pool spent; stopping", file=stdout)
            break
    summary = S.summarize(args.out, p["arms"], truths)
    S.print_table(summary, stdout=stdout)
    return 3 if cap_hit else 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--worker"]:
        return worker(argv[1])
    return run(validate(argv))


if __name__ == "__main__":
    sys.exit(main())
