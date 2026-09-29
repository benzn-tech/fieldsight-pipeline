"""Match a retired topic's children to their replacements by TEXT, so a human's tick,
status change, reassignment or deadline edit survives a re-extraction that reworded the row
around it. Track B Task 4; spec 2026-09-24 §2.1.

PURE module -- no psycopg, no boto3 (same posture as content_hash.py, and required by
Ruling R4/R9's design: the caller reads the "old"/"new" pools out of Aurora, this module
only ever sees plain dicts). Generic over the child table on purpose: Task 5 reuses `match`
verbatim for topic_decisions/topic_questions, so nothing here names an action_items or
findings column.

Item = {"id": ..., "stable_id": ..., "text": ..., "human_touched": bool}. `stable_id` rides
through unused by `match` itself -- it is what the CALLER copies from a matched old item
onto its new successor -- kept in the shape so a caller building Items from a DB row can pass
the row straight through without stripping fields.

Two passes, in order (Ruling R4 / brief Step 1):

  1. EXACT -- normalised keys are string-identical. One-to-one, greedy per old item: an old
     item consumes at most one new item sharing its key, so duplicate text on one side
     degrades gracefully to pass 2 for the overflow rather than double-booking a new id.
  2. FUZZY -- difflib.SequenceMatcher ratio on the same normalised keys, over what pass 1
     left unpaired. A candidate pair is accepted only when it is the MUTUAL best match for
     both sides, ratio >= fuzzy_floor, AND the runner-up on EITHER side is not within
     tie_margin of it. A tie or near-tie is refused as a guess, not resolved by picking the
     higher one: two reworded old items competing for one new item (or the reverse, a
     merge/split) have no principled winner, and a wrong guess would put a human's tick on
     someone else's task where nobody would ever look for it again.

Normalisation (Ruling R4): `content_hash.normalize` -- the SAME key `compliance_resolutions`
already hashes text on, so "the same text" cannot drift between the two features -- with CJK
inter-character whitespace additionally stripped (`evidence_match.strip_cjk_spacing`),
because a model writing Chinese writes it unspaced while turn/topic text is often
space-joined, so a reword can move a sentence between spaced and unspaced without changing a
single character; content_hash.normalize alone would treat those as different keys and bias
the floor against CJK content on a bilingual product.
"""
import difflib

import content_hash
from evidence_match import strip_cjk_spacing


def _key(text):
    return strip_cjk_spacing(content_hash.normalize(text))


def match(old, new, *, fuzzy_floor=0.90, tie_margin=0.05):
    """Pair `old` items to `new` items by text.

    Returns (pairs, orphans):
      pairs   -- [(old_id, new_id, how), ...], how is "exact" or "fuzzy"
      orphans -- [old_id, ...] for every old item that found no successor

    One-to-one on both sides; every id appears in at most one pair. Deterministic and
    independent of input order: pass 1 keys on exact string equality (a duplicate-text old
    pool consumes new ids in list order, the only place order matters at all), and pass 2's
    mutual-best-with-margin test is computed once over the full remaining candidate set
    rather than greedily, so accepting one pair never changes another pair's tie check.
    """
    old = list(old)
    new = list(new)
    old_keys = {item["id"]: _key(item["text"]) for item in old}
    new_keys = {item["id"]: _key(item["text"]) for item in new}

    pairs = []
    matched_old, matched_new = set(), set()

    # Pass 1: exact.
    new_by_key = {}
    for item in new:
        new_by_key.setdefault(new_keys[item["id"]], []).append(item["id"])
    for item in old:
        oid = item["id"]
        bucket = new_by_key.get(old_keys[oid])
        if bucket:
            nid = bucket.pop(0)
            pairs.append((oid, nid, "exact"))
            matched_old.add(oid)
            matched_new.add(nid)

    # Pass 2: fuzzy, mutual-best-with-margin over what pass 1 left unpaired.
    remaining_old = [item["id"] for item in old if item["id"] not in matched_old]
    remaining_new = [item["id"] for item in new if item["id"] not in matched_new]
    ratio = {}
    for oid in remaining_old:
        ok = old_keys[oid]
        for nid in remaining_new:
            ratio[(oid, nid)] = difflib.SequenceMatcher(None, ok, new_keys[nid]).ratio()

    def _best_and_margin(anchor_ids, other_ids, key):
        """Best (ratio, other_id) for `key(anchor)` and how far the runner-up trails it.

        A single candidate has no runner-up, which reads as an infinite margin -- exactly
        right, since there is nothing to be a near-tie with."""
        row = sorted(((ratio[key(o)], o) for o in other_ids), reverse=True)
        if not row:
            return None
        best_ratio, best_id = row[0]
        runner = row[1][0] if len(row) > 1 else -1.0
        return best_id, best_ratio, best_ratio - runner

    for oid in remaining_old:
        row_best = _best_and_margin(None, remaining_new, lambda n: (oid, n))
        if row_best is None:
            continue
        best_nid, best_ratio, row_margin = row_best
        if best_ratio < fuzzy_floor or row_margin <= tie_margin:
            continue                      # below the floor, or a tie/near-tie on OUR side
        col_best = _best_and_margin(None, remaining_old, lambda o: (o, best_nid))
        if col_best is None:
            continue
        best_oid, col_ratio, col_margin = col_best
        if best_oid != oid or col_margin <= tie_margin:
            continue                      # not mutual, or a tie/near-tie on THEIR side
        pairs.append((oid, best_nid, "fuzzy"))
        matched_old.add(oid)
        matched_new.add(best_nid)

    orphans = [item["id"] for item in old if item["id"] not in matched_old]
    return pairs, orphans
