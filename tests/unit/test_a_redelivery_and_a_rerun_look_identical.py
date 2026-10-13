"""A refused registration and a finished one must not look the same in the logs.

Found by running the pipeline, not by reading it. On 2026-09-11, 116 chunks were
copied from `users/Ben_UCPK2/` to `users/Ben_Lin/` to seed a test project. The
batch ledger is keyed `BATCH#{session_id}` with NO tenant dimension, so every
chunk was already present from the original run; `register_chunk`'s
`attribute_not_exists(SK)` condition refused each one, the seal planner found
nothing to do, and the session was retired.

Observable result: 232 VAD segments written, 116 transcribe invocations, 0
errors, 0 throttles — and ZERO transcripts. The summary line read
`{"total": 1, "started": 0, "skipped": 0, "exists": 0, "errors": 0}`.

Two separate silences produced that, and this file pins both:

  1. the ledger refuses a registration without saying so — correct for a
     duplicate S3 delivery, wrong for a legitimate re-run under another folder,
     and it cannot tell them apart;
  2. `batched_pending`, the ORDINARY outcome when batching is on, was counted by
     nothing, so a healthy run and a dead one printed the same four zeros.

Neither test asserts on a decision. Both assert that the decision is visible.
"""
import logging


import batch_ledger


class ConditionalCheckFailedException(Exception):
    """The NAME is the contract: _is_conditional_failure matches on
    `type(exc).__name__`, because boto3 builds these classes per service at
    runtime. A double called anything else silently takes the `raise` branch."""

    def __init__(self):
        super().__init__("ConditionalCheckFailedException")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeTable:
    """Minimal DynamoDB double: one pre-existing member, everything else free."""

    def __init__(self, existing=None):
        self.existing = existing or {}
        self.puts = []

    def put_item(self, Item=None, ConditionExpression=None, **kw):
        key = (Item["PK"], Item["SK"])
        if ConditionExpression and key in self.existing:
            raise ConditionalCheckFailedException()
        self.existing[key] = Item
        self.puts.append(Item)
        return {}

    def get_item(self, Key=None, **kw):
        item = self.existing.get((Key["PK"], Key["SK"]))
        return {"Item": item} if item else {}

    def update_item(self, **kw):
        return {}


SID = "d22615c636eb472d92759eb00938e2e6"
ORIGINAL = f"audio_segments/Ben_UCPK2/2026-09-10/x_sid{SID}_c0000_off0.0_to30.0_srcwav.wav"
RERUN = f"audio_segments/Ben_Lin/2026-09-10/x_sid{SID}_c0000_off0.0_to30.0_srcwav.wav"


def test_a_first_registration_is_silent_and_succeeds():
    """The common path must not start warning — that would train people to ignore it."""
    table = FakeTable()
    with _capture() as rec:
        status = batch_ledger.register_chunk(table, SID, 0, ORIGINAL, 1000)
    assert status == "registered"
    assert [r for r in rec.records if r.levelno >= logging.WARNING] == []


def test_a_refused_registration_says_so_and_names_both_keys():
    """The whole point: a refusal is legible, and carries enough to act on.

    Without the incumbent key in the message, an operator sees "already present"
    and cannot tell whether it is their own retry or another folder's chunk —
    which is exactly the question that took an hour to answer by hand.
    """
    table = FakeTable()
    batch_ledger.register_chunk(table, SID, 0, ORIGINAL, 1000)

    with _capture() as rec:
        status = batch_ledger.register_chunk(table, SID, 0, RERUN, 2000)

    assert status == "already_present", "the decision itself must not change"
    warnings = [r for r in rec.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, f"expected exactly one warning, got {warnings}"
    msg = warnings[0].getMessage()
    assert ORIGINAL in msg, "must name the key already in the ledger"
    assert RERUN in msg, "must name the key being refused"
    assert SID in msg


def test_the_refusal_survives_a_ledger_that_cannot_answer():
    """The diagnostic must never turn a working no-op into an exception."""
    table = FakeTable()
    batch_ledger.register_chunk(table, SID, 0, ORIGINAL, 1000)
    table.get_item = lambda **kw: (_ for _ in ()).throw(RuntimeError("dynamo down"))

    with _capture() as rec:
        status = batch_ledger.register_chunk(table, SID, 0, RERUN, 2000)

    assert status == "already_present"
    assert any(r.levelno >= logging.WARNING for r in rec.records)


# --------------------------------------------------------------------------
# The summary's four zeros
# --------------------------------------------------------------------------

def _summarise(results):
    """The summary block from lambda_transcribe, driven directly.

    Importing that module pulls a live boto3 client graph, so the shape under
    test is reproduced here and pinned against the source by
    `test_the_summary_counts_every_status_the_source_can_emit` below.
    """
    counted = ('started', 'skipped', 'exists', 'error', 'batched_pending')
    return {
        'total': len(results),
        'started': sum(1 for r in results if r.get('status') == 'started'),
        'skipped': sum(1 for r in results if r.get('status') == 'skipped'),
        'exists': sum(1 for r in results if r.get('status') == 'exists'),
        'errors': sum(1 for r in results if r.get('status') == 'error'),
        'batched_pending': sum(1 for r in results if r.get('status') == 'batched_pending'),
        'other': sum(1 for r in results if r.get('status') not in counted),
    }


def test_a_batching_run_no_longer_reads_as_four_zeros():
    s = _summarise([{'status': 'batched_pending'}])
    assert s['total'] == 1
    assert s['batched_pending'] == 1
    assert (s['started'], s['skipped'], s['exists'], s['errors']) == (0, 0, 0, 0)
    assert s['other'] == 0


def test_an_unknown_status_surfaces_instead_of_vanishing():
    """A status added later and forgotten here must not fall into the gap."""
    s = _summarise([{'status': 'invented_tomorrow'}])
    assert s['total'] == 1
    assert s['other'] == 1


def test_the_summary_counts_every_status_the_source_can_emit():
    """Source scan, and it only pins WIRING — the behaviour is driven above.

    Its job is narrow: if someone appends a new `'status': 'x'` to `results`,
    this fails until `x` is either counted by name or provably swept into
    `other`. The `found` self-check is load-bearing — a scan that matches
    nothing also reports zero failures.
    """
    import pathlib
    import re

    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "lambda_transcribe.py"
    text = src.read_text(encoding="utf-8")

    emitted = set(re.findall(r"'status':\s*'([a-z_]+)'", text))
    assert len(emitted) >= 4, f"scan found too few statuses ({emitted}) — it is not reading the file"
    assert "batched_pending" in emitted, "the status this whole file exists for is gone"

    counted = set(re.findall(r"r\.get\('status'\) == '([a-z_]+)'", text))
    missing = emitted - counted
    assert not missing, (
        f"{sorted(missing)} can be emitted but is counted by no named bucket. "
        "Either count it, or confirm `other` catches it and add it to `counted`."
    )


# --------------------------------------------------------------------------

class _capture:
    """Collect records from the root logger for the duration of the block."""

    def __enter__(self):
        self.records = []
        self._h = logging.Handler()
        self._h.emit = self.records.append
        logging.getLogger().addHandler(self._h)
        self._lvl = logging.getLogger().level
        logging.getLogger().setLevel(logging.DEBUG)
        return self

    def __exit__(self, *a):
        logging.getLogger().removeHandler(self._h)
        logging.getLogger().setLevel(self._lvl)
        return False
