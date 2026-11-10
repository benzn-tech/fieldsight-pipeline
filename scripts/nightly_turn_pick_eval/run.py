"""Run the nightly turn pick (J2) over Track C's built set (N0 pre-registration §2-§4).

    # count the calls; makes none and constructs no client
    python -m scripts.nightly_turn_pick_eval.run --split fit --reps 3 \
        --out scripts/nightly_turn_pick_eval/runs/fit_v0 --dry-run

    # the paid run (only after the owner approved the count)
    python -m scripts.nightly_turn_pick_eval.run --split fit --reps 3 \
        --out scripts/nightly_turn_pick_eval/runs/fit_v0
    python -m scripts.nightly_turn_pick_eval.run --split heldout --reps 3 \
        --out scripts/nightly_turn_pick_eval/runs/heldout --fit-run .../runs/fit_v1

One call per session per rep. The request, the prompt, the answer parsing and the guards are
`src/nightly_turn_pick.py`'s own; nothing is re-derived here. Every (session, rep) outcome is
appended as one line to `log.jsonl` the moment it returns, a failed call included, and
`--resume` re-calls only the pairs that have no line. `rep<k>.jsonl`, `sessions.jsonl` and the
totals in `meta.json` are rebuilt from that log. Rows carry refs, kinds, turn indexes and
outcomes only -- never item text, turn text or the model's answer text.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import nightly_turn_pick as ntp

from scripts.item_support_eval import score as tc_score
from scripts.item_support_eval import store

MODEL = "claude-sonnet-5-5"
MAX_TOKENS = 16000
MAX_RETRIES = 2
EFFORT = "high"
EXPECTED_MANIFEST = "38e50e20da1202fccdd29a9a819e08c51e6b644a514d513f380cb8b861eec2e3"

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_DIR = PACKAGE_DIR.parents[1]
RUNS_DIR = PACKAGE_DIR / "runs"
RESULTS_DIR = PACKAGE_DIR / "results"
WORDING_PATH = PACKAGE_DIR / "WORDING.md"
DEFAULT_DATA = r"C:\Users\camil\wt\track-c-eval\scripts\item_support_eval\data"
DEFAULT_LABELS = r"C:\Users\camil\wt\track-c-eval\scripts\item_support_eval\labels"

SOURCES = ("src/nightly_turn_pick.py", "src/item_support.py", "src/item_text_match.py")
WORDING_FREE = "src/nightly_turn_pick.py"     # may differ fit -> held-out (wording only)
SANS_KEY = "nightly_turn_pick_sans_instruction"   # ... which this hash proves
FAILED_PICK = "__failed__"
HELDOUT_REPS = 3
MAX_WORDINGS = 3          # v0 + at most 2 revisions
_WORDING_LINE = re.compile(r"^prompt_hash:\s*([0-9a-f]{12})\s*$", re.MULTILINE)
_RESUME_KEYS = ("model", "prompt_hash", "split", "reps", "hashes", "manifest_digest",
                "max_tokens", "max_retries", "effort")


def refuse(msg):
    raise SystemExit(f"Refusing: {msg}")


class SetupError(RuntimeError):
    """401/403/404 from the API: a setup problem, not an outcome. The run aborts and the
    (session, rep) is not recorded; fix the setup and --resume."""


# ---------------------------------------------------------------------------------------
# Guarded writes (only under runs/<name>/)
# ---------------------------------------------------------------------------------------

def require_runs_dir(path) -> Path:
    resolved, runs = Path(path).resolve(), Path(RUNS_DIR).resolve()
    if runs not in resolved.parents:
        refuse(f"writes go under {runs}/<run name>/ only, not {resolved}")
    return resolved


def write_json(path, obj):
    path = require_runs_dir(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8", newline="\n")


def write_jsonl(path, rows):
    path = require_runs_dir(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(store.dumps(row) + "\n")


def append_jsonl(path, row):
    path = require_runs_dir(path)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(store.dumps(row) + "\n")
        f.flush()
        os.fsync(f.fileno())


# ---------------------------------------------------------------------------------------
# Set, gold, hashes
# ---------------------------------------------------------------------------------------

def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sans_instruction_hash(source):
    """sha256 of a module source with the INSTRUCTION assignment's value cut out (line
    endings normalised), so a held-out run can prove the module changed in wording only.
    Offsets are cut in UTF-8 bytes, which is what `ast` col offsets count."""
    data = source.replace("\r\n", "\n").encode("utf-8")
    for node in ast.parse(data).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "INSTRUCTION" for t in node.targets):
            lines = data.splitlines(keepends=True)
            v = node.value
            start = sum(len(x) for x in lines[:v.lineno - 1]) + v.col_offset
            end = sum(len(x) for x in lines[:v.end_lineno - 1]) + v.end_col_offset
            data = data[:start] + data[end:]
            break
    else:
        raise RuntimeError("no INSTRUCTION assignment in the module source")
    return hashlib.sha256(data).hexdigest()


def file_hashes(data_dir, labels_dir):
    out = {rel: sha256_file(REPO_DIR / rel) for rel in SOURCES}
    out[SANS_KEY] = sans_instruction_hash(
        (REPO_DIR / WORDING_FREE).read_bytes().decode("utf-8"))
    out["labels/done.agent.jsonl"] = sha256_file(Path(labels_dir, "done.agent.jsonl"))
    out["data/manifest.json"] = sha256_file(Path(data_dir, "manifest.json"))
    return out


def load_split(data_dir, labels_dir, split):
    """The split's sessions, after checking the frozen manifest and that the refs this
    harness builds are exactly the gold refs of that split."""
    digest = store.manifest_digest(data_dir)
    if digest != EXPECTED_MANIFEST:
        refuse(f"data manifest digest {digest} is not the pre-registered {EXPECTED_MANIFEST}")
    sessions = store.load_sessions(data_dir, split=split)
    if not sessions:
        refuse(f"no {split} sessions under {data_dir}")
    built = {it["stable_id"] for s in sessions for it in session_items(s)}
    gold = tc_score.gold_by_ref(store.read_jsonl(Path(labels_dir, "done.agent.jsonl")))
    srefs = {s["ref"] for s in sessions}
    gold_refs = {r for r in gold if r.split(":")[0] in srefs}
    if built != gold_refs:
        refuse(f"built refs != gold refs for {split}: {len(built - gold_refs)} without gold, "
               f"{len(gold_refs - built)} gold refs not built")
    return sessions, digest


def last_wording_hash(path=None):
    p = Path(path or WORDING_PATH)
    found = _WORDING_LINE.findall(p.read_text(encoding="utf-8")) if p.exists() else []
    return found[-1] if found else None


def check_wording():
    p = Path(WORDING_PATH)
    logged = _WORDING_LINE.findall(p.read_text(encoding="utf-8")) if p.exists() else []
    if len(logged) > MAX_WORDINGS:
        refuse(f"{WORDING_PATH} logs {len(logged)} wordings; v0 plus at most 2 revisions "
               f"are allowed (pre-registration §5)")
    last = last_wording_hash()
    if last != ntp.PROMPT_HASH:
        refuse(f"PROMPT_HASH {ntp.PROMPT_HASH} is not the last entry of {WORDING_PATH} "
               f"({last}); log the wording before running it (pre-registration §5)")


def other_runs(exclude=None):
    """(run dir, meta) for every runs/*/meta.json except `exclude`."""
    out = []
    ex = Path(exclude).resolve() if exclude else None
    for m in sorted(Path(RUNS_DIR).glob("*/meta.json")):
        if ex is not None and m.parent.resolve() == ex:
            continue
        out.append((m.parent, store.read_json(m)))
    return out


def check_fit_runs_frozen(hashes, exclude=None):
    """Every complete fit run has the current non-wording hashes: only INSTRUCTION may
    change across the fit half (pre-registration §5)."""
    for d, m in other_runs(exclude):
        if m.get("split") != "fit" or not m.get("complete"):
            continue
        for rel, h in hashes.items():
            if rel != WORDING_FREE and (m.get("hashes") or {}).get(rel) != h:
                refuse(f"{rel} differs from fit run {d.name}; only the wording may change "
                       f"across fit runs")


def check_no_other_heldout(out):
    for d, m in other_runs(out):
        if m.get("split") == "heldout":
            refuse(f"a held-out run already exists ({d.name}); the held-out half is run once")


def check_fit_run(fit_run, hashes):
    """A held-out run uses the last fit run's wording and identical inputs (§5, hashes)."""
    if not fit_run:
        refuse("a held-out run needs --fit-run (the last fit run)")
    p = Path(fit_run, "meta.json")
    if not p.exists():
        refuse(f"no fit run meta at {p}")
    fm = store.read_json(p)
    if fm.get("split") != "fit" or not fm.get("complete"):
        refuse(f"{fit_run} is not a complete fit run")
    if fm.get("prompt_hash") != ntp.PROMPT_HASH:
        refuse(f"fit run wording {fm.get('prompt_hash')} != current PROMPT_HASH "
               f"{ntp.PROMPT_HASH}; the held-out half uses the last wording run on fit")
    for rel, h in hashes.items():
        if rel != WORDING_FREE and (fm.get("hashes") or {}).get(rel) != h:
            refuse(f"{rel} differs from the fit run's recorded hash")


# ---------------------------------------------------------------------------------------
# Request and rows
# ---------------------------------------------------------------------------------------

def session_items(session):
    """Task 1 request items for every Track C item, in `store.iter_items` order."""
    out = []
    for ti, list_name, ci, child, topic in store.iter_items(session["topics"]):
        out.append({"stable_id": store.item_ref(session["ref"], ti, list_name, ci),
                    "row_id": None, "kind": list_name, "topic_index": ti,
                    "time_range": topic.get("time_range"),
                    "text": store.item_text(list_name, child)})
    return out


def prepare(session):
    """{session, items, request, reasons}: reasons maps each item without a prompt slot to
    no_text / no_window / over_cap, classified by `build_request` itself."""
    turns, date = session["turns"], session["date"]
    items = session_items(session)
    request = ntp.build_request(items, turns, date)
    kept = {it["stable_id"] for it in request["items"]}
    reasons = {}
    for it in items:
        if it["stable_id"] in kept:
            continue
        sk = ntp.build_request([it], turns, date)["skipped"]
        reasons[it["stable_id"]] = ("no_text" if sk["no_text"] else
                                    "no_window" if sk["no_window"] else "over_cap")
    counts = Counter(reasons.values())
    if {k: counts.get(k, 0) for k in request["skipped"]} != request["skipped"]:
        raise RuntimeError(f"skip classification disagrees with build_request for "
                           f"session {session['ref']}")
    return {"session": session, "items": items, "request": request, "reasons": reasons}


def _raw_pick(answers):
    """The model's answer for one alias: its turn string, null when absent or null; an alias
    answered more than once is encoded as the answers joined by '|' (null as 'null')."""
    if not answers:
        return None
    if len(answers) == 1:
        return answers[0]
    return "|".join("null" if a is None else a for a in answers)


def session_rows(prep, picks=None, failed=False):
    s = prep["session"]
    turns = s["turns"]
    guard_rows, alias_of, answered = {}, {}, defaultdict(list)
    if picks is not None:
        guard_rows = {r["stable_id"]: r for r in ntp.apply_guards(prep["request"], picks, turns)}
        alias_of = {it["stable_id"]: it["alias"] for it in prep["request"]["items"]}
        for p in picks:
            answered[p["item"]].append(p["turn"])
    out = []
    for it in prep["items"]:
        ref = it["stable_id"]
        row = {"item_ref": ref, "session": s["ref"], "kind": it["kind"],
               "lang": store.language(it["text"]), "raw_pick": None, "turn_index": None,
               "outcome": None, "guard": None, "cross_language": False,
               "script_unknown": False}
        if ref in prep["reasons"]:
            row["outcome"] = f"skipped_{prep['reasons'][ref]}"
        elif failed or picks is None:
            row.update(raw_pick=FAILED_PICK, outcome="call_failed")
        else:
            g = guard_rows[ref]
            idx = g["turn_index"]
            row.update(raw_pick=_raw_pick(answered.get(alias_of[ref])), turn_index=idx,
                       outcome=g["outcome"], guard=g["guard"],
                       cross_language=bool(g["cross_language"]))
            if idx is not None:
                row["script_unknown"] = "unknown" in (
                    ntp.script_of(it["text"]), ntp.script_of(turns[idx].get("text") or ""))
        out.append(row)
    return out


# ---------------------------------------------------------------------------------------
# The model call (the only code that touches the SDK)
# ---------------------------------------------------------------------------------------

def make_client():
    """(client, APIError class, setup-error classes). Imports the SDK here, so nothing else
    needs it installed."""
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        refuse("ANTHROPIC_API_KEY is not set")
    import anthropic
    return (anthropic.Anthropic(max_retries=MAX_RETRIES), anthropic.APIError,
            (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
             anthropic.NotFoundError))


def _err_name(e):
    status = getattr(e, "status_code", None)
    return type(e).__name__ + (f":{status}" if status is not None else "")


def call_model(client, api_error, prompt, setup_errors=()):
    """{text, stop_reason, input_tokens, output_tokens, error}. No sampling parameters, no
    thinking parameter (adaptive is the model default), no fallbacks, no betas. A setup error
    (401/403/404) raises SetupError; any other APIError is a failed call."""
    try:
        resp = client.messages.create(model=MODEL, max_tokens=MAX_TOKENS,
                                      output_config={"effort": EFFORT},
                                      messages=[{"role": "user", "content": prompt}])
    except tuple(setup_errors) as e:
        raise SetupError(_err_name(e)) from None
    except api_error as e:
        return {"text": None, "stop_reason": None, "input_tokens": 0, "output_tokens": 0,
                "error": _err_name(e)}
    usage = getattr(resp, "usage", None)
    return {"text": "".join(b.text for b in resp.content if getattr(b, "type", None) == "text"),
            "stop_reason": resp.stop_reason,
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            "error": None}


def run_pair(prep, rep, client, api_error, setup_errors=()):
    """One (session, rep): the log entry, with the item rows. Never raises for a failed call
    (only SetupError, which is not an outcome)."""
    prompt = ntp.render_prompt(prep["request"], prep["session"]["turns"])
    res = call_model(client, api_error, prompt, setup_errors)
    failure, picks = None, None
    if res["error"]:
        failure = "api_error"
    elif res["stop_reason"] in ("refusal", "max_tokens"):
        failure = res["stop_reason"]
    else:
        try:
            picks = ntp.parse_answer(res["text"])
        except ValueError:
            failure = "parse"
    aliases = {it["alias"] for it in prep["request"]["items"]}
    return {"session": prep["session"]["ref"], "rep": rep,
            "status": "failed" if failure else "ok", "failure": failure,
            "error": res["error"], "stop_reason": res["stop_reason"],
            "input_tokens": res["input_tokens"], "output_tokens": res["output_tokens"],
            "unknown_aliases": 0 if picks is None else
            sum(1 for p in picks if p["item"] not in aliases),
            "rows": session_rows(prep, picks, failed=failure is not None)}


def no_call_entry(prep, rep):
    return {"session": prep["session"]["ref"], "rep": rep, "status": "no_call",
            "failure": None, "error": None, "stop_reason": None, "input_tokens": 0,
            "output_tokens": 0, "unknown_aliases": 0, "rows": session_rows(prep)}


# ---------------------------------------------------------------------------------------
# Log, resume, totals
# ---------------------------------------------------------------------------------------

def load_log(out):
    """Recorded entries. A torn last line (a crash mid-write) is dropped and the file
    rewritten without it; a bad line anywhere else refuses."""
    p = Path(out, "log.jsonl")
    if not p.exists():
        return []
    lines = [x for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    entries = []
    for i, line in enumerate(lines):
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            if i != len(lines) - 1:
                refuse(f"{p} line {i + 1} is not JSON")
            write_jsonl(p, entries)
    dup = [k for k, n in Counter((e["session"], e["rep"]) for e in entries).items() if n > 1]
    if dup:
        refuse(f"{p} holds duplicate (session, rep) entries for {len(dup)} pair(s); a "
               f"recorded outcome is never re-called, so the log is not trusted")
    return entries


def execute(todo, preps, client, api_error, workers, record, setup_errors=()):
    """Call each (ref, rep) on `workers` threads; `record` runs on this thread only, so the
    log is written serially. On any exception the pending calls are cancelled, every call
    that did return is still recorded, and the exception propagates."""
    if not todo:
        return
    pool = ThreadPoolExecutor(max_workers=workers)
    futures = [pool.submit(run_pair, preps[ref], rep, client, api_error, setup_errors)
               for ref, rep in todo]
    recorded = set()
    try:
        for f in as_completed(futures):
            record(f.result())
            recorded.add(f)
    except BaseException:
        pool.shutdown(wait=True, cancel_futures=True)
        for f in futures:
            if f not in recorded and f.done() and not f.cancelled() and f.exception() is None:
                record(f.result())
        raise
    pool.shutdown(wait=True)


def finalize(out, meta, entries, refs, reps):
    by_rep = defaultdict(list)
    for e in entries:
        by_rep[e["rep"]].extend(e["rows"])
    for k in range(1, reps + 1):
        write_jsonl(Path(out, f"rep{k}.jsonl"), sorted(by_rep[k], key=lambda r: r["item_ref"]))
    write_jsonl(Path(out, "sessions.jsonl"),
                sorted(({k: v for k, v in e.items() if k != "rows"} for e in entries),
                       key=lambda e: (e["rep"], e["session"])))
    called = [e for e in entries if e["status"] != "no_call"]
    done = {(e["session"], e["rep"]) for e in entries}
    meta.update(
        calls=len(called), failed_calls=sum(1 for e in called if e["status"] == "failed"),
        failures=dict(sorted(Counter(e["failure"] for e in called if e["failure"]).items())),
        unknown_aliases=sum(e["unknown_aliases"] for e in called),
        usage={"input_tokens": sum(e["input_tokens"] for e in called),
               "output_tokens": sum(e["output_tokens"] for e in called)},
        complete=all((r, k) in done for r in refs for k in range(1, reps + 1)),
        updated_at=_now())
    write_json(Path(out, "meta.json"), meta)
    return meta


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--split", choices=("fit", "heldout"), required=True)
    p.add_argument("--reps", type=int, required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--fit-run", default=None)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--data", default=DEFAULT_DATA)
    p.add_argument("--labels", default=DEFAULT_LABELS)
    a = p.parse_args(argv)
    if a.reps < 1 or a.workers < 1:
        refuse("--reps and --workers must be >= 1")
    if a.split == "heldout" and a.reps != HELDOUT_REPS:
        refuse(f"a held-out run has exactly {HELDOUT_REPS} reps (bar 4 needs them)")

    out = require_runs_dir(a.out)
    sessions, digest = load_split(a.data, a.labels, a.split)
    preps = {s["ref"]: prepare(s) for s in sessions}
    calling = sorted(r for r, pr in preps.items() if pr["request"]["items"])
    skipped = Counter(x for pr in preps.values() for x in pr["reasons"].values())
    if a.dry_run:
        print(f"sessions={len(calling)} calls={len(calling) * a.reps} "
              f"skipped_no_window={skipped['no_window']} skipped_over_cap={skipped['over_cap']} "
              f"skipped_no_text={skipped['no_text']} "
              f"sessions_without_call={len(preps) - len(calling)}")
        return {"sessions": len(calling), "calls": len(calling) * a.reps}

    if out.exists() and not a.resume:
        refuse(f"{out} exists; pass --resume to continue it (a run is never re-made)")
    if a.resume and not Path(out, "meta.json").exists():
        refuse(f"nothing to resume at {out}")
    hashes = file_hashes(a.data, a.labels)
    check_wording()
    check_fit_runs_frozen(hashes, exclude=out)
    if a.split == "heldout":
        if not a.resume:
            check_no_other_heldout(out)
        check_fit_run(a.fit_run, hashes)
    client, api_error, setup_errors = make_client()

    base = {"model": MODEL, "prompt_hash": ntp.PROMPT_HASH, "split": a.split, "reps": a.reps,
            "hashes": hashes, "manifest_digest": digest, "max_tokens": MAX_TOKENS,
            "max_retries": MAX_RETRIES, "effort": EFFORT}
    if a.resume:
        meta = store.read_json(Path(out, "meta.json"))
        diff = [k for k in _RESUME_KEYS if meta.get(k) != base[k]]
        if diff:
            refuse(f"--resume with different {diff} than the recorded run")
    else:
        meta = {**base, "started_at": _now(), "data_dir": str(a.data),
                "labels_dir": str(a.labels), "fit_run": a.fit_run,
                "sessions": len(preps), "calling_sessions": len(calling),
                "items": sum(len(pr["items"]) for pr in preps.values()),
                "skipped": dict(sorted(skipped.items())), "complete": False}
        out.mkdir(parents=True)
        write_json(Path(out, "meta.json"), meta)
    meta["workers"] = a.workers

    entries = load_log(out)
    done = {(e["session"], e["rep"]) for e in entries}
    log_path = Path(out, "log.jsonl")

    def record(entry):
        append_jsonl(log_path, entry)
        entries.append(entry)

    todo = []
    for k in range(1, a.reps + 1):
        for ref in sorted(preps):
            if (ref, k) in done:
                continue
            if ref in calling:
                todo.append((ref, k))
            else:
                record(no_call_entry(preps[ref], k))
    try:
        execute(todo, preps, client, api_error, a.workers, record, setup_errors)
    except SetupError as e:     # `finally` still rebuilds the totals from the log
        refuse(f"setup error from the API ({e}); nothing recorded for that call. Fix the "
               f"setup, then re-run with --resume")
    finally:
        meta = finalize(out, meta, entries, sorted(preps), a.reps)
    print(store.dumps({k: meta[k] for k in ("calls", "failed_calls", "usage", "complete")}))
    return meta


if __name__ == "__main__":
    main()
