"""Blind assignment labelling for the item-continuity measurement (spec S7 "Gold").

Reads Task 10's run files (`runs/<shape>/<arm>/<rep>/<session>/<step>.json`, as written by
`scripts/continuity_eval/run.py`) and builds the gold-labelling worklist: one assignment per
(session, shape, arm, rep, step, new item), pairing each new item against the SAME step's
prior list (the published items one pass back in that chain). Assignments are the unit of
work, not pairs -- the annotator sees one new item and its whole prior list at once and picks
one counterpart (or "none"), exactly as spec S7 "Gold" describes.

Two audiences read different files:
  - the AGENT annotator gets `prompt_for_agent(line)`'s rendering of a line from
    `assignments_todo.<labeller>.jsonl` -- never the model's own claim, never which arm or run
    produced the list (spec S7 "Gold"; controller ruling on the agent annotator).
  - the OWNER adjudicates `adjudication_todo.jsonl`, which is allowed to show the model's claim
    and the agent's own answer, because adjudicating a disagreement requires knowing what the
    two labels were.

Pure I/O over JSONL plus `item_continuity`'s already-tested prior-list logic: no psycopg, no
boto3, no LLM call. Every function here is exercised on hand-built run directories in
tests/unit/test_continuity_eval_score.py; nothing here calls AWS.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import item_continuity

AGENT = "agent"
OWNER = "owner"
_LABELLERS = (AGENT, OWNER)

# Shown verbatim to the agent annotator. Deliberately says nothing about "claims", "the model",
# "continues", or an "arm" -- the annotator judges the new item against the prior list on its
# own merits, the same way a human reading two extraction passes cold would, not by guessing
# what an LLM asserted (spec S7 "Gold": "labelling is done blind to the model's claims";
# controller ruling: the agent annotator is never shown the model's claims, the continuity
# block, or which arm produced the list). Kept as one constant so a test can pin its content.
BLIND_LABEL_INSTRUCTION = (
    "You are shown a transcript excerpt, a list of items published from an earlier pass over "
    "that same recording (each under a short id), and one new item written from a later pass "
    "over the SAME recording. Decide whether the new item is the same piece of work as one of "
    "the earlier items -- continued, finished, or just reworded -- or whether it has no "
    "earlier counterpart. Answer with that earlier item's id, or \"none\" if there is no "
    "counterpart. Separately, judge from the transcript excerpt alone whether the new item is "
    "actually something said in the recording, not invented. Answer true or false for that. "
    "Also list any of the earlier items of the SAME kind that are about the same topic or "
    "subject as the new item but are clearly different work -- a hard negative, not the "
    "counterpart. For example, if the new item is \"Fix the leaking valve in room 12\" and an "
    "earlier item reads \"Fix the leaking valve in room 14\", that earlier item is a hard "
    "negative: same kind of work, different valve. List their ids, or an empty list if none."
)

# Public fields (`export` copies exactly these, nothing else, into every todo file). Adding a
# field here is a decision that BOTH the agent and the owner get to see it -- `transcript_ref`
# is a path into the gitignored run directory's `transcripts/` folder, not transcript text
# itself, so it carries no more information than the session/step fields already do.
_PUBLIC_FIELDS = ("assignment_id", "session", "shape", "arm", "rep", "step",
                   "list_name", "new_item_text", "prior_list", "transcript_ref")


def _read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        raw_line = raw_line.strip()
        if raw_line:
            yield json.loads(raw_line)


def _write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _session_env_by_key(run_dir):
    """{session_key: "test"|"prod"} from `run_dir/sessions.json` (written by run.py's `main`).
    A session missing from that file (e.g. a hand-built test fixture) defaults to "test", the
    safer default: it means agent-labelled, never routed to the owner file by mistake."""
    path = Path(run_dir) / "sessions.json"
    if not path.exists():
        return {}
    sessions = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for s in sessions:
        key = f"{s['user_folder']}__{s['date']}__{s['session_base']}"
        out[key] = s.get("env", "test")
    return out


def _load_chain_steps(session_dir):
    """Step records 0.json, 1.json, ... in step order, for one (shape, arm, rep, session)
    chain directory."""
    files = sorted(session_dir.glob("*.json"), key=lambda p: int(p.stem))
    return [json.loads(f.read_text(encoding="utf-8")) for f in files]


def iter_assignments(run_dir):
    """Every gold-labelling assignment under `run_dir/runs/`, in a stable document order.

    A step contributes assignments only when it has a prior to offer (a chain's step 0 never
    does) and the step is not void (spec S7 "Void condition"). Each new item is paired with the
    SAME kind's slice of the prior step's `item_continuity.prior_items()` list -- cross-kind
    links are meaningless (item_continuity's own CONTINUITY_INSTRUCTION: "Only link items of
    the same kind"), so there is nothing for an annotator to judge across kinds either.

    Each yielded record carries the PUBLIC fields `prompt_for_agent`/`export` show the blind
    annotator (see `_PUBLIC_FIELDS`), plus PRIVATE fields -- `model_claim_alias`,
    `model_claim_outcome`, `model_claim_guard`, `new_item_id`, `_alias_to_item_id`,
    `_transcript_text` -- that only `build_adjudication_todo` (owner-facing) and scoring ever
    read. `export` strips them before writing any todo file.
    """
    runs_root = Path(run_dir) / "runs"
    if not runs_root.exists():
        return
    for shape_dir in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        for arm_dir in sorted(p for p in shape_dir.iterdir() if p.is_dir()):
            for rep_dir in sorted((p for p in arm_dir.iterdir() if p.is_dir()),
                                   key=lambda p: p.name):
                for session_dir in sorted(p for p in rep_dir.iterdir() if p.is_dir()):
                    yield from _assignments_for_chain(shape_dir.name, arm_dir.name,
                                                        rep_dir.name, session_dir)


def _assignments_for_chain(shape, arm, rep, session_dir):
    steps = _load_chain_steps(session_dir)
    for step_idx in range(1, len(steps)):
        step = steps[step_idx]
        if step.get("void"):
            continue
        prior_topics = steps[step_idx - 1].get("extraction", {}).get("topics")
        prior_list = item_continuity.prior_items({"topics": prior_topics})
        prior_by_kind = {}
        for p in prior_list:
            prior_by_kind.setdefault(p.list_name, []).append(
                {"alias": p.alias, "kind": item_continuity.KINDS[p.list_name].record_type,
                 "text": p.text})
        alias_to_item_id = {p.alias: p.item_id for p in prior_list}
        claims_by_new_id = {c["new_item_id"]: c for c in (step.get("claims") or [])}
        new_topics = step.get("extraction", {}).get("topics")
        # One shared transcript per step (every new item in this step came from the SAME
        # extraction call over the SAME transcript excerpt): `run.py` persists it as
        # `transcript_text` on the step record; here it is only referenced by a stable path
        # under `transcripts/`, written once by `export` (never duplicated per new item).
        transcript_ref = "/".join([shape, arm, rep, session_dir.name, f"{step_idx}.txt"])
        transcript_text = step.get("transcript_text") or ""
        new_index = 0
        for list_name, child in item_continuity._children(new_topics):
            text = (child.get(item_continuity.KINDS[list_name].text_field) or "").strip()
            if text:
                claim = claims_by_new_id.get(child.get("item_id"))
                assignment_id = "|".join(
                    [session_dir.name, shape, arm, rep, str(step_idx), str(new_index)])
                yield {
                    "assignment_id": assignment_id,
                    "session": session_dir.name, "shape": shape, "arm": arm, "rep": rep,
                    "step": step_idx, "list_name": list_name, "new_item_text": text,
                    "prior_list": prior_by_kind.get(list_name, []),
                    "transcript_ref": transcript_ref,
                    "model_claim_alias": claim["alias"] if claim else None,
                    "model_claim_outcome": claim["outcome"] if claim else None,
                    "model_claim_guard": claim["guard"] if claim else None,
                    # Private bookkeeping -- never copied by `export` (only `_PUBLIC_FIELDS`
                    # is), used by scoring to check whether the new item's FINAL item_id
                    # already equals a gold counterpart's item_id (carry_recall), to build
                    # (prior_item_id, new_item_id)-style pairs for the label-rate stat, and by
                    # `export` to write the shared transcript side file once per step.
                    "new_item_id": child.get("item_id"),
                    "_alias_to_item_id": alias_to_item_id,
                    "_transcript_text": transcript_text,
                }
            new_index += 1


def prompt_for_agent(line, transcripts_dir=None):
    """The exact blind instruction text for one `assignments_todo` line: the fixed
    instruction, the transcript excerpt, this line's prior list, and the new item text --
    nothing else. `line` may carry extra bookkeeping keys (arm, model claim fields, ...); only
    `transcript_text`/`transcript_ref`, `prior_list`, and `new_item_text` are ever read here,
    so those extra keys never reach the rendered text.

    The transcript itself is looked up, not duplicated per line: `line["transcript_text"]` is
    used directly if present (a caller holding the text in memory, e.g. a test); otherwise, if
    `transcripts_dir` is given and `line["transcript_ref"]` names a file under it, that file is
    read (the real `assignments_todo` path -- `export` writes one shared transcript file per
    step, referenced by every new item's line from that step, never copied per line)."""
    transcript = line.get("transcript_text")
    if transcript is None and transcripts_dir is not None and line.get("transcript_ref"):
        path = Path(transcripts_dir) / line["transcript_ref"]
        if path.exists():
            transcript = path.read_text(encoding="utf-8")
    transcript = transcript or "(transcript unavailable)"
    prior = line.get("prior_list") or []
    prior_text = "\n".join(f"- {p['alias']}: {p['text']}" for p in prior) \
        or "(no earlier items of this kind)"
    return (
        BLIND_LABEL_INSTRUCTION
        + "\n\nTranscript excerpt:\n" + transcript
        + "\n\nEarlier items:\n" + prior_text
        + "\n\nNew item:\n" + line["new_item_text"]
    )


def _labeller_for(env, prod_labeller):
    if env == "test":
        return AGENT
    return prod_labeller


def export(run_dir, out_dir, prod_labeller=OWNER):
    """Writes `assignments_todo.agent.jsonl` and `assignments_todo.owner.jsonl` under
    `out_dir`, plus one transcript file per step under `out_dir/transcripts/` (both todo
    files reference the SAME transcript file for a given step -- it is written once, not
    once per new item or per labeller). TEST sessions always go to the agent file; PROD
    sessions go to the agent file only if `prod_labeller == "agent"`, otherwise (the default,
    until the owner decides otherwise) to the owner file (controller ruling on
    `prod_labeller`). Every line is stripped to the public fields -- no claims, no
    arm-identifying commentary beyond the bookkeeping `arm` field itself, which
    `prompt_for_agent` never reads.

    Returns the two file paths, the transcripts directory, and how many lines went to each
    todo file, for the caller to log."""
    if prod_labeller not in _LABELLERS:
        raise ValueError(f"prod_labeller must be 'agent' or 'owner', got {prod_labeller!r}")
    envs = _session_env_by_key(run_dir)
    rows = {AGENT: [], OWNER: []}
    transcripts = {}
    for a in iter_assignments(run_dir):
        env = envs.get(a["session"], "test")
        dest = _labeller_for(env, prod_labeller)
        rows[dest].append({k: a[k] for k in _PUBLIC_FIELDS})
        transcripts.setdefault(a["transcript_ref"], a.get("_transcript_text") or "")
    out_dir = Path(out_dir)
    paths = {labeller: out_dir / f"assignments_todo.{labeller}.jsonl" for labeller in _LABELLERS}
    for labeller, path in paths.items():
        _write_jsonl(path, rows[labeller])
    transcripts_dir = out_dir / "transcripts"
    for ref, text in transcripts.items():
        transcript_path = transcripts_dir / ref
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text(text, encoding="utf-8")
    return {"agent_path": str(paths[AGENT]), "owner_path": str(paths[OWNER]),
            "transcripts_dir": str(transcripts_dir),
            "agent_count": len(rows[AGENT]), "owner_count": len(rows[OWNER])}


def import_labels(done_paths, out_path):
    """Merges one or more `assignments_done` files, keyed by `assignment_id`, into the
    canonical file. Where the same `assignment_id` appears in more than one file -- the agent's
    initial label and the owner's adjudication of it -- the OWNER's label always wins,
    regardless of which file is processed first: adjudication exists to replace a wrong agent
    label, so an owner record is never overwritten by an agent one for the same id."""
    merged = {}
    for path in done_paths:
        for row in _read_jsonl(path):
            aid = row["assignment_id"]
            existing = merged.get(aid)
            if existing is None or row.get("labeller") == OWNER:
                merged[aid] = row
    ordered = sorted(merged.values(), key=lambda r: r["assignment_id"])
    _write_jsonl(out_path, ordered)
    return ordered


def _counterparts(done_row):
    """Normalises a done row's `counterpart` field ("<alias>", "none", or a list of aliases for
    a merge -- spec S7 "Pre-registered scoring": "a claim onto an item that merged the prior
    item with another counts as correct") to a set of aliases (empty for "none")."""
    c = done_row.get("counterpart")
    if c is None or c == "none":
        return set()
    if isinstance(c, list):
        return set(c)
    return {c}


def _adjudication_row(assignment, agent_done, reason):
    return {
        "assignment_id": assignment["assignment_id"], "session": assignment["session"],
        "shape": assignment["shape"], "arm": assignment["arm"], "rep": assignment["rep"],
        "step": assignment["step"], "list_name": assignment["list_name"],
        "new_item_text": assignment["new_item_text"], "prior_list": assignment["prior_list"],
        "transcript_ref": assignment["transcript_ref"],   # same side file `export` wrote
        "agent_counterpart": agent_done.get("counterpart"),
        "model_claimed_alias": assignment.get("model_claim_alias"),
        "reason": reason,
    }


def build_adjudication_todo(run_dir, agent_done_path, out_path, sample_rate=0.10, seed=0):
    """`adjudication_todo.jsonl`: every accepted claim the agent's own answer disagrees with,
    plus a seeded random `sample_rate` of the remaining agent-labelled assignments (spec S7
    "Gold": "the owner adjudicates every accepted claim the agent calls different, plus a
    random 10% of all assignments"). A row already sent for disagreement is never re-added by
    the sample, so the two reasons never double-count the same assignment.

    Only assignments the agent actually labelled (present in `agent_done_path`) are eligible --
    an unlabelled assignment has nothing for the owner to agree or disagree with yet."""
    agent_done = {row["assignment_id"]: row for row in _read_jsonl(agent_done_path)}
    assignments = [a for a in iter_assignments(run_dir) if a["assignment_id"] in agent_done]

    disagreements, chosen_ids = [], set()
    for a in assignments:
        if a.get("model_claim_outcome") != "accepted":
            continue
        done = agent_done[a["assignment_id"]]
        if a.get("model_claim_alias") not in _counterparts(done):
            disagreements.append(_adjudication_row(a, done, "claim_disagreement"))
            chosen_ids.add(a["assignment_id"])

    pool = [a for a in assignments if a["assignment_id"] not in chosen_ids]
    n_sample = round(len(pool) * sample_rate)
    rng = random.Random(seed)
    for a in rng.sample(pool, min(n_sample, len(pool))):
        disagreements.append(_adjudication_row(a, agent_done[a["assignment_id"]], "sample"))

    _write_jsonl(out_path, disagreements)
    return disagreements
