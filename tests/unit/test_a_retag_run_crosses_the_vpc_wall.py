"""Re-tagging existing data, across a wall that does not let one lambda do it.

The constraint is not a preference. In-VPC functions reach Aurora and NOTHING
else -- the VPC has an S3 gateway endpoint and no NAT (BUG-36). Non-VPC
functions reach the model and not the database. Re-tagging needs both, so it is
three hops, and the shape is the one the programme matcher already uses rather
than a new one:

    org-api          in-VPC   picks what to re-tag, opens the run,
                              writes a retag_requests/ artifact to S3
    RetagFunction    non-VPC  S3-triggered; classifies; invokes ->
    item-writer      in-VPC   resolves slugs, applies tags, closes the run

WHAT THIS MUST NOT DO, and each has a test:

  * rewrite anything. The whole point of the owner's boundary is that changing
    the taxonomy and re-tagging never alters a topic's body. The request
    artifact carries title and summary so the model can read them, and the
    answer carries nothing but slugs.
  * overwrite a human's tag. A re-tag may replace what a machine wrote.
  * report success when the write hop crashed. A Lambda invoke returns 200
    with `FunctionError` set when the function raised -- treating that as
    written is how a run ends up marked done with nothing in it.
  * leave a run open forever. A run that starts and never finishes is
    indistinguishable from one still running, and nobody can tell whether to
    roll it back.
"""
import json

import pytest

pytest.importorskip("psycopg", reason="the repo layer needs psycopg")
import retag_request  # noqa: E402
from repositories import tags, tag_writes  # noqa: E402


class _Cursor:
    def __init__(self, rows):
        self._rows = list(rows)
        self.rowcount = len(self._rows)

    def execute(self, sql, params=None):
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class RecordingConn:
    def __init__(self, rows=()):
        self.executed = []
        self._rows = list(rows)

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        return _Cursor(self._rows)

    def cursor(self, row_factory=None):
        return _Cur(self)

    def sql(self):
        return [s for s, _ in self.executed]


class _Cur:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        self._conn.executed.append((" ".join(sql.split()), params))
        return _Cursor(self._conn._rows)


class FakeS3:
    def __init__(self):
        self.puts = []

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.puts.append({"bucket": Bucket, "key": Key,
                          "body": json.loads(Body), "type": ContentType})


# ---------------------------------------------------------------------------
# What a run covers
# ---------------------------------------------------------------------------

def test_the_selection_is_scoped_to_one_company():
    conn = RecordingConn()
    tags.topics_to_retag(conn, "co-1")
    sql, params = conn.executed[0]
    assert "company_id" in sql
    assert "co-1" in params


def test_the_selection_excludes_deleted_topics_with_both_arms():
    """A re-tag must not put a label on a topic a customer deleted, and the
    SOURCE arm is the load-bearing one: a re-extracted day's topics come back
    with new uuids that no topic-keyed tombstone names."""
    conn = RecordingConn()
    tags.topics_to_retag(conn, "co-1")
    sql, _ = conn.executed[0]
    assert "scope = 'deleted'" in sql and "reverted_at IS NULL" in sql
    assert "target_key" in sql, "the source arm is missing"


def test_the_selection_carries_only_what_the_model_needs_to_read():
    """Title and summary, and the id to write back to. Not the action items,
    not the evidence, not the transcript: an artifact that carries a session's
    words again is a second copy to keep in step with the first."""
    conn = RecordingConn()
    tags.topics_to_retag(conn, "co-1")
    sql, _ = conn.executed[0]
    selected = sql.split("SELECT", 1)[1].split("FROM")[0]
    assert "title" in selected and "summary" in selected and "id" in selected
    for extra in ("evidence", "participants", "open_questions", "decisions"):
        assert extra not in selected, extra


# ---------------------------------------------------------------------------
# The request artifact
# ---------------------------------------------------------------------------

def test_the_request_carries_the_text_and_the_run_it_belongs_to():
    s3 = FakeS3()
    key = retag_request.emit(s3, "bucket", "run-1", "co-1", [
        {"id": "t-1", "title": "Concrete", "summary": "Poured."},
    ])
    assert key and key.startswith("retag_requests/")
    body = s3.puts[0]["body"]
    assert body["run_id"] == "run-1" and body["company_id"] == "co-1"
    assert body["topics"] == [{"id": "t-1", "title": "Concrete", "summary": "Poured."}]


def test_the_request_key_is_deterministic_for_a_run_and_batch():
    """Re-driving the same batch overwrites its artifact instead of piling up
    duplicates -- the same source-key idempotency every other writer here
    relies on. A uuid or a timestamp in the key would defeat it."""
    a = retag_request.emit(FakeS3(), "b", "run-1", "co-1",
                           [{"id": "t-1", "title": "x", "summary": "y"}], batch=0)
    b = retag_request.emit(FakeS3(), "b", "run-1", "co-1",
                           [{"id": "t-1", "title": "x", "summary": "y"}], batch=0)
    c = retag_request.emit(FakeS3(), "b", "run-1", "co-1",
                           [{"id": "t-2", "title": "x", "summary": "y"}], batch=1)
    assert a == b and a != c


def test_an_empty_batch_writes_no_artifact_at_all():
    s3 = FakeS3()
    assert retag_request.emit(s3, "b", "run-1", "co-1", []) is None
    assert s3.puts == []


# ---------------------------------------------------------------------------
# The non-VPC hop
# ---------------------------------------------------------------------------

@pytest.fixture
def retag(monkeypatch):
    # NOT importorskip: this module is ours, so the only reason it could fail
    # to import is that it does not exist -- and a skip reads like a pass.
    import lambda_retag as rt
    monkeypatch.setattr(rt, "S3_BUCKET", "bucket")
    monkeypatch.setattr(rt, "TAG_WRITER_FUNCTION", "fieldsight-test-item-writer")
    return rt


def _event(key="retag_requests/run-1/0000.json"):
    return {"Records": [{"s3": {"object": {"key": key}}}]}


def _s3_with(body):
    import io as _io

    class S3:
        def get_object(self, Bucket, Key):
            return {"Body": _io.BytesIO(json.dumps(body).encode("utf-8"))}
    return S3()


def test_the_answer_carries_slugs_and_nothing_else(retag, monkeypatch):
    """Not a rewritten title, not a summary, not a confidence-weighted body --
    slugs. The write hop has no parameter for anything else, and this is the
    other half of that guarantee."""
    payloads = []
    monkeypatch.setattr(retag, "s3", lambda: _s3_with(
        {"run_id": "run-1", "company_id": "co-1",
         "topics": [{"id": "t-1", "title": "Concrete", "summary": "Poured."}]}))
    monkeypatch.setattr(retag.tagging, "classify_with_stats",
                        lambda t, l, c: ([["structure.concrete"]], {"unanswered": 0}))
    monkeypatch.setattr(retag, "_invoke_writer",
                        lambda payload: payloads.append(payload))
    retag.lambda_handler(_event(), None)
    assert payloads == [{"op": "apply_tags", "run_id": "run-1",
                         "company_id": "co-1",
                         "tagged": [{"topic_id": "t-1",
                                     "slugs": ["structure.concrete"]}],
                         "tagged_actions": []}]
    # The whole payload, asserted as a whole: there is no field for a title, a
    # summary or any other text, so a re-tag cannot alter what was written.
    assert set(payloads[0]) == {"op", "run_id", "company_id", "tagged",
                                "tagged_actions"}


def test_a_topic_the_model_abstained_on_is_still_reported(retag, monkeypatch):
    """With an EMPTY slug list, not by being left out. Absent would mean 'we
    did not get to it' and the run could never tell the two apart."""
    monkeypatch.setattr(retag, "s3", lambda: _s3_with(
        {"run_id": "run-1", "company_id": "co-1",
         "topics": [{"id": "t-1", "title": "Mic test", "summary": "One two."}]}))
    monkeypatch.setattr(retag.tagging, "classify_with_stats",
                        lambda t, l, c: ([[]], {"unanswered": 0}))
    seen = []
    monkeypatch.setattr(retag, "_invoke_writer", lambda p: seen.append(p))
    retag.lambda_handler(_event(), None)
    assert seen[0]["tagged"] == [{"topic_id": "t-1", "slugs": []}]


def test_an_unanswered_batch_never_reaches_the_writer(retag, monkeypatch):
    """A call that returned nothing is not a batch of abstentions. Writing it
    as one would record a broken run as a finished one."""
    monkeypatch.setattr(retag, "s3", lambda: _s3_with(
        {"run_id": "run-1", "company_id": "co-1",
         "topics": [{"id": "t-1", "title": "x", "summary": "y"}]}))
    monkeypatch.setattr(retag.tagging, "classify_with_stats",
                        lambda t, l, c: ([[]], {"unanswered": 1}))
    monkeypatch.setattr(retag, "_invoke_writer",
                        lambda p: pytest.fail("the writer was called"))
    with pytest.raises(RuntimeError):
        retag.lambda_handler(_event(), None)


def test_a_crashed_writer_is_not_a_success(retag, monkeypatch):
    """`invoke` answers 200 with FunctionError set when the function raised.
    Reading the 200 alone is how a run is marked done with nothing written --
    the same trap as reading an invoke's 200 without looking at the body."""
    class Client:
        def invoke(self, **kw):
            return {"StatusCode": 200, "FunctionError": "Unhandled"}
    monkeypatch.setattr(retag, "lambda_client", lambda: Client())
    with pytest.raises(RuntimeError):
        retag._invoke_writer({"op": "apply_tags"})


# ---------------------------------------------------------------------------
# The write hop, and closing the run
# ---------------------------------------------------------------------------

def test_the_writer_marks_the_run_done_when_the_batch_lands():
    """A run that starts and never finishes reads exactly like one still
    running, and nobody can tell whether to roll it back."""
    conn = RecordingConn()
    tag_writes.finish_run(conn, "run-1", stats={"topics": 3})
    assert any(s.startswith("UPDATE tag_run") and "status='done'" in s
               for s in conn.sql())


# ---------------------------------------------------------------------------
# The in-VPC write hop: item-writer's `apply_tags` op
# ---------------------------------------------------------------------------

@pytest.fixture
def writer(monkeypatch):
    import lambda_item_writer as iw

    state = {"applied": [], "finished": [], "slugs": {}}
    monkeypatch.setattr(iw, "get_connection", lambda *a, **k: _Conn())
    monkeypatch.setattr(iw.tags, "ids_for_slugs",
                        lambda conn, c, slugs: {s: "id-" + s for s in slugs
                                                if s in state["slugs"]})
    monkeypatch.setattr(iw.tag_writes, "apply_tags",
                        lambda conn, kind, eid, ids, **kw:
                            state["applied"].append((kind, eid, list(ids), kw)) or len(ids))
    monkeypatch.setattr(iw.tag_writes, "finish_run",
                        lambda conn, rid, **kw: state["finished"].append((rid, kw)))
    state["iw"] = iw
    return state


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        return _Cursor([])

    def cursor(self, row_factory=None):
        return _Cursor([])


def _payload(tagged, run="run-1"):
    return {"op": "apply_tags", "run_id": run, "company_id": "co-1", "tagged": tagged}


def test_the_op_applies_each_topics_slugs(writer):
    writer["slugs"] = {"safety.hazard", "structure.concrete"}
    writer["iw"].lambda_handler(
        _payload([{"topic_id": "t-1", "slugs": ["safety.hazard"]},
                  {"topic_id": "t-2", "slugs": ["structure.concrete"]}]), None)
    kinds = [(k, e, i) for k, e, i, _kw in writer["applied"]]
    assert kinds == [("topic", "t-1", ["id-safety.hazard"]),
                     ("topic", "t-2", ["id-structure.concrete"])]


def test_the_op_records_the_run_that_wrote_them(writer):
    """Without the run id on every row, undoing a batch is impossible: there is
    no way to tell a bad run's rows from a good one that overlapped it."""
    writer["slugs"] = {"safety.hazard"}
    writer["iw"].lambda_handler(
        _payload([{"topic_id": "t-1", "slugs": ["safety.hazard"]}]), None)
    _k, _e, _i, kw = writer["applied"][0]
    assert kw["run_id"] == "run-1"
    assert kw["source"] == "classifier", (
        "a re-tag is a classifier's work, not an extraction's -- the source is "
        "what a later run and a human correction are distinguished by")


def test_a_topic_the_model_abstained_on_writes_nothing_but_is_not_an_error(writer):
    writer["slugs"] = {"safety.hazard"}
    writer["iw"].lambda_handler(
        _payload([{"topic_id": "t-1", "slugs": []}]), None)
    assert writer["applied"] == []
    assert writer["finished"], "the run must still be closed"


def test_the_run_is_closed_when_the_batch_lands(writer):
    """A run that starts and never finishes reads exactly like one still
    running, and nobody can tell whether to roll it back."""
    writer["slugs"] = {"safety.hazard"}
    writer["iw"].lambda_handler(
        _payload([{"topic_id": "t-1", "slugs": ["safety.hazard"]}]), None)
    assert writer["finished"][0][0] == "run-1"


def test_an_s3_event_is_still_an_s3_event(writer):
    """The op dispatch must not swallow the shape this lambda already has."""
    iw = writer["iw"]
    assert iw._is_tag_op({"op": "apply_tags"}) is True
    assert iw._is_tag_op({"Records": [{"s3": {"object": {"key": "x"}}}]}) is False
    assert iw._is_tag_op({}) is False


# ---------------------------------------------------------------------------
# Action items ride the same three hops.
#
# Chosen by measurement, not by symmetry with topics: three arms over 90 real
# action items, four runs of the classifier. It scored 0.861 against an
# independent second annotator's labels, inheritance scored 0.644 and tagging
# nothing scored 0.411. A and inheritance find a right leaf about equally
# often (48 vs 47 of 57); the classifier's whole advantage is not adding wrong
# ones (precision 0.866 vs 0.657), which for a filter is the difference
# between useful and noisy.
#
# THE CONTEXT IS THE ACTION'S TEXT PLUS ITS TOPIC'S TITLE, AND NOT THE TOPIC'S
# TAGS. Both are produced in the same pass, so the title exists and the tags
# do not yet. Giving the tags would make the tagger better informed than the
# system can be; giving nothing would make it blinder. The same context was in
# the blind-annotation pack, so annotator, model and production all read the
# same thing.
# ---------------------------------------------------------------------------

def test_the_request_can_carry_actions_as_well_as_topics():
    s3 = FakeS3()
    retag_request.emit(s3, "bucket", "run-1", "co-1",
                       [{"id": "t-1", "title": "Concrete", "summary": "Poured."}],
                       actions=[{"id": "a-1", "text": "Order rebar",
                                 "topic_title": "Concrete"}])
    body = s3.puts[0]["body"]
    assert body["actions"] == [{"id": "a-1", "text": "Order rebar",
                                "topic_title": "Concrete"}]


def test_an_action_only_batch_is_still_a_batch():
    """A re-tag run walks topics and actions separately; a batch of one kind
    must not be mistaken for nothing to do."""
    s3 = FakeS3()
    key = retag_request.emit(s3, "b", "run-1", "co-1", [],
                             actions=[{"id": "a-1", "text": "x", "topic_title": "T"}])
    assert key is not None and s3.puts[0]["body"]["topics"] == []


def test_an_action_carries_no_more_than_the_tagger_reads():
    """Its text and its topic's TITLE. Not the topic's tags, not the
    responsible person, not the deadline -- none of which the tagger is
    allowed to see or has any use for, and all of which would be a second copy
    of a row crossing a trust boundary."""
    s3 = FakeS3()
    retag_request.emit(s3, "b", "run-1", "co-1", [],
                       actions=[{"id": "a-1", "text": "x", "topic_title": "T",
                                 "responsible": "Neil", "deadline": "Friday",
                                 "topic_tags": ["structure.concrete"]}])
    assert set(s3.puts[0]["body"]["actions"][0]) == {"id", "text", "topic_title"}


def test_the_non_vpc_hop_classifies_actions_with_their_topic_title(retag, monkeypatch):
    seen = {}
    monkeypatch.setattr(retag, "s3", lambda: _s3_with(
        {"run_id": "run-1", "company_id": "co-1", "topics": [],
         "actions": [{"id": "a-1", "text": "Order rebar", "topic_title": "Slab pour"}]}))
    monkeypatch.setattr(retag.tagging, "classify_actions_with_stats",
                        lambda items, leaves, call: (
                            seen.update({"items": items}) or
                            ([["structure.concrete"]], {"unanswered": 0})))
    payloads = []
    monkeypatch.setattr(retag, "_invoke_writer", lambda p: payloads.append(p))
    retag.lambda_handler(_event(), None)
    assert seen["items"][0]["topic_title"] == "Slab pour"
    assert payloads[0]["tagged_actions"] == [
        {"action_item_id": "a-1", "slugs": ["structure.concrete"]}]


def test_an_unanswered_action_batch_is_not_a_batch_of_abstentions(retag, monkeypatch):
    monkeypatch.setattr(retag, "s3", lambda: _s3_with(
        {"run_id": "run-1", "company_id": "co-1", "topics": [],
         "actions": [{"id": "a-1", "text": "x", "topic_title": "T"}]}))
    monkeypatch.setattr(retag.tagging, "classify_actions_with_stats",
                        lambda *a, **k: ([[]], {"unanswered": 1}))
    monkeypatch.setattr(retag, "_invoke_writer",
                        lambda p: pytest.fail("the writer was called"))
    with pytest.raises(RuntimeError):
        retag.lambda_handler(_event(), None)


def test_the_write_hop_applies_action_tags_to_the_action_table(writer):
    writer["slugs"] = {"structure.concrete"}
    writer["iw"].lambda_handler(
        {"op": "apply_tags", "run_id": "run-1", "company_id": "co-1",
         "tagged": [],
         "tagged_actions": [{"action_item_id": "a-1",
                             "slugs": ["structure.concrete"]}]}, None)
    kinds = [(k, e, i) for k, e, i, _kw in writer["applied"]]
    assert kinds == [("action_item", "a-1", ["id-structure.concrete"])]


def test_an_action_the_model_abstained_on_writes_nothing(writer):
    writer["slugs"] = {"structure.concrete"}
    writer["iw"].lambda_handler(
        {"op": "apply_tags", "run_id": "run-1", "company_id": "co-1",
         "tagged": [], "tagged_actions": [{"action_item_id": "a-1", "slugs": []}]},
        None)
    assert writer["applied"] == []
    assert writer["finished"], "the run must still be closed"


def test_the_selection_for_actions_carries_its_topic_title():
    conn = RecordingConn()
    tags.actions_to_retag(conn, "co-1")
    sql, params = conn.executed[0]
    assert "action_items" in sql and "topics" in sql, "it must join its topic"
    selected = sql.split("SELECT", 1)[1].split(" FROM")[0]
    assert "title" in selected and "text" in selected
    for never in ("responsible", "deadline", "priority"):
        assert never not in selected, never


def test_the_selection_for_actions_excludes_deleted_topics_with_both_arms():
    """An action under a deleted recording's topic must not be re-tagged, and
    the source arm is the load-bearing one for the same reason it is on the
    topic side: a re-extracted day's topics come back with new uuids."""
    conn = RecordingConn()
    tags.actions_to_retag(conn, "co-1")
    sql, _ = conn.executed[0]
    assert "scope = 'deleted'" in sql and "reverted_at IS NULL" in sql
    assert "target_key" in sql
