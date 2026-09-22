"""The base set exists twice. This is what stops the copies drifting.

`src/migrations/0065_taxonomy.sql` seeds the vocabulary into Aurora.
`src/taxonomy_base.py` is the same vocabulary as Python, because
`lambda_extract_session` runs OUTSIDE the VPC: it can reach the model and
cannot reach the database, so the tagger has to carry the words with it.

Two copies of one thing is the failure this repo keeps re-learning -- a writer
and a reader spelling the same key differently, a mock teaching a shape the
server does not send, two implementations of one limit. The fix each time is
one definition, and where that genuinely is not possible, a test that makes the
pair fail loudly instead of quietly.

What drifting would actually do here: the tagger would emit a slug the database
has no row for, `lambda_item_writer` would resolve it to nothing, and the topic
would come out untagged. No error, no log line worth reading -- just a feature
that works a little less well every time someone edits one file.
"""
import io
import os
import re

import taxonomy_base

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..",
                         "src", "migrations", "0065_taxonomy.sql")


def _seeded():
    """(parents, leaves) exactly as the migration will INSERT them."""
    sql = io.open(MIGRATION, encoding="utf-8").read()
    parents = [{"slug": s, "label": l, "sort_order": int(o)}
               for s, l, o in re.findall(r"\('([a-z-]+)',\s*'([^']+)',\s*(\d+)\)", sql)]
    leaves = [{"parent_slug": p, "slug": s, "label": l, "sort_order": int(o)}
              for p, s, l, o in
              re.findall(r"\('([a-z-]+)',\s*'([a-z.\-]+)',\s*'([^']+)',\s*(\d+)\)", sql)]
    return parents, leaves


def test_every_leaf_the_tagger_can_emit_is_a_row_the_database_will_hold():
    _p, seeded = _seeded()
    assert {l["slug"] for l in seeded} == taxonomy_base.SLUGS


def test_the_labels_and_parents_and_order_match_too():
    """Not just the slugs. A label that differs shows the wrong word on screen;
    a parent that differs puts a leaf in the wrong group; a sort_order that
    differs reorders the picker against what the database serves."""
    _p, seeded = _seeded()
    mine = {l["slug"]: (l["label"], l["parent_slug"], l["sort_order"])
            for l in taxonomy_base.LEAVES}
    theirs = {l["slug"]: (l["label"], l["parent_slug"], l["sort_order"])
              for l in seeded}
    assert mine == theirs


def test_the_twelve_groups_match():
    seeded, _l = _seeded()
    mine = [(p["slug"], p["label"], p["sort_order"]) for p in taxonomy_base.PARENTS]
    theirs = [(p["slug"], p["label"], p["sort_order"]) for p in seeded]
    assert mine == theirs


def test_the_shape_is_two_levels_and_only_two():
    """A third level would be a tag whose parent is a tag with a parent, and
    nothing downstream -- the picker, the tree builder, the prompt -- is
    written for it."""
    for leaf in taxonomy_base.LEAVES:
        assert leaf["slug"].count(".") == 1, leaf["slug"]
        assert leaf["slug"].startswith(leaf["parent_slug"] + "."), leaf["slug"]


def test_no_slug_appears_twice():
    slugs = [l["slug"] for l in taxonomy_base.LEAVES]
    assert len(slugs) == len(set(slugs))


def test_the_counts_are_the_ones_that_were_measured():
    """12 and 72, counted against a real database rather than by eye -- the
    first draft of the migration comment said 65 and the count was 70. If
    either number moves, the bake-off's 100 gold labels were scored against a
    different vocabulary and have to be RE-RUN rather than carried forward.

    It moved once, from 70 to 72, and the re-run happened: two annotators
    labelling the same 100 topics blind both ran out of vocabulary on the same
    three items (an evacuation drill, a Task Analysis nobody could open,
    emergency procedures on a site walk), so `safety.emergency-preparedness`
    and `safety.method-statement` were added and the gold re-hashed."""
    assert len(taxonomy_base.PARENTS) == 12
    assert len(taxonomy_base.LEAVES) == 72
