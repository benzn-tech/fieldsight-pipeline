"""Every payload the embedder can send, replayed into the writer.

Three separate defects tonight had one shape: the embedder and the writer are two halves of
one contract that nobody had written down, each half's tests exercised its own side, and both
stayed green while the seam between them was broken.

* the embedder built `label_map` and the writer read it — and the hop between them dropped
  the key, so label inheritance was unreachable code that reported success;
* the embedder began sending a refused enrolment as `{"voiceprint_id", "refused"}` and the
  writer crashed on it, so every refusal on TEST died and the outcome it existed to record
  was never recorded;
* the precedence table was keyed on a source string the writer had stopped using.

None of these is a logic error either side could have caught alone. They are all the same
question — *does what one half sends survive what the other half does with it* — and nothing
asked it.

So this asks it mechanically: drive the embedder over the paths that produce a payload,
capture every payload it actually hands to `invoke_writer`, and feed each one back through
the writer's own `lambda_handler`. The database is stubbed; what is under test is the shape
of the message, not what it stores.

Adding a field to one side and not the other is now a red test rather than a silent
production failure.
"""
import json

import numpy as np
import pytest

import lambda_speaker_embed as se
import lambda_voiceprint_writer as vw

CO = "11111111-1111-1111-1111-111111111111"
SID = "sid" + "c" * 32
SRC = f"x_{SID}_c0000.wav"
AUDIO_KEY = f"users/u/audio/2026-08-13/x_{SID}_c0000.wav"


class FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        import io as _io
        return {"Body": _io.BytesIO(self.objects[Key])}


def _wav(seconds=40.0, sr=16000, silent=False):
    import io as _io
    import wave
    n = int(seconds * sr)
    if silent:
        samples = np.zeros(n, dtype="<i2")
    else:
        t = np.arange(n, dtype=np.float64) / sr
        samples = (np.sin(2 * np.pi * 220.0 * t) * 8000).astype("<i2")
    buf = _io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


def _turns(n=3):
    return [{"source_filename": SRC, "start_sec": float(i * 12),
             "end_sec": float(i * 12 + 11), "speaker_label": "spk_0"} for i in range(n)]


def _artifact(**over):
    base = {"request_id": "r1", "session_base": SID, "company_id": CO,
            "user_folder": "u", "date": "2026-08-13",
            "correction": {"source_filename": SRC, "start_sec": 0.0, "end_sec": 11.0,
                           "display_name": "Ben L"},
            "turns": _turns(),
            "label_map": [{"turn_ref": f"{SRC}@{t['start_sec']}",
                           "source_filename": SRC, "speaker_label": "spk_0"}
                          for t in _turns()]}
    base.update(over)
    return base


@pytest.fixture
def captured(monkeypatch):
    """Runs the embedder for real over its S3 entry point, recording every writer payload."""
    sent = []
    # TWO profiles, not one. A single-profile pool names nobody by design — there is no
    # runner-up to beat — so a one-profile stub would make the match path produce no payload
    # at all and this file would assert on an empty list.
    pool = [{"person_key": "vp-1", "display_name": "Ben L", "status": "confirmed",
             "embedding": [1.0] * 192},
            {"person_key": "vp-2", "display_name": "Zoe", "status": "confirmed",
             "embedding": [1.0] + [-1.0] * 191}]
    monkeypatch.setattr(se, "invoke_writer", lambda p: sent.append(p) or
                        ({"profiles": pool} if p.get("op") == "profiles"
                         else {"written": 0}))
    monkeypatch.setattr(se, "embed_audio",
                        lambda audio, sr: np.ones(192, dtype=np.float32))
    return sent


def _run_embedder(monkeypatch, artifact, audio=None):
    key = "voiceprint_requests/c/s/r1.json"
    se._AUDIO_CACHE.clear()
    monkeypatch.setattr(se, "s3", lambda: FakeS3({
        key: json.dumps(artifact).encode(),
        AUDIO_KEY: audio if audio is not None else _wav()}))
    se.lambda_handler({"Records": [{"s3": {"bucket": {"name": "b"},
                                           "object": {"key": key}}}]}, None)


@pytest.fixture
def writer_db(monkeypatch):
    """The writer with its database replaced. What is under test is whether it can READ the
    message, not what it stores."""
    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    calls = {"turns": [], "samples": [], "attempts": [], "floor_by_company": {}}
    monkeypatch.setattr(vw, "get_connection", lambda: Conn())
    monkeypatch.setattr(vw, "record_turn_name",
                        lambda conn, co, **kw: calls["turns"].append(kw) or {"id": "t"})
    monkeypatch.setattr(vw, "add_sample",
                        lambda conn, co, *a, **kw: calls["samples"].append(kw) or {"id": "s"})
    monkeypatch.setattr(vw, "record_attempt",
                        lambda conn, co, vp, outcome, detail=None:
                        calls["attempts"].append(outcome))
    monkeypatch.setattr(vw, "live_turn_names", lambda conn, co, sb: [])
    monkeypatch.setattr(vw, "rejected_names", lambda conn, co, sb: set())
    monkeypatch.setattr(vw, "profiles_for_matching", lambda conn, co, site_id=None: [])
    # `_profiles` reads the company's calibrated floor alongside its profiles (Task 2)
    # over the same `conn` this fixture's fake `Conn` does not serve. Defaults to the
    # no-floor case -- nobody has run the recompute job, or the company is below the
    # minimum sample count (spec S1.5) -- and `floor_by_company` lets a test opt a
    # specific company into having one without building a real cursor.
    monkeypatch.setattr(vw, "company_floor",
                        lambda conn, co: calls["floor_by_company"].get(co))
    return calls


# --- the paths that produce a payload -------------------------------------


CASES = {
    "correction, no enrolment": dict(artifact={}, audio=None),
    "correction with enrolment": dict(
        artifact={"enrol": {"voiceprint_id": "vp-1"}}, audio=None),
    "correction whose enrolment is refused": dict(
        artifact={"enrol": {"voiceprint_id": "vp-1"}}, audio=_wav(silent=True)),
    "match run": dict(artifact={"op": "match", "mode": "on", "site_id": "st-1"},
                      audio=None),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_every_payload_the_embedder_sends_is_one_the_writer_understands(
        name, captured, writer_db, monkeypatch):
    """The seam, asked directly. Three defects tonight lived here and none of them was
    visible from either side alone."""
    case = CASES[name]
    _run_embedder(monkeypatch, _artifact(**case["artifact"]), case["audio"])
    assert captured, f"{name}: the embedder sent nothing, so this proves nothing"
    for payload in captured:
        # Raises on an unknown op, a missing key, or a shape the writer cannot read — which
        # is exactly what happened in production, twice, while every unit test was green.
        vw.lambda_handler(payload, None)


def test_a_companys_floor_reaches_the_profiles_payload(captured, writer_db, monkeypatch):
    """The no-floor case above proves the seam does not crash. This proves it also carries
    a real value through: a company with a calibrated floor must see it on the same
    `profiles` op the no-floor company sees as `None`, not just avoid raising."""
    writer_db["floor_by_company"][CO] = 0.42
    _run_embedder(monkeypatch, _artifact(op="match", mode="on", site_id="st-1"))
    profiles_payloads = [p for p in captured if p.get("op") == "profiles"]
    assert profiles_payloads, "match run sent no profiles request; this proves nothing"
    for payload in profiles_payloads:
        result = vw.lambda_handler(payload, None)
        assert result["company_floor"] == 0.42


@pytest.mark.parametrize("name", sorted(CASES))
def test_nothing_the_artifact_carries_is_dropped_at_the_hop(name, captured, monkeypatch):
    """The embedder is a middle hop, and a middle hop that does not speak a key silently
    removes the feature that key exists for.

    `label_map` was built by org-api and read by the writer, and this hop did not forward it:
    label inheritance was unreachable code that reported success, and each end's tests passed
    because each end worked.

    Asserted on the KEY surviving rather than on any behaviour it produces — a dropped key
    raises nothing and changes no result that a stub can see."""
    case = CASES[name]
    art = _artifact(**case["artifact"])
    _run_embedder(monkeypatch, art, case["audio"])
    forwarded = [p for p in captured if p.get("op") in ("propagation", "match_names")]
    assert forwarded, f"{name}: nothing was forwarded"
    for key in ("label_map",):
        if art.get(key) is None:
            continue
        assert any(p.get(key) is not None for p in forwarded), (
            f"{name}: the artifact carried {key!r} and no payload did")


def test_a_refused_enrolment_never_carries_the_vector_it_was_refused_for(captured,
                                                                        monkeypatch):
    """Keyed on the CASE — silent audio, which the guard must refuse — not on a field of the
    payload. A first version asked `if enrol.get("refused")`, so a change that dropped that
    field skipped the assertion entirely: the guard hung on the thing it was checking."""
    _run_embedder(monkeypatch,
                  _artifact(enrol={"voiceprint_id": "vp-1"}), _wav(silent=True))
    assert captured
    for payload in captured:
        blob = json.dumps(payload)
        assert "embedding" not in blob, (
            "a window the guard refused had its vector sent anyway — biometric data on a "
            "path not designed to hold it")
        assert (payload.get("enrol") or {}).get("refused"), (
            "the refusal reached the writer without a reason, so the profile stays empty "
            "and unexplained")


@pytest.mark.parametrize("name", sorted(CASES))
def test_every_harvested_sample_belongs_to_a_profile(name, captured, monkeypatch):
    case = CASES[name]
    _run_embedder(monkeypatch, _artifact(**case["artifact"]), case["audio"])
    for payload in captured:
        for h in payload.get("harvest") or []:
            assert h.get("voiceprint_id"), (
                f"{name}: a harvested sample belongs to no profile")


def test_the_writer_rejects_an_op_it_does_not_know(writer_db):
    """The counterpart to the contract test: a payload the writer cannot handle must RAISE,
    not be silently ignored. A quiet no-op here is how a dropped key looks like success."""
    with pytest.raises(ValueError):
        vw.lambda_handler({"op": "something_nobody_sends", "company_id": CO}, None)


def test_the_admitted_limit_reaches_the_column_it_is_stored_in(monkeypatch):
    """A new field on this seam, tested AT the seam rather than on either side of it.

    `admitted_max_spread` exists so that a sample stored under a loosened homogeneity guard
    can be found again later, and it crosses three components to get there: the embedder
    stamps it, the writer forwards it, the repository writes it. Every one of tonight's five
    defects was a field that one side sent and the other never read, so the check is that the
    value arrives in the SQL parameters — not that each half handles it.
    """
    import lambda_speaker_embed as se
    import repositories.voiceprints as vpr

    monkeypatch.setattr(se, "MAX_FRAME_SPREAD", 0.7)
    assert se._admitted_limit() == 0.7

    captured_sql = {}

    class _Cur:
        def execute(self, sql, params=None):
            captured_sql["sql"] = sql
            captured_sql["params"] = params
            return self

        def fetchone(self):
            return {"id": "s-1"}

    class _Conn:
        def cursor(self, row_factory=None):
            return _Cur()

    monkeypatch.setattr(vpr, "_agreement", lambda *a, **k: (None, None, None))
    vpr.add_sample(_Conn(), "co-1", "vp-1", [0.1] * 192, "correction", "k", (0.0, 15.0),
                   admitted_max_spread=se._admitted_limit())
    assert "admitted_max_spread" in captured_sql["sql"]
    assert 0.7 in captured_sql["params"], captured_sql["params"]

    # And the ordinary case stores NULL, so the non-NULL rows are exactly the ones worth
    # re-examining rather than every row ever written.
    monkeypatch.setattr(se, "MAX_FRAME_SPREAD", se.vp.DEFAULT_MAX_FRAME_SPREAD)
    assert se._admitted_limit() is None


def test_no_field_crosses_this_seam_unread_in_either_direction():
    """The census, kept as a test so it cannot quietly stop being true.

    Three defects tonight were one shape on this one seam: a value computed on one side and
    discarded on the other. `invoke_writer` returned boto3's envelope, so `profiles` was
    always None and the match path was inert in production. `inherited` was read as
    `written`, which is structurally zero on the branch that reads it. And the writer's whole
    reply was thrown away on the correction path, where the log line is the only production
    signal because Lambda discards what an event-driven invocation returns.

    Each was found by asking the same question of one more field. This asks it of all of
    them at once, so the fourth instance fails here instead of in production.

    The four exceptions are deliberate and documented: `score`, `margin`,
    `label_disagreement` and the top-level `voiceprint_id` fallback are read by the writer
    and sent by nobody. Their columns exist (0040) and a matcher-driven propagation would
    fill them. They are listed by name rather than skipped by a pattern, so adding a fifth is
    a decision somebody makes here rather than an omission.
    """
    import re

    emb = open("src/lambda_speaker_embed.py", encoding="utf-8").read()
    wr = open("src/lambda_voiceprint_writer.py", encoding="utf-8").read()

    # --- embedder -> writer: every key sent must be read -------------------
    block = emb[emb.index("    payload = {"):]
    block = block[:block.index("invoke_writer(payload)")]
    sent = set(re.findall(r'^\s{8}"(\w+)":', block, re.M))
    assert "label_map" in sent, "the payload block moved; this test reads it by position"
    for key in sent:
        read = (re.search(r'\.get\("%s"\)|\["%s"\]|_require\(event, "%s"\)' % (key, key, key),
                          wr) is not None)
        assert read, f"the embedder sends {key!r} and the writer never reads it"

    # --- writer -> embedder: every key returned must be read ---------------
    # Exemptions are scoped to the specific function that returns the key, not to the key
    # name globally. A name-based exemption (`returned - {"results", ...}`) would silence
    # this guard for *any* function that happens to return a key called `results`, which is
    # exactly the defect shape this test exists to catch. So each `return {...}` is attributed
    # to its enclosing `def`, and only that function's own documented exception set is
    # subtracted from its own keys.
    func_starts = [(m.start(), m.group(1)) for m in re.finditer(r'^def (\w+)\(', wr, re.M)]

    def _enclosing_func(pos):
        name = None
        for start, fname in func_starts:
            if start <= pos:
                name = fname
            else:
                break
        return name

    returned_by_func = {}
    for m in re.finditer(r'return \{("(?:\w+)":[^}]*)\}', wr):
        keys = set(re.findall(r'"(\w+)":', m.group(1)))
        fname = _enclosing_func(m.start())
        returned_by_func.setdefault(fname, set()).update(keys)

    callers = emb + open("src/lambda_org_api.py", encoding="utf-8").read()

    # `stored` and `reason` belong to the writer's `_enrol` op, which has no production
    # caller — recorded in its own docstring. Reading them would mean wiring that op.
    #
    # `companies`, `floors_written`, `failed` and `results` belong to `_recompute_floors`,
    # whose only caller is an EventBridge Schedule (`FloorRecomputeFunction`'s own docstring
    # names the shape: one sweep, on its own schedule, with no per-item fan-out). Lambda
    # discards what an event-driven invocation returns, so there is no code path in this
    # repo that could read them — not a gap the way `stored`/`reason` are a gap, but the
    # same structural fact: a value with nowhere to be read is not read. `floors_written`
    # is the one operators actually need, so it is also written to the INFO log line right
    # beside this return (`"floor recompute: %d/%d compan%s now have a floor (%d failed)"`)
    # — the number exists somewhere a human can see it even though the return value it also
    # lives in is thrown away.
    exempt_by_func = {
        "_enrol": {"stored", "reason"},
        "_recompute_floors": {"companies", "floors_written", "failed", "results"},
    }
    for fname, keys in returned_by_func.items():
        exempt = exempt_by_func.get(fname, set())
        for key in keys - exempt:
            read = re.search(r'\.get\("%s"|\["%s"\]' % (key, key), callers) is not None
            assert read, (
                f"the writer's {fname} returns {key!r} and no caller reads it")


def test_a_matched_turn_carries_its_score_across_the_seam(captured, writer_db, monkeypatch):
    """The writer has always asked for `score`; for 604 rows nothing sent one.

    This is the seam's own failure shape, and it stayed invisible for the usual reason: the
    writer's half was correct (`score=r.get("score")` has been there all along) and the
    embedder's half was correct about everything it did send. Nobody asked whether the key
    existed in between, so `speaker_turn_names.score` was NULL on every row ever written,
    `recompute_company_floor` never reached its minimum sample count, and no company ever
    calibrated a floor -- which is the whole reason a confident name cannot currently be
    justified.

    `match_names` is the op that carries it, so this asserts on the op rather than on a
    database the double does not really have.
    """
    writer_db["floor_by_company"][CO] = 0.1
    _run_embedder(monkeypatch, _artifact(op="match", mode="on", site_id="st-1"))
    # `person_key`, not `name`: the writer payload is a REBUILT row, not `_match`'s own
    # dict, and that rebuild is exactly where the score was being dropped.
    named = [r for p in captured if p.get("op") == "match_names"
             for r in (p.get("results") or []) if r.get("person_key")]
    assert named, "the match run named nothing, so this proves nothing about the score"
    for r in named:
        assert r.get("score") is not None, (
            "a named turn crossed the seam without the score it was named on; the writer "
            "will store NULL and the company's floor can never be computed from it")
        # Cosine's range, with float slop -- an identical pair came back as
        # 1.0000000000000002. The bound is here to catch a wrong FIELD (a margin, an index,
        # a duration) landing in this slot, not to police the arithmetic.
        assert -1.001 <= float(r["score"]) <= 1.001, r["score"]


def test_the_embedder_puts_a_centroid_on_every_group_it_sends(monkeypatch):
    """The producer's half. `_rebind` computes a centroid per (call, label) to cluster on
    and, until 0065, dropped it on the floor -- so the column could be added, the writer
    could read it, and every row would still be NULL with nothing failing anywhere.

    Two labels across TWO calls, because `_rebind` skips any session with fewer than two of
    either: the shared fixture has one call, on which the re-bind correctly reports "nothing
    to group", and a test built on it would assert on an empty list forever.

    The audio read and the ONNX forward pass are stubbed -- the latter because CI has no
    `onnxruntime` and the former because there is no S3. **The arithmetic under test is
    not**: the mean, the unit normalisation, the clustering and the row construction are
    the shipped ones. Stubbing those would prove the centroid's presence about a fake.

    (The first version of this test stubbed only the read, passed locally on a machine that
    happens to have `onnxruntime`, and failed in CI with `ModuleNotFoundError`. Local green
    says nothing about what CI imports. The fix was verified by blocking `onnxruntime` at
    the import hook and re-running -- not by running it again locally, which is the evidence
    that had already failed.)

    **WHAT THIS TEST DOES NOT COVER, and must not be read as covering.** By stubbing
    `embed_audio` it never reaches this lambda's lazy `onnxruntime` import, so **it says
    nothing about whether `lambda_speaker_embed` can be imported in production**. That is
    not a small caveat here: `SpeakerEmbedFunction` once deployed with cfn-lint clean and
    2637 tests passing while **every single invocation raised ModuleNotFoundError**,
    precisely because every unit test monkeypatched the import away. The risk is not this
    test -- it is nobody remembering afterwards that import health has no unit-test guard
    at all.

    What does guard it: `test_voiceprint_onnx_parity.py` (skipped in CI by design -- the
    wheel is ~200MB and its exclusion is a decision, not an oversight) and calling the
    deployed function for real after a deploy. Neither runs here.
    """
    import lambda_speaker_embed as se

    def fake_window(folder, date, src, start, end):
        return "k", np.zeros(16000, dtype=np.float32), 16000

    def fake_embed(audio, sr, _src=[None]):
        # Two distinguishable voices, so the clustering has something real to separate and
        # the normalisation has something other than a unit vector to normalise.
        v = np.arange(192, dtype=np.float32) if _src[0] else np.ones(192, dtype=np.float32)
        _src[0] = not _src[0]
        return v

    monkeypatch.setattr(se, "_window_audio", fake_window)
    monkeypatch.setattr(se, "embed_audio", fake_embed)
    rows = se._rebind({"user_folder": "u", "date": "2026-08-13", "turns": [
        {"source_filename": f"{c}.json", "speaker_label": f"spk_{i}",
         "start_sec": 0.0, "end_sec": 9.0}
        for c in ("a", "b") for i in (0, 1)]})

    assert rows, "the re-bind emitted no groups, so this proves nothing about the centroid"
    for g in rows:
        c = g.get("centroid")
        assert c, (
            "a group left the producer without the vector it was clustered on; the column "
            "lands NULL and every candidate lookup falls back to re-embedding the audio")
        assert len(c) == 192, len(c)
        # A LIST here, turned into pgvector's text form by the repository. The two forms are
        # easy to confuse and only one survives a real database -- see the insert test.
        assert isinstance(c, list), type(c)


def test_the_rebind_centroid_survives_the_hop_and_reaches_the_insert(monkeypatch):
    """The vector the grouping was decided on must arrive in the INSERT, not just exist.

    Adding a column is easy; the writer's half not reading it is the failure this repository
    made twice in one night (a template body dropped from a PATCH reply, a score dropped from
    the match payload). Both times the column was right, the producer was right, and the hop
    between them silently carried nothing.

    This drives the embedder's real `_rebind` to build the rows, hands them to the writer's
    real `_rebind`, and reads the parameters the repository passed to the driver.

    **A connection double never executes SQL**, so this cannot prove Postgres accepts the
    value -- it proves the value is present, non-empty, and in pgvector's text form rather
    than the Python list that would fail at runtime against a real database. The shape check
    is the part a double can carry.
    """
    import repositories.speaker_label_groups as slg

    captured_params = []

    class Cur:
        def execute(self, sql, params=None):
            if "INSERT INTO speaker_label_groups" in sql:
                captured_params.append((sql, params))
            return self

    class Conn:
        def cursor(self, row_factory=None):
            return Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    rows = [{"source_filename": "a.json", "speaker_label": "spk_0", "group_label": "A",
             "spread": 0.1, "turns": 3, "seconds": 12.0,
             "centroid": [0.1] * 192},
            # A row with no centroid is legitimate (pre-0065 producers) and must not be
            # dropped or turned into a zero vector -- "not cached" is a real answer.
            {"source_filename": "b.json", "speaker_label": "spk_1", "group_label": "B",
             "spread": None, "turns": 1, "seconds": 4.0}]
    written = slg.replace_for_session(Conn(), CO, "sid" + "a" * 32, rows)
    assert written == 2, "a row without a centroid must still be stored"
    assert len(captured_params) == 2

    # By NAME-of-position, not `[-1]`: 0068 appended `user_folder` and `session_date`
    # after the centroid, and an index counted from the end silently started asserting
    # about a date. Found by this test going red on that change, which is the only reason
    # it was not a wrong assertion that kept passing.
    sql, params = captured_params[0]
    cols = sql.split("(", 1)[1].split(")", 1)[0]
    idx = [c.strip() for c in cols.split(",")].index("centroid")
    with_vec = params[idx]
    assert with_vec is not None, (
        "the centroid did not reach the INSERT; the column will be NULL on every row and "
        "every candidate lookup will fall back to re-embedding the audio")
    assert isinstance(with_vec, str) and with_vec.startswith("[") and with_vec.endswith("]"), (
        f"pgvector needs its text form; a Python list fails against a real database and a "
        f"connection double cannot tell you so: {with_vec!r:.60}")
    assert len(with_vec.strip("[]").split(",")) == 192

    assert captured_params[1][1][idx] is None, (
        "a missing centroid must stay NULL -- a zero vector would read as a voice that "
        "matches nothing, which is a different and answerable claim")
