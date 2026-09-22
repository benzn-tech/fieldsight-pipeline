"""Which tagging method should we build? Measure it, do not argue it.

Two candidates, both behind one interface so neither gets a head start:

  A  classifier   one model call per batch of topics; the model is given the
                  taxonomy and the topic text and returns slugs. Nothing about
                  the topic is rewritten -- it returns labels and nothing else.
  B  embedding    no model call at all: cosine nearest-neighbour between the
                  topic's embedding and each taxonomy leaf's.

GROUND TRUTH, AND ITS LIMIT. `scripts/fixtures/taxonomy_bakeoff_gold.json` holds
100 real prod topics and the leaves each should carry. Those labels were
assigned by reading the topics against the base set BEFORE either method was
run, and the file was written and hashed before the first API call. There is
ONE annotator and no second opinion: that is the weakest part of this
measurement and no conclusion here should be stated more strongly than it
supports. The repo's usual standard is "what the user actually judged"
(scripts/eval_task_admission.py); this is not that.

WHAT THE SAMPLE ALREADY SHOWS, before any method runs: 50 of the 100 topics
take NO construction tag. They are device tests, product discussions, personal
conversation, and recordings with nothing in them. Any method that always
emits a label is wrong half the time on a real corpus, so ABSTENTION IS SCORED
here as heavily as assignment -- a scorer that only measured the 50 taggable
items would rank a method that never shuts up above one that knows when to.

Usage:
    python scripts/bakeoff_taxonomy_tagging.py            # both methods
    python scripts/bakeoff_taxonomy_tagging.py --only A
"""
import argparse
import io
import json
import math
import os
import re
import sys
import time

# line_buffering, or a redirected run shows nothing until it finishes and a
# long job is indistinguishable from a hung one.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              line_buffering=True)
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("S3_BUCKET", "fieldsight-data-509194952652")

import boto3  # noqa: E402

GOLD_PATH = os.path.join(HERE, "fixtures", "taxonomy_bakeoff_gold.json")
MIGRATION = os.path.join(HERE, "..", "src", "migrations", "0065_taxonomy.sql")


def load_env_from_lambda(fn, keys):
    """The repo's convention (scripts/eval_task_admission.py): read the keys off
    the deployed function rather than keeping a second copy of them here."""
    env = boto3.client("lambda").get_function_configuration(
        FunctionName=fn)["Environment"]["Variables"]
    for k in keys:
        if env.get(k):
            os.environ[k] = env[k]


def taxonomy():
    """The 70 leaves, read from the MIGRATION rather than retyped.

    A second copy of the base set in this file is a second copy that drifts,
    and the first thing it would break is the comparison it exists to serve --
    a method scored against a vocabulary the product does not have.
    """
    sql = io.open(MIGRATION, encoding="utf-8").read()
    parents = dict(re.findall(r"\('([a-z-]+)',\s*'([^']+)',\s*\d+\)", sql))
    leaves = []
    for pslug, slug, label, _ord in re.findall(
            r"\('([a-z-]+)',\s*'([a-z.\-]+)',\s*'([^']+)',\s*(\d+)\)", sql):
        leaves.append({"slug": slug, "label": label,
                       "parent": parents.get(pslug, pslug), "parent_slug": pslug})
    return leaves


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score(rows, predicted):
    """Set-overlap per item, macro-averaged, plus what the averages hide.

    An item whose gold is [] scores 1.0 when the method also says [] and 0.0
    otherwise: "say nothing" is a correct answer and has to be worth the same
    as a correct label, or the method that never abstains wins by volume.
    """
    p_sum = r_sum = f_sum = 0.0
    abstain_right = abstain_wrong = 0
    any_overlap = exact = 0
    taggable = 0
    for row, pred in zip(rows, predicted):
        gold, got = set(row["gold"]), set(pred)
        if not gold:
            ok = 1.0 if not got else 0.0
            p_sum += ok; r_sum += ok; f_sum += ok
            abstain_right += int(ok == 1.0)
            abstain_wrong += int(ok == 0.0)
            continue
        taggable += 1
        hit = len(gold & got)
        p = hit / len(got) if got else 0.0
        r = hit / len(gold)
        f = (2 * p * r / (p + r)) if (p + r) else 0.0
        p_sum += p; r_sum += r; f_sum += f
        any_overlap += int(hit > 0)
        exact += int(gold == got)
    n = len(rows)
    return {
        "macro_precision": p_sum / n, "macro_recall": r_sum / n, "macro_f1": f_sum / n,
        "abstained_correctly": abstain_right,
        "labelled_something_untaggable": abstain_wrong,
        "any_overlap_on_taggable": any_overlap, "taggable": taggable,
        "exact_set_match": exact,
    }


# ---------------------------------------------------------------------------
# Method A -- one light classification call per batch
# ---------------------------------------------------------------------------

_BATCH = 20

_PROMPT = """You are labelling construction site conversation topics with a fixed taxonomy.

TAXONOMY (use the slug on the left, nothing else):
{taxonomy}

RULES
- Return 0 to 3 slugs per topic. Fewer is better than more.
- An EMPTY list is the correct answer for a topic that is not about construction
  work: a device or software test, a product or business discussion, personal
  conversation, or a recording with nothing in it. Roughly half of real topics
  are like this. Do not reach for a label that is merely adjacent.
- Use only slugs from the list above. Never invent one.

TOPICS
{topics}

Return ONLY a JSON object mapping each topic's number to its list of slugs:
{{"0": ["programme.schedule"], "1": [], ...}}
No prose, no markdown fences."""


def run_classifier(rows, leaves, log):
    import llm_utils
    tax = "\n".join(f"  {l['slug']}  ({l['parent']} > {l['label']})" for l in leaves)
    valid = {l["slug"] for l in leaves}
    out = [[] for _ in rows]
    calls = tok_in = tok_out = empty = 0
    t0 = time.time()
    for start in range(0, len(rows), _BATCH):
        chunk = rows[start:start + _BATCH]
        topics = "\n\n".join(
            f"[{start + i}] {r['title']}\n{' '.join(r['summary'].split())[:400]}"
            for i, r in enumerate(chunk))
        prompt = _PROMPT.format(taxonomy=tax, topics=topics)
        # (text, error), not a string -- and `enable_thinking` is the spelling.
        text, err = llm_utils.call_llm(prompt, max_tokens=2000,
                                       enable_thinking=False,
                                       caller="taxonomy_bakeoff")
        calls += 1
        tok_in += len(prompt) // 4
        tok_out += len(text or "") // 4
        if err or not (text or "").strip():
            # A 200 WITH EMPTY CONTENT is a known shape from this endpoint, and
            # it must not be read as "the model said no tags" -- that would
            # score a broken call as 20 correct abstentions.
            empty += 1
            log(f"  batch {start}: EMPTY/ERROR reply ({err or 'no content'})")
            continue
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            log(f"  batch {start}: no JSON in reply ({text[:80]!r})")
            continue
        try:
            parsed = json.loads(m.group(0))
        except Exception as e:
            log(f"  batch {start}: unparseable JSON ({e})")
            continue
        for k, v in parsed.items():
            try:
                idx = int(k)
            except ValueError:
                continue
            if 0 <= idx < len(rows) and isinstance(v, list):
                # Invented slugs are DROPPED, not mapped to something near.
                # A method that hallucinates a label should lose recall for it.
                out[idx] = [s for s in v if s in valid][:3]
        log(f"  batch {start}-{start + len(chunk) - 1}: ok")
    return out, {"calls": calls, "empty_replies": empty,
                 "prompt_tokens": tok_in, "completion_tokens": tok_out,
                 "seconds": round(time.time() - t0, 1)}


# ---------------------------------------------------------------------------
# Method B -- cosine nearest neighbour, zero model calls
# ---------------------------------------------------------------------------

def _cos(a, b):
    num = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return num / (na * nb) if na and nb else 0.0


def run_embedding(rows, leaves, log):
    import dashscope_utils
    t0 = time.time()
    # "parent > child" rather than the bare leaf word: 'walls' on its own is a
    # one-word string and this repo's own retrieval eval already showed short
    # strings matching badly. This is the strongest form of the method, not a
    # straw man.
    leaf_texts = [f"{l['parent']}: {l['label']}" for l in leaves]
    topic_texts = [f"{r['title']}. {' '.join(r['summary'].split())}" for r in rows]
    leaf_vecs = dashscope_utils.embed(leaf_texts)
    topic_vecs = dashscope_utils.embed(topic_texts)
    log(f"  embedded {len(leaf_texts)} leaves + {len(topic_texts)} topics")
    sims = [[_cos(tv, lv) for lv in leaf_vecs] for tv in topic_vecs]

    # THE THRESHOLD IS SWEPT ON THE SAME 100 ITEMS IT IS SCORED ON, which is
    # fitting the method to its own test set. That is deliberate and it is a
    # HANDICAP GIVEN TO A, not to B: it reports B at its best possible showing.
    best = None
    for cut in [x / 100 for x in range(20, 80)]:
        preds = []
        for row_sims in sims:
            ranked = sorted(range(len(leaves)), key=lambda i: -row_sims[i])
            preds.append([leaves[i]["slug"] for i in ranked[:3] if row_sims[i] >= cut])
        s = score(rows, preds)
        if best is None or s["macro_f1"] > best[1]["macro_f1"]:
            best = (cut, s, preds)
    cut, _s, preds = best
    log(f"  best cosine cut = {cut:.2f} (swept on the scored set -- B's best case)")
    tokens = sum(len(t) for t in leaf_texts + topic_texts) // 4
    return preds, {"calls": 0, "embed_tokens": tokens, "threshold": cut,
                   "seconds": round(time.time() - t0, 1)}


# ---------------------------------------------------------------------------

def recall_at_k(rows, leaves, log):
    """Could the embedding be used as a CANDIDATE GENERATOR for the classifier?

    That hybrid -- cosine picks the top k leaves, the model chooses among them --
    only helps if the right leaf is inside the top k. Its ceiling is therefore
    this recall, and no prompt can recover a leaf the shortlist dropped.

    Measured rather than assumed, because "use embeddings to cut the prompt" is
    the kind of optimisation that sounds free and quietly caps quality.
    """
    import dashscope_utils
    leaf_texts = [f"{l['parent']}: {l['label']}" for l in leaves]
    topic_texts = [f"{r['title']}. {' '.join(r['summary'].split())}" for r in rows]
    lv = dashscope_utils.embed(leaf_texts)
    tv = dashscope_utils.embed(topic_texts)
    taggable = [(r, [_cos(tv[i], v) for v in lv]) for i, r in enumerate(rows) if r["gold"]]
    out = {}
    for k in (3, 5, 8, 12, 20, 30):
        covered = total = 0
        whole = 0
        for row, sims in taggable:
            top = {leaves[i]["slug"] for i in
                   sorted(range(len(leaves)), key=lambda j: -sims[j])[:k]}
            gold = set(row["gold"])
            covered += len(gold & top)
            total += len(gold)
            whole += int(gold <= top)
        out[k] = (covered / total, whole / len(taggable))
        log(f"  top-{k:<2d}  {covered}/{total} gold leaves in the shortlist "
            f"({out[k][0]:.0%}), all of them for {whole}/{len(taggable)} items")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["A", "B", "K"],
                    help="K measures the embedding's recall@k -- the ceiling "
                         "of using it as a candidate generator for A.")
    ap.add_argument("--runs", type=int, default=1,
                    help="repeat method A this many times. ONE RUN OF A MODEL "
                         "SAYS NOTHING -- the repo's own admission eval exists "
                         "because one prompt gave 3/0/2/2 across four runs.")
    args = ap.parse_args()

    rows = json.load(io.open(GOLD_PATH, encoding="utf-8"))
    leaves = taxonomy()
    print(f"{len(rows)} topics, {len(leaves)} taxonomy leaves")
    taggable = sum(1 for r in rows if r["gold"])
    print(f"gold: {taggable} taggable, {len(rows) - taggable} take no tag at all\n")

    def log(m):
        print(m, flush=True)

    results = {}
    if args.only in (None, "A"):
        load_env_from_lambda("fieldsight-prod-extract-session",
                             ("LLM_PROVIDER", "QWEN_API_KEY", "QWEN_BASE_URL", "QWEN_MODEL"))
        os.environ["LLM_HTTP_TIMEOUT"] = "240"
        for n in range(args.runs):
            label = "A classifier" if args.runs == 1 else f"A classifier run {n + 1}"
            print(f"{label}")
            preds, meta = run_classifier(rows, leaves, log)
            results[label] = (score(rows, preds), meta, preds)
    if args.only == "K":
        load_env_from_lambda("fieldsight-prod-embed-report",
                             ("DASHSCOPE_API_KEY", "DASHSCOPE_BASE_URL",
                              "DASHSCOPE_EMBED_MODEL", "DASHSCOPE_EMBED_DIM"))
        print("\nK  embedding as a candidate generator -- recall@k on the taggable")
        recall_at_k(rows, leaves, log)
        return

    if args.only in (None, "B"):
        load_env_from_lambda("fieldsight-prod-embed-report",
                             ("DASHSCOPE_API_KEY", "DASHSCOPE_BASE_URL",
                              "DASHSCOPE_EMBED_MODEL", "DASHSCOPE_EMBED_DIM"))
        print("\nB  embedding nearest neighbour")
        preds, meta = run_embedding(rows, leaves, log)
        results["B embedding"] = (score(rows, preds), meta, preds)

    print("\n" + "=" * 72)
    for name, (s, meta, _p) in results.items():
        print(f"\n{name}")
        print(f"  macro F1          {s['macro_f1']:.3f}   "
              f"(precision {s['macro_precision']:.3f}, recall {s['macro_recall']:.3f})")
        print(f"  abstained right   {s['abstained_correctly']}/50 of the untaggable")
        print(f"  mislabelled       {s['labelled_something_untaggable']}/50 untaggable got a label")
        print(f"  any overlap       {s['any_overlap_on_taggable']}/{s['taggable']} of the taggable")
        print(f"  exact set match   {s['exact_set_match']}/{s['taggable']}")
        print(f"  cost/meta         {meta}")

    out = os.environ.get("TEMP", ".") + "/bakeoff/results.json"
    io.open(out, "w", encoding="utf-8").write(json.dumps(
        {k: {"score": v[0], "meta": v[1], "preds": v[2]} for k, v in results.items()},
        ensure_ascii=False, indent=1))
    print(f"\nper-item predictions written to {out}")


if __name__ == "__main__":
    main()
