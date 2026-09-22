"""Label a session's topics with the taxonomy. One extra call, nothing else.

CHOSEN BY MEASUREMENT (scripts/bakeoff_taxonomy_tagging.py, 100 real prod
topics, four runs of each method against labels written and hashed before the
first API call):

    classifier call      macro F1 0.839  (0.822-0.855 over four runs)
    embedding NN         macro F1 0.590  at a threshold tuned on the scored set

The embedding did not lose on a technicality. At the very threshold that made
it abstain correctly it was ALSO silent on 25 of the 50 taggable items: the cut
that stops it labelling a microphone test stops it labelling half the real work
too. A hybrid -- embedding shortlists, model chooses -- was measured as well and
is worse than either: a top-8 shortlist holds only 55% of the right leaves, and
no prompt recovers a leaf the shortlist removed.

A SEPARATE CALL, NOT A FIELD IN THE EXTRACTION SCHEMA. Admission and style
drift together in this repo: changing how the model is asked to write changes
WHAT IT ADMITS. Folding a `tags` key into EXTRACTION_SCHEMA would risk the
topics and action items themselves for a label, and the label costs
$0.0014 per hundred items to ask for on its own.

ABSTENTION IS THE POINT. 50 of the 100 sampled topics take no construction tag
at all -- 31 of them are this product talking about itself (device tests, app
features, AI trials), 12 are personal or non-construction business, 6 have
nothing intelligible in them. The classifier abstained correctly on 50/50 in
every single run, and that is the most valuable thing it does. Anything that
trades it for "a few more labels" is a regression even when the label count
rises.

Pure: the LLM call is injected. The caller supplies the taxonomy and something
that behaves like `llm_utils.call_llm` -- so this module is tested without a
network, and the bake-off and production run the same code.
"""
import json
import logging
import re

logger = logging.getLogger()

#: Measured at 20. Per call of 20 the bake-off saw 127 prompt tokens and 8
#: completion tokens per item, and 27-48 seconds. One call per topic would be
#: twenty times the calls for the same tokens, because the taxonomy -- the bulk
#: of the prompt -- would be re-sent every time.
BATCH = 20

#: The cap the prompt asks for, enforced here too. The model is asked for "0 to
#: 3, fewer is better"; this is what happens when it is not listened to.
MAX_TAGS = 3

PROMPT = """You are labelling construction site conversation topics with a fixed taxonomy.

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


def build_prompt(batch, leaves, offset=0):
    """The prompt for one batch. Separate so a caller can log or diff it."""
    taxonomy = "\n".join(
        f"  {l['slug']}  ({l.get('parent', '')} > {l['label']})" for l in leaves)
    topics = "\n\n".join(
        f"[{offset + i}] {t.get('topic_title') or t.get('title') or ''}\n"
        f"{' '.join((t.get('summary') or '').split())[:400]}"
        for i, t in enumerate(batch))
    return PROMPT.format(taxonomy=taxonomy, topics=topics)


def _parse(text, valid, out, lo, hi):
    """Read one reply into `out`. Returns True if anything was understood.

    Invented slugs are DROPPED rather than mapped to the nearest real one. A
    label the vocabulary does not have cannot become a tag id, and guessing
    which leaf was meant is how a wrong tag acquires a confident provenance.
    """
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return False
    try:
        parsed = json.loads(m.group(0))
    except (ValueError, TypeError):
        return False
    if not isinstance(parsed, dict):
        return False
    for key, slugs in parsed.items():
        try:
            idx = int(key)
        except (TypeError, ValueError):
            continue
        if not (lo <= idx < hi) or not isinstance(slugs, list):
            continue
        seen = []
        for s in slugs:
            if s in valid and s not in seen:
                seen.append(s)
        out[idx] = seen[:MAX_TAGS]
    return True


def classify_with_stats(topics, leaves, call_llm):
    """([slugs] per topic, stats). Never raises; never invents a tag.

    `stats['unanswered']` counts batches whose call produced nothing usable --
    an error, an empty body, or a reply with no JSON in it. THAT COUNT IS NOT
    COSMETIC. This endpoint is known to answer 200 with empty content, and an
    empty reply read as "the model said no tags" would record a broken call as
    a batch of confident abstentions. Abstention is the property this method is
    trusted for, so the one thing that must never be silently faked is an
    abstention.
    """
    return _classify(topics, leaves, call_llm, build_prompt,
                     caller="topic_tagging", what="topic")


def _classify(items, leaves, call_llm, build, *, caller, what):
    """The half both taggers share: batch, call, parse, count what failed.

    One implementation rather than two, because the thing that must not drift
    between them is the unanswered-batch accounting -- two copies of that is
    two chances for one of them to start reading an empty reply as a batch of
    abstentions.
    """
    out = [[] for _ in items]
    if not items:
        return out, {"batches": 0, "unanswered": 0, "tagged": 0, "abstained": 0}

    valid = {l["slug"] for l in leaves}
    batches = unanswered = 0
    for start in range(0, len(items), BATCH):
        chunk = items[start:start + BATCH]
        batches += 1
        text, err = call_llm(
            build(chunk, leaves, offset=start),
            max_tokens=2000,
            enable_thinking=False,
            caller=caller,
        )
        if err or not (text or "").strip():
            unanswered += 1
            logger.warning("tagging: %s batch at %d returned nothing (%s) -- "
                           "its items stay untagged, which is NOT an abstention",
                           what, start, err or "empty body")
            continue
        if not _parse(text, valid, out, start, start + len(chunk)):
            unanswered += 1
            logger.warning("tagging: %s batch at %d had no readable JSON (%.80r)",
                           what, start, text)

    tagged = sum(1 for s in out if s)
    stats = {"batches": batches, "unanswered": unanswered,
             "tagged": tagged, "abstained": len(out) - tagged}
    logger.info("tagging: %d %s(s), %d tagged, %d left untagged, "
                "%d batch(es), %d unanswered",
                len(items), what, tagged, len(out) - tagged, batches, unanswered)
    return out, stats


def classify(topics, leaves, call_llm):
    """`classify_with_stats` without the stats, for callers that only want the
    labels. The stats exist because "tagged nothing" and "the call failed" have
    to stay distinguishable, so anything that RECORDS the result should use the
    other one."""
    return classify_with_stats(topics, leaves, call_llm)[0]


# The action prompt, verbatim from scripts/bakeoff_action_tagging.py. It is the
# one that was measured -- 0.861 against an independent annotator over 90 real
# action items, four runs -- and a reworded copy would describe something
# nobody ran. The two prompts differ in one instruction that carries the whole
# result: LABEL THE ACTION, NOT THE TOPIC.
ACTION_PROMPT = """You are labelling ACTION ITEMS from construction site conversations.

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


def build_action_prompt(batch, leaves, offset=0):
    """The prompt for one batch of actions, byte-for-byte the shape the
    bake-off measured: the action's text, then its topic's title on its own
    indented line as context."""
    taxonomy = "\n".join(
        f"  {l['slug']}  ({l.get('parent', '')} > {l['label']})" for l in leaves)
    items = "\n\n".join(
        f"[{offset + i}] {' '.join((a.get('text') or '').split())[:300]}\n"
        f"    (from topic: {a.get('topic_title') or ''})"
        for i, a in enumerate(batch))
    return ACTION_PROMPT.format(taxonomy=taxonomy, items=items)


def classify_actions_with_stats(actions, leaves, call_llm):
    """The same contract as classify_with_stats, for action items.

    A SEPARATE CALL FROM THE TOPICS, not one prompt doing both. Measured
    separately, and the instruction that makes it work ("label the action, not
    the topic") is the opposite of what a combined prompt would have to say.
    Inheriting the topic's labels instead was measured too and is worse where
    it matters: it finds a right leaf about as often (47 of 57 against 48) and
    adds a wrong one on 46 of 57, because a topic's tag set is wider than any
    single action under it.

    Unanswered batches are counted, never read as abstentions -- see
    classify_with_stats.
    """
    return _classify(actions, leaves, call_llm, build_action_prompt,
                     caller="action_tagging", what="action")
