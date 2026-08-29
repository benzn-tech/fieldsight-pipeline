"""Diff what the extractor found against what the brief found, for one session.

The plan for moving the item store onto the brief (spec
`2026-08-13-briefing-first-capture-design.md`, section 11, stage 2) says not to
cut over blind: run both paths for a week or two and compare the item sets per
session first. This is the tool that comparison needs.

The reason it is not optional. The extractor is *correct* on the sessions that
matter most -- assignee fill was 5/5 on a site walk and 14/14 on a named team
sync -- and the brief was measured across 27 runs at roughly 2-3 real tasks of 5
with 0-2 false. Neither number is a licence to retire the other path. What
settles it is a list, per real session, of what each one found and the other
missed.

Usage:

    python scripts/compare_extraction_vs_brief.py Ben_UCPK2 2026-08-27
    python scripts/compare_extraction_vs_brief.py Ben_UCPK2 2026-08-27 --env prod
    python scripts/compare_extraction_vs_brief.py Ben_UCPK2 2026-08-27 --json

Reads only. Needs S3 read on the chosen bucket and nothing else.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

BUCKETS = {
    "test": "fieldsight-data-test-509194952652",
    "prod": "fieldsight-data-509194952652",
}

# Join two items only when the score is unambiguous. Measured on real pairs from
# one session, the should-match and should-not-match bands OVERLAP:
#
#   0.625  SAME       "Downtown proposal review -- attend Friday 2 PM"
#   0.500  SAME       "Recording devices -- deploy to James (South Island)"
#   0.067  SAME       "Photo visibility bug -- ..." / "Fix the bug where photos ..."
#   0.000  DIFFERENT  (all four measured pairs)
#
# The gap between the weakest true pair and every false one is what MATCH_RATIO
# sits in. It is set high because a wrong join reads in this report as "both
# paths found it" and hides one side's work -- the failure that would make the
# whole comparison misleading. Anything below is reported one-sided WITH its
# best candidate and score, so a near miss like the 0.067 photo pair is
# resolved by the reader rather than guessed at by the tool.
MATCH_RATIO = 0.4
NEAR_MISS = 0.05

_WORD = re.compile(r"[0-9a-z一-鿿]+")
# Words that carry no signal about WHICH item this is.
_STOP = set("""the a an and or of to in on at for with from by is are be
this that it its they them we our you your do does did not no都 了 的 和 与 把
一个 这个 那个 需要 进行 以及 或者""".split())


def _tokens(text: str) -> set:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 1}


def similarity(a: str, b: str) -> float:
    """Shared content words over all content words. No character-level ratio.

    An earlier version took max(jaccard, SequenceMatcher). Measured on real
    pairs from one session, the character ratio is noise: it scores ANY two
    English sentences of similar length around 0.26-0.41, including
    "Outlook calendar integration" against "Pour the slab on level two" (0.41).
    Taking the max let that noise dominate the signal.
    
    Token overlap alone separates the same cases cleanly:
    
        same       0.067  0.500  0.625
        different  0.000  0.000  0.000  0.000
    
    The two paths share the nouns that identify an item -- "photos", "James",
    "downtown", "Clement" -- and share nothing when the items differ. That is
    the whole signal, and mixing anything else in only blurred it.
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0          # nothing to compare; two empty items are not one item
    return len(ta & tb) / len(ta | tb)


def pair_up(left: list, right: list, key=lambda x: x):
    """Greedy best-first pairing. Returns (pairs, only_left, only_right).

    Greedy rather than optimal on purpose: an optimal assignment would pair
    items that merely rank best against each other, which reads as a match when
    it is really two leftovers. Best-first pairs the confident ones and leaves
    the rest visible.
    """
    scored = sorted(
        ((similarity(key(l), key(r)), i, j) for i, l in enumerate(left) for j, r in enumerate(right)),
        key=lambda t: -t[0])
    used_l, used_r, pairs = set(), set(), []
    for score, i, j in scored:
        if score < MATCH_RATIO or i in used_l or j in used_r:
            continue
        used_l.add(i)
        used_r.add(j)
        pairs.append((left[i], right[j], round(score, 2)))
    def annotate(item, others):
        """Attach the closest thing on the other side, so a near miss is visible
        as a near miss rather than as two unrelated leftovers."""
        best, score = None, 0.0
        for o in others:
            sc = similarity(key(item), key(o))
            if sc > score:
                best, score = o, sc
        if best is not None and score >= NEAR_MISS:
            return dict(item, _near=key(best), _near_score=round(score, 2))
        return item

    only_left = [annotate(l, right) for i, l in enumerate(left) if i not in used_l]
    only_right = [annotate(r, left) for j, r in enumerate(right) if j not in used_r]
    return pairs, only_left, only_right


def _get(s3, bucket, key):
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8"))
    except Exception:
        return None


def sessions_for(s3, bucket, folder, date):
    """Every session id that has an extraction, a brief, or both.

    Listing both sides rather than one is the point: a session the brief never
    produced is exactly the kind of gap this is looking for, and starting from
    the extraction prefix would hide it.
    """
    ids = set()
    for prefix, pat in ((f"extractions/{folder}/{date}/", r"/(sid[0-9a-f]+)\.json$"),
                        (f"session_brief/{folder}/{date}/", r"/(sid[0-9a-f]+)/")):
        token = None
        while True:
            kw = {"Bucket": bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            page = s3.list_objects_v2(**kw)
            for obj in page.get("Contents", []):
                m = re.search(pat, obj["Key"])
                if m:
                    ids.add(m.group(1))
            if not page.get("IsTruncated"):
                break
            token = page["NextContinuationToken"]
    return sorted(ids)


def compare_session(s3, bucket, folder, date, sid):
    extraction = _get(s3, bucket, f"extractions/{folder}/{date}/{sid}.json")
    brief = _get(s3, bucket, f"session_brief/{folder}/{date}/{sid}/latest.json")

    ex_items = [] if not extraction else [
        {"text": a.get("action") or "", "who": a.get("responsible"),
         "due": a.get("deadline"), "topic": t.get("topic_title")}
        for t in (extraction.get("topics") or []) for a in (t.get("action_items") or [])]
    br_items = [] if not brief else [
        {"text": t.get("text") or "", "who": t.get("assignee"),
         "due": t.get("due"), "basis": t.get("basis")}
        for t in (brief.get("tasks") or [])]

    pairs, only_ex, only_br = pair_up(ex_items, br_items, key=lambda x: x["text"])
    return {
        "session": sid,
        "has_extraction": extraction is not None,
        "has_brief": brief is not None,
        "extraction_items": len(ex_items), "brief_items": len(br_items),
        "extraction_assigned": sum(1 for i in ex_items if i["who"]),
        "brief_assigned": sum(1 for i in br_items if i["who"]),
        "matched": [{"extraction": a["text"], "brief": b["text"], "score": s} for a, b, s in pairs],
        "only_extraction": only_ex, "only_brief": only_br,
        "brief_stats": (brief or {}).get("stats"),
    }


def render(result):
    out = []
    a, b = result["extraction_items"], result["brief_items"]
    flags = []
    if not result["has_extraction"]:
        flags.append("NO EXTRACTION")
    if not result["has_brief"]:
        flags.append("NO BRIEF")
    out.append(f"── {result['session']}   extraction {a} (assigned {result['extraction_assigned']})"
               f"   brief {b} (assigned {result['brief_assigned']})"
               + ("   [" + ", ".join(flags) + "]" if flags else ""))
    if result["brief_stats"]:
        out.append(f"     brief stats: {result['brief_stats']}")
    for m in result["matched"]:
        out.append(f"   = {m['score']:.2f}  {m['extraction'][:64]}")
        out.append(f"            brief: {m['brief'][:64]}")
    for tag, items in (("- only extraction:", result["only_extraction"]),
                       ("+ only brief:     ", result["only_brief"])):
        for i in items:
            out.append(f"   {tag} {i['text'][:70]}" + (f"   [{i['who']}]" if i.get("who") else ""))
            if i.get("_near"):
                out.append(f"        near ({i['_near_score']:.2f}): {i['_near'][:64]}"
                           "   <- same item? decide by eye")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("folder", help="recording folder, e.g. Ben_UCPK2")
    ap.add_argument("date", help="YYYY-MM-DD")
    ap.add_argument("--env", choices=sorted(BUCKETS), default="test")
    ap.add_argument("--session", help="one session id (sid...), default all that day")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = ap.parse_args(argv)

    import boto3
    s3 = boto3.client("s3", region_name="ap-southeast-2")
    bucket = BUCKETS[args.env]

    ids = [args.session] if args.session else sessions_for(s3, bucket, args.folder, args.date)
    if not ids:
        print(f"no sessions under {args.folder}/{args.date} in {args.env}", file=sys.stderr)
        return 1

    results = [compare_session(s3, bucket, args.folder, args.date, sid) for sid in ids]
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    for r in results:
        print(render(r))
        print()
    both = [r for r in results if r["has_extraction"] and r["has_brief"]]
    if both:
        print(f"{len(both)} session(s) with both paths: "
              f"extraction {sum(r['extraction_items'] for r in both)} items "
              f"({sum(r['extraction_assigned'] for r in both)} assigned), "
              f"brief {sum(r['brief_items'] for r in both)} "
              f"({sum(r['brief_assigned'] for r in both)} assigned), "
              f"{sum(len(r['matched']) for r in both)} matched, "
              f"{sum(len(r['only_extraction']) for r in both)} extraction-only, "
              f"{sum(len(r['only_brief']) for r in both)} brief-only")
    missing = [r["session"] for r in results if r["has_extraction"] and not r["has_brief"]]
    if missing:
        print(f"{len(missing)} session(s) have an extraction and NO brief: {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
