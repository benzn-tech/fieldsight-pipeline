"""Tagging a session's topics: a SEPARATE call that returns labels and nothing else.

Chosen by measurement, not argument (scripts/bakeoff_taxonomy_tagging.py, 100
real prod topics, four runs): the classifier call scored macro F1 0.839 against
0.590 for embedding nearest-neighbour, and the embedding was silent on 25 of
the 50 taggable items at the very threshold that made it abstain correctly.

TWO PROPERTIES ARE LOAD-BEARING AND BOTH ARE PINNED HERE.

**Abstention is a first-class answer.** 50 of those 100 topics take no
construction tag at all -- 31 of them are this product talking about itself.
The classifier abstained correctly on 50/50 in every one of four runs, and
that stability is the most valuable thing it does. Anything that trades it for
"a few more labels" is a regression even if the label count goes up.

**It never touches the extraction prompt.** Admission and style have drifted
together in this repo before: changing how the model is asked to write changes
what it admits. So this is a second call over the ALREADY-EXTRACTED topics, and
a test below pins that the extraction schema is unchanged.
"""
import io
import os
import re

import pytest


def _src(name):
    return os.path.join(os.path.dirname(__file__), "..", "..", "src", name)


# NO importorskip. `tagging` has no external dependency, so the only reason it
# could fail to import is that it does not exist -- and importorskip would turn
# that into a SKIP, which reads exactly like a PASS in the run summary. The
# same mistake was made one commit ago in test_tags_repo.py.
import tagging


LEAVES = [
    {"slug": "structure.concrete", "label": "Concrete", "parent": "Structure"},
    {"slug": "programme.schedule", "label": "Schedule", "parent": "Programme & logistics"},
    {"slug": "safety.hazard", "label": "Hazard", "parent": "Safety"},
]
TOPICS = [
    {"topic_title": "Concrete Pour Dates", "summary": "Reviewed upcoming pours."},
    {"topic_title": "Microphone Test", "summary": "Counted to ten to check the mic."},
]


def _caller(reply):
    """A stand-in for llm_utils.call_llm, which returns (text, error)."""
    calls = []

    def call(prompt, **kw):
        calls.append({"prompt": prompt, **kw})
        return (reply, None) if not isinstance(reply, tuple) else reply

    call.calls = calls
    return call


# ---------------------------------------------------------------------------
# The reply, parsed
# ---------------------------------------------------------------------------

def test_slugs_reach_the_topics_they_were_asked_about():
    call = _caller('{"0": ["structure.concrete", "programme.schedule"], "1": []}')
    out = tagging.classify(TOPICS, LEAVES, call)
    assert out == [["structure.concrete", "programme.schedule"], []]


def test_an_empty_list_is_a_result_and_not_a_failure():
    call = _caller('{"0": [], "1": []}')
    assert tagging.classify(TOPICS, LEAVES, call) == [[], []]


def test_a_slug_that_is_not_in_the_taxonomy_is_dropped():
    """Never mapped to something near it. A label the vocabulary does not have
    would become a tag id nobody can resolve, and guessing which real leaf was
    meant is how a wrong tag gets a confident-looking provenance."""
    call = _caller('{"0": ["structure.concrete", "made.up"], "1": []}')
    assert tagging.classify(TOPICS, LEAVES, call) == [["structure.concrete"], []]


def test_more_than_three_slugs_are_cut_to_three():
    call = _caller('{"0": ["structure.concrete","programme.schedule","safety.hazard",'
                   '"structure.concrete"], "1": []}')
    assert len(tagging.classify(TOPICS, LEAVES, call)[0]) <= 3


def test_duplicate_slugs_collapse():
    call = _caller('{"0": ["safety.hazard", "safety.hazard"], "1": []}')
    assert tagging.classify(TOPICS, LEAVES, call) == [["safety.hazard"], []]


def test_an_index_the_model_invented_is_ignored():
    call = _caller('{"0": ["safety.hazard"], "7": ["structure.concrete"]}')
    assert tagging.classify(TOPICS, LEAVES, call) == [["safety.hazard"], []]


def test_a_reply_wrapped_in_prose_or_fences_is_still_read():
    call = _caller('Sure!\n```json\n{"0": ["safety.hazard"], "1": []}\n```\n')
    assert tagging.classify(TOPICS, LEAVES, call) == [["safety.hazard"], []]


# ---------------------------------------------------------------------------
# When the call does not answer
# ---------------------------------------------------------------------------

def test_an_empty_reply_leaves_every_topic_untagged_and_says_so():
    """A 200 WITH EMPTY CONTENT is a known shape from this endpoint. It must
    never be read as 'the model said no tags' -- that would record a broken
    call as a batch of confident abstentions, and abstention is the property
    this method is being trusted for."""
    call = _caller(("", None))
    out, stats = tagging.classify_with_stats(TOPICS, LEAVES, call)
    assert out == [[], []]
    assert stats["unanswered"] == 1 and stats["batches"] == 1


def test_an_error_is_not_an_abstention_either():
    call = _caller((None, "gateway timeout"))
    out, stats = tagging.classify_with_stats(TOPICS, LEAVES, call)
    assert out == [[], []]
    assert stats["unanswered"] == 1


def test_unparseable_json_leaves_the_topics_untagged():
    call = _caller("here are your tags, sorry no JSON")
    out, stats = tagging.classify_with_stats(TOPICS, LEAVES, call)
    assert out == [[], []]
    assert stats["unanswered"] == 1


def test_nothing_to_tag_makes_no_call_at_all():
    call = _caller("{}")
    out, stats = tagging.classify_with_stats([], LEAVES, call)
    assert out == [] and stats["batches"] == 0
    assert call.calls == []


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------

def test_the_prompt_tells_the_model_that_saying_nothing_is_correct():
    """The single most valuable behaviour measured, so it is stated in the
    prompt rather than hoped for. Half a real corpus takes no tag."""
    call = _caller('{"0": [], "1": []}')
    tagging.classify(TOPICS, LEAVES, call)
    prompt = call.calls[0]["prompt"].lower()
    assert "empty" in prompt
    assert "not about construction" in prompt or "not construction" in prompt


def test_the_prompt_carries_every_leaf_it_will_accept():
    call = _caller('{"0": [], "1": []}')
    tagging.classify(TOPICS, LEAVES, call)
    prompt = call.calls[0]["prompt"]
    for leaf in LEAVES:
        assert leaf["slug"] in prompt


def test_the_call_asks_for_no_thinking_and_carries_its_own_caller_name():
    """`caller` is what makes this call findable in the LLM_USAGE line; without
    it a tagging call and an extraction call are indistinguishable in the logs
    and nobody can tell what the tagging is costing."""
    call = _caller('{"0": [], "1": []}')
    tagging.classify(TOPICS, LEAVES, call)
    kw = call.calls[0]
    assert kw.get("enable_thinking") is False
    assert "tag" in str(kw.get("caller", "")).lower()


def test_topics_are_batched_rather_than_sent_one_per_call():
    """20 per call is what was measured. One call per topic would be 20x the
    calls for the same tokens, and the taxonomy would be re-sent every time."""
    call = _caller("{}")
    many = [{"topic_title": f"T{i}", "summary": "x"} for i in range(45)]
    _out, stats = tagging.classify_with_stats(many, LEAVES, call)
    assert stats["batches"] == 3, stats


# ---------------------------------------------------------------------------
# It is a SEPARATE call
# ---------------------------------------------------------------------------

def test_the_extraction_schema_does_not_mention_tags():
    """Admission and style drift together here: changing how the model is asked
    to write changes what it admits. Tagging is a second call over topics that
    already exist, so the extraction schema must be untouched."""
    import lambda_extract_session as ex
    assert "tag" not in ex.EXTRACTION_SCHEMA.lower()


def test_the_tagging_prompt_asks_for_labels_and_nothing_else():
    """No summary, no rewrite, no action items. The owner's boundary is that
    re-tagging never alters what was written; the narrowest form of that is a
    prompt with nowhere to put prose."""
    call = _caller('{"0": [], "1": []}')
    tagging.classify(TOPICS, LEAVES, call)
    prompt = call.calls[0]["prompt"].lower()
    for forbidden in ("summar", "rewrite", "improve", "correct the", "action item"):
        assert forbidden not in prompt, forbidden


# ---------------------------------------------------------------------------
# WHERE IT RUNS, AND WHERE IT MUST NOT.
#
# Tagging does NOT happen inside lambda_extract_session. It used to, and the
# arithmetic says it cannot: the recorder's confirmation email is sent once the
# extraction artifact lands (lambda_item_writer._final_email_context), that
# path measures p90 163s against a 180s budget, and one tagging call measures
# 27-48s. Seventeen seconds of headroom does not absorb thirty, so at p90 it
# does not "maybe" breach -- it breaches.
#
# It runs off that path instead, on the chain that already exists for exactly
# this: item-writer emits a retag_requests/ artifact AFTER the topics are
# committed and the email is enqueued, the non-VPC RetagFunction classifies,
# and item-writer writes the tags back. Labels arrive a few minutes late, and
# nothing waits on them -- the email carries `openTodos` and no tags, checked
# rather than assumed (email_sender, lambda_finalize_claim and session_brief
# mention tags nowhere).
# ---------------------------------------------------------------------------

def test_the_extraction_lambda_does_not_tag_anything():
    """The property, stated as an absence. If a `tag_topics` call ever comes
    back to this module, it comes back onto the email's path with it."""
    import lambda_extract_session as ex
    assert not hasattr(ex, "tag_topics"), (
        "tagging is back inside extract_session, which is the path the "
        "stop-recording email waits on")
    src = io.open(_src("lambda_extract_session.py"), encoding="utf-8").read()
    code = re.sub(r"#.*", "", src)
    assert "tagging." not in code and "import tagging" not in code, (
        "extract_session imports the tagger again")


def test_the_writer_asks_for_tagging_after_the_email_is_enqueued(monkeypatch):
    """Order is the whole point. Emitting the request before the email is
    enqueued would put the S3 write, and anything that retries behind it, in
    front of the thing with seventeen seconds of headroom."""
    src = io.open(_src("lambda_item_writer.py"), encoding="utf-8").read()
    i_email = src.index("_enqueue_final_email")
    i_tag = src.index("tag_request.emit") if "tag_request.emit" in src else -1
    assert i_tag > 0, "item-writer never asks for tagging"
    assert i_tag > i_email, (
        "the tagging request is emitted before the email is enqueued")


def test_tagging_is_off_until_it_is_switched_on(monkeypatch):
    """Ships inert, like every other switch in this pipeline."""
    import lambda_item_writer as iw
    monkeypatch.setattr(iw, "ENABLE_TOPIC_TAGGING", False)
    emitted = []
    monkeypatch.setattr(iw.retag_request, "emit",
                        lambda *a, **k: emitted.append(a))
    iw._request_topic_tagging(None, "co-1", [{"id": "t-1", "title": "A", "summary": "x"}])
    assert emitted == []


def test_a_failed_tagging_request_never_fails_the_extraction(monkeypatch):
    """A label is an addition to an extraction that is already correct, and
    this now runs AFTER the email. A failure here must cost the label and
    nothing else."""
    import lambda_item_writer as iw
    monkeypatch.setattr(iw, "ENABLE_TOPIC_TAGGING", True)
    monkeypatch.setattr(iw.retag_request, "emit",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("s3 down")))
    iw._request_topic_tagging(None, "co-1",
                              [{"id": "t-1", "title": "A", "summary": "x"}])   # no raise


def test_nothing_to_tag_asks_for_nothing(monkeypatch):
    import lambda_item_writer as iw
    monkeypatch.setattr(iw, "ENABLE_TOPIC_TAGGING", True)
    emitted = []
    monkeypatch.setattr(iw.retag_request, "emit", lambda *a, **k: emitted.append(a))
    iw._request_topic_tagging(None, "co-1", [])
    assert emitted == []


def test_the_writer_resolves_slugs_and_records_who_said_so(monkeypatch):
    """`source='extraction'`, so a later re-tag run can replace it and a human
    correction cannot be replaced by anything."""
    import lambda_item_writer as iw
    applied = []
    monkeypatch.setattr(iw.tags, "ids_for_slugs",
                        lambda conn, company_id, slugs: {"safety.hazard": "tag-1"})
    monkeypatch.setattr(iw.tag_writes, "apply_tags",
                        lambda conn, kind, eid, ids, **kw: applied.append(
                            (kind, eid, list(ids), kw)) or len(ids))
    iw._write_topic_tags(None, "co-1", "topic-1", ["safety.hazard", "not.real"])
    assert applied == [("topic", "topic-1", ["tag-1"],
                        {"source": "extraction", "run_id": None})]


def test_a_slug_the_database_does_not_know_is_dropped_not_invented(monkeypatch):
    import lambda_item_writer as iw
    applied = []
    monkeypatch.setattr(iw.tags, "ids_for_slugs", lambda conn, c, slugs: {})
    monkeypatch.setattr(iw.tag_writes, "apply_tags",
                        lambda *a, **k: applied.append(a) or 0)
    iw._write_topic_tags(None, "co-1", "topic-1", ["ghost.slug"])
    assert applied == []


def test_no_tags_on_a_topic_writes_nothing_at_all(monkeypatch):
    """Half of a real corpus abstains. That must cost zero queries, not one
    lookup per untagged topic."""
    import lambda_item_writer as iw
    calls = []
    monkeypatch.setattr(iw.tags, "ids_for_slugs",
                        lambda *a, **k: calls.append(a) or {})
    iw._write_topic_tags(None, "co-1", "topic-1", [])
    assert calls == []


def test_the_topic_loop_actually_calls_the_tag_writer():
    """Pinned on the SOURCE, because the function existing and the loop calling
    it are different facts and only the second one tags anything. A writer
    nobody calls is the shape this repo has shipped before -- a feature that is
    present, tested, and never runs."""
    import io as _io, os as _os
    src = _io.open(_os.path.join(_os.path.dirname(__file__), "..", "..",
                                 "src", "lambda_item_writer.py"),
                   encoding="utf-8").read()
    assert "_write_topic_tags(conn, company[\"id\"], row[\"id\"], t.get(\"tags\")" in src         or "_write_topic_tags(conn, company['id'], row['id'], t.get('tags')" in src,         "the topic loop never hands a topic's tags to the writer"
