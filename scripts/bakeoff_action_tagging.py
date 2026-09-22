"""Should action items be tagged at all, and if so how? Three arms.

  A  classifier   one model call per batch. The action's text plus its TOPIC'S
                  TITLE -- and not the topic's tags.
  B  inherit      the action takes its topic's tags. Zero model calls.
  C  abstain      nothing is tagged. Zero of everything.

WHY C IS NOT A JOKE. If few actions are taggable, "tag nothing" is a strong
baseline, and without it there is no way to know whether A is beating anything
at all. A method that scores 0.7 against a corpus where doing nothing scores
0.65 has bought almost nothing for its money.

WHY B IS HERE DESPITE A GOOD ARGUMENT AGAINST IT. Inheritance was talked out
of the design with a reasonable counter-example -- an action to send roofing
prices under a concrete-pour topic would inherit `structure.concrete`. That is
an ARGUMENT. An argument was also what recommended using embeddings to
shortlist candidates for the topic tagger, and measurement killed it. So
inheritance is measured, at its best: B is given the topic's GOLD tags, not
the tags a classifier would have produced. If it loses with a perfect input,
it loses.

THE CONTEXT GIVEN TO A IS CHOSEN TO EQUAL PRODUCTION, not to make the task
easy. In production an action and its topic are labelled in the same
extraction pass, so the topic's TITLE exists and its TAGS do not yet -- they
are being computed in the same batch. Giving the model the topic's tags would
make it better informed than the system can ever be; giving it nothing would
make it blinder. The same context is in the blind-annotation pack, so the
second annotator, the model and production are all looking at the same thing.

GROUND TRUTH: scripts/fixtures/action_bakeoff_gold.json, 90 action items --
every one under the 100 topics of the topic bake-off, so there is no sampling
on top of sampling and B gets its topic tags for free. Labelled before any arm
was run, hashed, and a second annotator labelled the same 90 blind.
"""
import argparse
import io
import json
import os
import re
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("S3_BUCKET", "fieldsight-data-509194952652")

import boto3  # noqa: E402
import taxonomy_base  # noqa: E402

GOLD = os.path.join(HERE, "fixtures", "action_bakeoff_gold.json")


def load_env_from_lambda(fn, keys):
    env = boto3.client("lambda").get_function_configuration(
        FunctionName=fn)["Environment"]["Variables"]
    for k in keys:
        if env.get(k):
            os.environ[k] = env[k]


def score(rows, predicted):
    """Identical to the topic bake-off's scorer, and deliberately so: an arm
    that looks better only because it was measured differently is not better.

    An item whose gold is [] scores 1.0 for an empty prediction and 0.0
    otherwise -- abstention is an answer and is worth what a correct label is
    worth, or arm C would be unmeasurable and arm A would be rewarded for
    never shutting up."""
    p = r = f = 0.0
    right_none = wrong_none = overlap = exact = taggable = 0
    for row, pred in zip(rows, predicted):
        g, got = set(row["gold"]), set(pred)
        if not g:
            ok = 1.0 if not got else 0.0
            p += ok; r += ok; f += ok
            right_none += int(ok == 1.0); wrong_none += int(ok == 0.0)
            continue
        taggable += 1
        hit = len(g & got)
        pi = hit / len(got) if got else 0.0
        ri = hit / len(g)
        p += pi; r += ri
        f += (2 * pi * ri / (pi + ri)) if (pi + ri) else 0.0
        overlap += int(hit > 0); exact += int(g == got)
    n = len(rows)
    return {"macro_f1": f / n, "macro_precision": p / n, "macro_recall": r / n,
            "abstained_right": right_none, "labelled_the_untaggable": wrong_none,
            "overlap": overlap, "taggable": taggable, "exact": exact}


# ---------------------------------------------------------------------------

_BATCH = 20
_PROMPT = """You are labelling ACTION ITEMS from construction site conversations.

Each item is a task somebody was asked to do, shown with the title of the
conversation topic it came out of. LABEL THE ACTION, not the topic.

TAXONOMY (use the slug on the left, nothing else):
{taxonomy}

RULES
- Return 0 to 3 slugs per action. Fewer is better than more.
- An EMPTY list is the correct answer for an action that is not about
  construction work: a product or software task, a meeting to arrange, a
  personal errand, or anything else off the site. Do not reach for a label
  that is merely adjacent.
- The topic title is CONTEXT. An action can be about something the topic only
  mentioned in passing -- label what the action says.
- Use only slugs from the list above. Never invent one.

ACTIONS
{items}

Return ONLY a JSON object mapping each number to its list of slugs:
{{"0": ["programme.schedule"], "1": [], ...}}
No prose, no markdown fences."""


def arm_classifier(rows, leaves, log):
    import llm_utils
    tax = "\n".join(f"  {l['slug']}  ({l['parent']} > {l['label']})" for l in leaves)
    valid = {l["slug"] for l in leaves}
    out = [[] for _ in rows]
    calls = tin = tout = unanswered = 0
    t0 = time.time()
    for start in range(0, len(rows), _BATCH):
        chunk = rows[start:start + _BATCH]
        items = "\n\n".join(
            f"[{start + i}] {r['text']}\n    (from topic: {r['topic_title']})"
            for i, r in enumerate(chunk))
        prompt = _PROMPT.format(taxonomy=tax, items=items)
        text, err = llm_utils.call_llm(prompt, max_tokens=2000,
                                       enable_thinking=False,
                                       caller="action_tag_bakeoff")
        calls += 1
        tin += len(prompt) // 4
        tout += len(text or "") // 4
        if err or not (text or "").strip():
            unanswered += 1
            log(f"  batch {start}: EMPTY/ERROR ({err or 'no content'})")
            continue
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            unanswered += 1
            log(f"  batch {start}: no JSON ({text[:70]!r})")
            continue
        try:
            parsed = json.loads(m.group(0))
        except ValueError as e:
            unanswered += 1
            log(f"  batch {start}: unparseable ({e})")
            continue
        for k, v in parsed.items():
            try:
                i = int(k)
            except ValueError:
                continue
            if start <= i < start + len(chunk) and isinstance(v, list):
                seen = []
                for s in v:
                    if s in valid and s not in seen:
                        seen.append(s)
                out[i] = seen[:3]
        log(f"  batch {start}-{start + len(chunk) - 1}: ok")
    return out, {"calls": calls, "unanswered": unanswered, "prompt_tokens": tin,
                 "completion_tokens": tout, "seconds": round(time.time() - t0, 1)}


def arm_inherit(rows, _leaves, _log):
    """The action takes its topic's tags, verbatim.

    Given the topic's GOLD tags rather than a classifier's, which is the
    strongest form this arm can take: it cannot be beaten by saying the topic
    tagger was imperfect."""
    return [list(r["topic_gold"]) for r in rows], {"calls": 0, "seconds": 0.0}


def arm_abstain(rows, _leaves, _log):
    return [[] for _ in rows], {"calls": 0, "seconds": 0.0}


ARMS = {"A": ("A classifier", arm_classifier),
        "B": ("B inherit the topic's tags", arm_inherit),
        "C": ("C tag nothing", arm_abstain)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="ABC")
    ap.add_argument("--runs", type=int, default=1,
                    help="repeat arm A this many times; one run of a model says nothing")
    args = ap.parse_args()

    rows = json.load(io.open(GOLD, encoding="utf-8"))
    leaves = taxonomy_base.LEAVES
    taggable = sum(1 for r in rows if r["gold"])
    print(f"{len(rows)} action items, {len(leaves)} leaves")
    print(f"gold: {taggable} taggable, {len(rows) - taggable} take no tag\n")

    def log(m):
        print(m, flush=True)

    results = {}
    if "A" in args.arms:
        load_env_from_lambda("fieldsight-prod-extract-session",
                             ("LLM_PROVIDER", "QWEN_API_KEY", "QWEN_BASE_URL", "QWEN_MODEL"))
        os.environ["LLM_HTTP_TIMEOUT"] = "240"
        for n in range(args.runs):
            name = "A classifier" if args.runs == 1 else f"A classifier run {n + 1}"
            print(name)
            preds, meta = arm_classifier(rows, leaves, log)
            results[name] = (score(rows, preds), meta, preds)
    for k in "BC":
        if k in args.arms:
            name, fn = ARMS[k]
            preds, meta = fn(rows, leaves, log)
            results[name] = (score(rows, preds), meta, preds)

    print("\n" + "=" * 74)
    for name, (s, meta, _p) in results.items():
        print(f"\n{name}")
        print(f"  macro F1        {s['macro_f1']:.3f}  "
              f"(P {s['macro_precision']:.3f}, R {s['macro_recall']:.3f})")
        print(f"  abstained right {s['abstained_right']}/{len(rows) - s['taggable']}")
        print(f"  wrongly tagged  {s['labelled_the_untaggable']}/{len(rows) - s['taggable']}")
        print(f"  any overlap     {s['overlap']}/{s['taggable']}")
        print(f"  exact set       {s['exact']}/{s['taggable']}")
        print(f"  cost            {meta}")

    out = os.environ.get("TEMP", ".") + "/bakeoff/action_results.json"
    io.open(out, "w", encoding="utf-8").write(json.dumps(
        {k: {"score": v[0], "meta": v[1], "preds": v[2]} for k, v in results.items()},
        ensure_ascii=False, indent=1))
    print(f"\nper-item predictions written to {out}")


if __name__ == "__main__":
    main()
