"""Unit: claim_type.derive -- rule v1 (spec 2026-11-08 §5.4). Synthetic values only."""
import json
import uuid

import claim_type

REC = uuid.uuid4()
OTHER = uuid.uuid4()


def _linked(**kw):
    s = {"status": "linked", "source_stem": "stem-a", "start_sec": 1.0, "end_sec": 4.0,
         "speaker_label": "spk_0", "quote_status": None, "score": 0.9,
         "candidate_path": "window", "rule_version": "r-test", "link_id": uuid.uuid4()}
    s.update(kw)
    return s


def _named(user_id, state="confirmed"):
    return {"user_id": user_id, "state": state, "display_name": "Synthetic Person",
            "source": "correction", "cluster_ref": "C1", "voiceprint_id": uuid.uuid4()}


def test_none_close_is_inferred():
    ct, basis = claim_type.derive({"status": "none_close", "best": 0.1}, None, REC)
    assert ct == "inferred"
    assert basis == {"rule_version": "claim_v1", "support_status": "none_close"}


def test_recorder_named_is_observed():
    s = _linked()
    ct, basis = claim_type.derive(s, _named(REC), REC)
    assert ct == "observed"
    assert basis["link_id"] == str(s["link_id"])
    assert basis["named_user_id"] == str(REC) and basis["name_state"] == "confirmed"


def test_other_named_is_stated():
    ct, basis = claim_type.derive(_linked(), _named(OTHER), REC)
    assert ct == "stated"
    assert basis["named_user_id"] == str(OTHER)


def test_unnamed_is_null():
    assert claim_type.derive(_linked(), None, REC)[0] is None
    # a name whose voiceprint has no user is not an identity
    assert claim_type.derive(_linked(), _named(None), REC)[0] is None
    # an unknown recorder cannot be told apart from anyone else
    assert claim_type.derive(_linked(), _named(OTHER), None)[0] is None
    ct, basis = claim_type.derive(None, None, REC)
    assert ct is None and basis["support_status"] is None


def test_ambiguous_is_null():
    for s in ({"status": "ambiguous", "best": 0.8, "second": 0.79},
              {"status": "below_floor", "best": 0.2, "guard": "distinguishing_token"}):
        ct, basis = claim_type.derive(s, _named(REC), REC)
        assert ct is None
        assert basis["support_status"] == s["status"]


def test_tentative_name_is_null():
    ct, basis = claim_type.derive(_linked(), _named(REC, state="tentative"), REC)
    assert ct is None
    assert basis["name_state"] == "tentative"


def test_basis_has_no_text():
    named = _named(OTHER)
    _, basis = claim_type.derive(_linked(), named, REC)
    blob = json.dumps(basis)
    assert named["display_name"] not in blob
    assert "stem-a" not in blob and "spk_0" not in blob
    assert set(basis) <= {"rule_version", "support_status", "link_id", "named_user_id",
                          "name_state"}
