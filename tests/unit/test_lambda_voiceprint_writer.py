"""Unit: the in-VPC half of the voiceprint chain.

The embedder cannot write. It runs on python3.12 for onnxruntime, and the psycopg layer is
cp311-only, so it holds no connection and has no VpcConfig — which is also why it cannot be
the thing that persists anything. This function is the other half: in VPC, psycopg, no model,
no audio.

It is invoked DIRECTLY by the embedder (non-VPC → in-VPC is permitted; the callee initiates
nothing outward, BUG-43 note 4), not through a second S3 artifact. That choice is what keeps
the enrolment vector out of S3 entirely: it travels in the invoke payload and lands in the
column that already requires consent.

Everything here is stubbed at the repository boundary. The SQL those functions emit is pinned
in test_voiceprints_repo.py, and duplicating it would mean two places to update and one to
forget.
"""
import pytest

vw = pytest.importorskip("lambda_voiceprint_writer")

CO = "11111111-1111-1111-1111-111111111111"


class FakeConn:
    def __init__(self):
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.committed = a[0] is None
        return False


@pytest.fixture
def calls(monkeypatch):
    seen = {"turns": [], "samples": []}
    monkeypatch.setattr(vw, "get_connection", lambda: FakeConn())
    monkeypatch.setattr(vw, "record_turn_name",
                        lambda conn, company_id, **kw: seen["turns"].append(kw) or {"id": "t"})
    monkeypatch.setattr(vw, "add_sample",
                        lambda conn, company_id, *a, **kw: seen["samples"].append(kw) or {"id": "s"})
    return seen


# ---- propagation results ------------------------------------------------


def test_each_named_turn_becomes_a_row(calls):
    out = vw.lambda_handler({
        "op": "propagation", "company_id": CO, "session_base": "s1",
        "cluster_threshold": 0.85,
        "results": [
            {"turn_ref": "f.wav@1.0", "state": "tentative", "cluster_ref": "C1",
             "score": 0.7, "margin": 0.1},
            {"turn_ref": "f.wav@9.0", "state": "unknown", "cluster_ref": None},
        ]}, None)
    assert out["written"] == 2
    assert [t["turn_ref"] for t in calls["turns"]] == ["f.wav@1.0", "f.wav@9.0"]


def test_a_propagated_row_never_carries_a_profile_id(calls):
    """The promotion loop, cut at the writer as well as in the query. A propagated
    `confirmed` row with a voiceprint_id counts as an independent human confirmation and the
    system promotes profiles with its own output."""
    vw.lambda_handler({
        "op": "propagation", "company_id": CO, "session_base": "s1",
        "results": [{"turn_ref": "f.wav@1.0", "state": "confirmed", "cluster_ref": "C1"}]},
        None)
    assert calls["turns"][0].get("voiceprint_id") is None
    assert calls["turns"][0]["source"] == "correction_propagation"


def test_the_threshold_that_produced_the_run_is_stamped_on_every_row(calls):
    """A row that cannot say which clustering produced it cannot be disqualified from
    calibration when the threshold moves."""
    vw.lambda_handler({
        "op": "propagation", "company_id": CO, "session_base": "s1",
        "cluster_threshold": 0.85,
        "results": [{"turn_ref": "a", "state": "tentative", "cluster_ref": "C1"},
                    {"turn_ref": "b", "state": "tentative", "cluster_ref": "C1"}]}, None)
    assert {t["cluster_threshold"] for t in calls["turns"]} == {0.85}


def test_the_turn_the_user_corrected_is_written_as_a_correction_not_a_propagation(calls):
    """It is the one row a human asserted. Marking it `correction_propagation` would both
    lose that fact and leave the profile-promotion count with nothing to count."""
    vw.lambda_handler({
        "op": "propagation", "company_id": CO, "session_base": "s1",
        "correction_ref": "corr-1", "voiceprint_id": "vp1",
        "results": [{"turn_ref": "f.wav@1.0", "state": "confirmed", "cluster_ref": "C1",
                     "asserted": True},
                    {"turn_ref": "f.wav@9.0", "state": "tentative", "cluster_ref": "C1"}]},
        None)
    asserted = [t for t in calls["turns"] if t["source"] == "correction"]
    assert len(asserted) == 1 and asserted[0]["turn_ref"] == "f.wav@1.0"
    assert asserted[0]["voiceprint_id"] == "vp1"


def test_every_row_carries_the_correction_that_caused_it(calls):
    """Withdrawal has to enumerate what a correction justified (§6). Without this pointer
    that means grepping S3 artifacts."""
    vw.lambda_handler({
        "op": "propagation", "company_id": CO, "session_base": "s1",
        "correction_ref": "corr-1",
        "results": [{"turn_ref": "a", "state": "tentative", "cluster_ref": "C1"}]}, None)
    assert calls["turns"][0]["correction_ref"] == "corr-1"


# ---- enrolment results --------------------------------------------------


def test_an_embedded_vector_is_stored_with_its_provenance(calls):
    vw.lambda_handler({
        "op": "enrol", "company_id": CO, "voiceprint_id": "vp1",
        "embedding": [0.1] * 192, "s3_key": "users/u/audio/d/x.wav",
        "window": [0.0, 15.0], "correction_ref": "corr-1", "created_by": "u1"}, None)
    assert len(calls["samples"]) == 1
    assert calls["samples"][0]["correction_ref"] == "corr-1"
    assert calls["samples"][0]["s3_key"] == "users/u/audio/d/x.wav"


def test_a_refused_enrolment_stores_nothing(calls):
    """The embedder refuses a window it cannot judge as one voice. That refusal must not
    become a stored sample here — a profile cannot be un-poisoned."""
    out = vw.lambda_handler({
        "op": "enrol", "company_id": CO, "voiceprint_id": "vp1",
        "status": "refused", "reason": "window is not homogeneous"}, None)
    assert calls["samples"] == []
    assert out["stored"] == 0


def test_an_enrolment_without_a_vector_raises_rather_than_storing_a_blank(calls):
    with pytest.raises((KeyError, ValueError)):
        vw.lambda_handler({"op": "enrol", "company_id": CO, "voiceprint_id": "vp1"}, None)


# ---- shared -------------------------------------------------------------


def test_a_missing_company_raises(calls):
    with pytest.raises(ValueError):
        vw.lambda_handler({"op": "propagation", "session_base": "s1", "results": []}, None)


def test_an_unknown_op_is_rejected(calls):
    with pytest.raises(ValueError):
        vw.lambda_handler({"op": "sing"}, None)


def test_the_writer_holds_no_model_and_no_audio():
    """The split is the point: this half has psycopg and no onnxruntime, the other half has
    onnxruntime and no psycopg. A writer that grew an embedder would need both layers, which
    is the cp311/cp312 conflict that made the deployed function non-functional."""
    import ast
    import pathlib
    src = pathlib.Path(vw.__file__).read_text(encoding="utf-8")
    names = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    assert not (names & {"onnxruntime", "wave", "batch_stitch", "batch_seal"}), (
        f"the writer reached for audio or the model: {sorted(names)}")


def test_the_name_reaches_the_row(calls):
    """The second hop that dropped it. `record_turn_name` gained `display_name` in 0041 and
    the writer has to actually pass it — a closing mutation pass showed that deleting this
    one keyword left every test green while every row named nobody."""
    vw.lambda_handler({
        "op": "propagation", "company_id": CO, "session_base": "s1",
        "results": [{"turn_ref": "f.wav@1.0", "state": "confirmed", "cluster_ref": "C1",
                     "display_name": "Ben L", "asserted": True}]}, None)
    assert calls["turns"][0]["display_name"] == "Ben L", (
        "the writer dropped the name; nothing raises, the row is simply anonymous")


def test_an_unnamed_result_stays_unnamed_rather_than_becoming_a_placeholder(calls):
    """An unnamed cluster is a real answer — 'someone consistent, not identified'."""
    vw.lambda_handler({
        "op": "propagation", "company_id": CO, "session_base": "s1",
        "results": [{"turn_ref": "f.wav@1.0", "state": "tentative", "cluster_ref": "C2"}]},
        None)
    assert calls["turns"][0].get("display_name") is None


def test_a_propagation_carrying_an_enrolment_stores_the_sample(calls):
    """One gesture, two effects, one transaction. The turn names land and the profile gains
    a sample — and a failure in either must not leave half of it applied."""
    vw.lambda_handler({
        "op": "propagation", "company_id": CO, "session_base": "s1",
        "correction_ref": "corr-1",
        "results": [{"turn_ref": "f.wav@1.0", "state": "confirmed", "cluster_ref": None,
                     "display_name": "Ben L", "asserted": True}],
        "enrol": {"voiceprint_id": "vp-1", "embedding": [0.1] * 192,
                  "s3_key": "users/u/audio/d/x.wav", "window": [0.0, 5.0]}}, None)
    assert len(calls["samples"]) == 1
    assert calls["samples"][0]["correction_ref"] == "corr-1", (
        "the sample cannot be traced back to the correction that justified it, so a "
        "withdrawal could not enumerate what it produced")


def test_a_propagation_without_an_enrolment_stores_no_sample(calls):
    vw.lambda_handler({
        "op": "propagation", "company_id": CO, "session_base": "s1",
        "results": [{"turn_ref": "f.wav@1.0", "state": "tentative", "cluster_ref": "C1"}]},
        None)
    assert calls["samples"] == []


def test_an_enrolment_with_no_vector_is_refused_rather_than_stored_blank(calls):
    with pytest.raises((KeyError, ValueError)):
        vw.lambda_handler({
            "op": "propagation", "company_id": CO, "session_base": "s1", "results": [],
            "enrol": {"voiceprint_id": "vp-1"}}, None)


def test_the_names_and_the_sample_land_in_one_transaction(monkeypatch):
    """The docstring claims one transaction; nothing asserted it, and a mutation moving the
    sample into a second connection stayed green. Half-applied is the bad state here: turn
    names pointing at a profile that has no sample, or a sample justified by names that
    never landed."""
    conns = []

    class C(FakeConn):
        def __enter__(self):
            conns.append(self)
            return self
    monkeypatch.setattr(vw, "get_connection", lambda: C())
    seen = []
    monkeypatch.setattr(vw, "record_turn_name",
                        lambda conn, company_id, **kw: seen.append(("turn", conn)) or {"id": "t"})
    monkeypatch.setattr(vw, "add_sample",
                        lambda conn, company_id, *a, **kw: seen.append(("sample", conn)) or {"id": "s"})
    vw.lambda_handler({
        "op": "propagation", "company_id": CO, "session_base": "s1",
        "results": [{"turn_ref": "f.wav@1.0", "state": "confirmed", "cluster_ref": None,
                     "display_name": "Ben L", "asserted": True}],
        "enrol": {"voiceprint_id": "vp-1", "embedding": [0.1] * 192,
                  "s3_key": "k", "window": [0.0, 15.0]}}, None)
    assert len(conns) == 1, f"{len(conns)} connections opened; the two halves can differ"
    assert {c for _, c in seen} == {conns[0]}, "the sample used a different connection"


# ---- a refused enrolment is half a gesture, not none of it ----------------


def _refuse(monkeypatch):
    def boom(conn, company_id, *a, **kw):
        raise vw.EnrolmentBelongsToSomebodyElse(0.42, 0.71, "vp-other")
    monkeypatch.setattr(vw, "add_sample", boom)


def _correction_event():
    return {"op": "propagation", "company_id": CO, "session_base": "s1",
            "correction_ref": "corr-1",
            "results": [{"turn_ref": "f.wav@1.0", "state": "confirmed",
                         "cluster_ref": None, "display_name": "Ben L", "asserted": True},
                        {"turn_ref": "f.wav@9.0", "state": "tentative",
                         "cluster_ref": "c1", "display_name": "Ben L"}],
            "enrol": {"voiceprint_id": "vp-1", "embedding": [0.1] * 192,
                      "s3_key": "users/u/audio/d/x.wav", "window": [0.0, 5.0]}}


def test_a_refused_enrolment_does_not_take_the_names_with_it(calls, monkeypatch):
    """The names describe THIS meeting and were earned by the user's own assertion; only
    the half that stores biometric data needs consent, and only that half is refused.

    Letting the exception escape would roll back the transaction the names are written in,
    so a user who corrected a speaker would see nothing happen at all — and the API already
    reports the two effects separately precisely because they can differ."""
    _refuse(monkeypatch)
    out = vw.lambda_handler(_correction_event(), None)
    assert len(calls["turns"]) == 2, "the names were rolled back with the enrolment"
    assert out["written"] == 2
    assert out["enrolled"] is False
    assert out["enrolRefused"]["reason"] == "closer-to-another-profile"


def test_a_refused_enrolment_says_which_profile_it_looked_like(calls, monkeypatch):
    """Refusing without saying what it resembled leaves a person with nothing to act on —
    and the whole point of comparing an order rather than a threshold is that the runner-up
    is the evidence."""
    _refuse(monkeypatch)
    out = vw.lambda_handler(_correction_event(), None)
    assert out["enrolRefused"]["nearestOtherId"] == "vp-other"
    assert out["enrolRefused"]["bestOther"] > out["enrolRefused"]["own"]


def test_a_standalone_enrolment_refusal_stores_nothing_and_says_so(calls, monkeypatch):
    _refuse(monkeypatch)
    out = vw.lambda_handler({"op": "enrol", "company_id": CO, "voiceprint_id": "vp-1",
                             "embedding": [0.1] * 192, "s3_key": "k",
                             "window": [0.0, 4.0]}, None)
    assert out["stored"] == 0 and out["reason"] == "closer-to-another-profile"


# ---- Phase 5: names with no human behind them ----------------------------


def _match_event():
    return {"op": "match_names", "company_id": CO, "session_base": "s1",
            "results": [{"turn_ref": "f.wav@1.0", "status": "confirmed",
                         "person_key": "vp-1", "display_name": "Ben L",
                         "score": 0.9, "margin": 0.3},
                        {"turn_ref": "f.wav@9.0", "status": "tentative",
                         "person_key": "vp-1", "display_name": "Ben L"}]}


def test_a_matched_name_is_never_written_as_a_human_correction(calls):
    """`confirmations_count` counts rows whose source is 'correction' and promotes a profile
    after enough of them. Writing machine output under that source would let the system
    promote its own profiles from its own guesses — and the promotion would then justify
    more confident guesses."""
    vw.lambda_handler(_match_event(), None)
    assert calls["turns"], "nothing was written"
    assert {t["source"] for t in calls["turns"]} == {"voiceprint_match"}


def test_a_matched_name_carries_the_profile_so_a_withdrawal_can_find_it(calls):
    """§6 requires a withdrawal to reach everything the profile justified, and `withdraw`
    supersedes turn names by exactly this column. A matched name with no id is a name the
    withdrawal cannot reach — unlike a propagated row, which has no profile to point at."""
    vw.lambda_handler(_match_event(), None)
    assert all(t["voiceprint_id"] == "vp-1" for t in calls["turns"])


def test_a_refusal_from_the_matcher_writes_no_row(calls):
    vw.lambda_handler({"op": "match_names", "company_id": CO, "session_base": "s1",
                       "results": [{"turn_ref": "f.wav@1.0", "status": "unknown",
                                    "person_key": None}]}, None)
    assert calls["turns"] == []


def test_fetching_profiles_is_company_scoped(monkeypatch):
    seen = {}
    monkeypatch.setattr(vw, "get_connection", lambda: FakeConn())
    monkeypatch.setattr(vw, "profiles_for_matching",
                        lambda conn, company_id, site_id=None:
                        seen.update({"co": company_id}) or [])
    vw.lambda_handler({"op": "profiles", "company_id": CO}, None)
    assert seen["co"] == CO, "one company's voice would be matched against another's"
