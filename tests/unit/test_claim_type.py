"""Unit: claim_type.derive -- rule v2 (spec 2026-11-08 §5.4; locator spec 2026-11-10 §4.4).
Synthetic values only."""
import json
import uuid

import pytest

import claim_type
import item_locator

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
    assert basis == {"rule_version": "claim_v2", "support_status": "none_close"}


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


# --- rule v2: locator statuses (locator spec §4.4) ---------------------------------------

def _locator_linked(**kw):
    s = {"status": "linked", "source_stem": "stem-a", "start_sec": 1.0, "end_sec": 4.0,
         "speaker_label": "spk_0", "quote_status": None, "method": item_locator.RULE_VERSION,
         "rule_version": item_locator.RULE_VERSION, "score": None, "range_checked": True,
         "link_id": uuid.uuid4()}
    s.update(kw)
    return s


def _refusal(status):
    return {"status": status, "method": item_locator.RULE_VERSION,
            "rule_version": item_locator.RULE_VERSION}


def test_rule_version_is_v2():
    assert claim_type.RULE_VERSION == "claim_v2"
    assert claim_type.INFERRED_FROM_NO_LOCATOR is False


@pytest.mark.parametrize("flag", [False, True])
def test_locator_linked_is_as_v1(monkeypatch, flag):
    monkeypatch.setattr(claim_type, "INFERRED_FROM_NO_LOCATOR", flag)
    s = _locator_linked()
    ct, basis = claim_type.derive(s, _named(REC), REC)
    assert ct == "observed"
    assert basis == {"rule_version": "claim_v2", "support_status": "linked",
                     "link_id": str(s["link_id"]), "name_state": "confirmed",
                     "named_user_id": str(REC)}
    assert claim_type.derive(_locator_linked(), _named(OTHER), REC)[0] == "stated"
    assert claim_type.derive(_locator_linked(), None, REC)[0] is None
    assert claim_type.derive(_locator_linked(), _named(REC, "tentative"), REC)[0] is None


@pytest.mark.parametrize("flag,expected", [(False, None), (True, "inferred")])
def test_no_locator_follows_the_constant(monkeypatch, flag, expected):
    monkeypatch.setattr(claim_type, "INFERRED_FROM_NO_LOCATOR", flag)
    ct, basis = claim_type.derive(_refusal("no_locator"), _named(REC), REC)
    assert ct == expected
    assert basis == {"rule_version": "claim_v2", "support_status": "no_locator"}


@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize("status", [s for s in item_locator.STATUSES
                                    if s not in ("linked", "no_locator")])
def test_every_refusal_is_null(monkeypatch, flag, status):
    monkeypatch.setattr(claim_type, "INFERRED_FROM_NO_LOCATOR", flag)
    ct, basis = claim_type.derive(_refusal(status), _named(REC), REC)
    assert ct is None
    assert basis == {"rule_version": "claim_v2", "support_status": status}


def test_lexical_none_close_unaffected_by_the_constant(monkeypatch):
    monkeypatch.setattr(claim_type, "INFERRED_FROM_NO_LOCATOR", False)
    assert claim_type.derive({"status": "none_close"}, None, REC)[0] == "inferred"


def test_stored_locator_link_rebuilt_as_recompute_does_derives_linked():
    """recompute_session rebuilds a support from the stored link as
    {"status": "linked", **dst_turn, "link_id"} -- no method, no score. A locator link's
    dst_turn (DST_TURN_KEYS only) must still derive as linked."""
    store = pytest.importorskip("repositories.claim_type_store")
    loc = _locator_linked()
    dst_turn = {k: loc.get(k) for k in store.DST_TURN_KEYS}
    link_id = uuid.uuid4()
    rebuilt = {"status": "linked", **dst_turn, "link_id": link_id}
    ct, basis = claim_type.derive(rebuilt, _named(REC), REC)
    assert ct == "observed"
    assert basis["support_status"] == "linked" and basis["link_id"] == str(link_id)


def test_support_statuses_cover_every_locator_status():
    store = pytest.importorskip("repositories.claim_type_store")
    for status in item_locator.STATUSES:
        assert status in store.SUPPORT_STATUSES, status
    for status in ("linked", "none_close", "ambiguous", "below_floor"):
        assert status in store.SUPPORT_STATUSES, status
    assert len(set(store.SUPPORT_STATUSES)) == len(store.SUPPORT_STATUSES)


def test_support_metric_covers_every_locator_status():
    iw = pytest.importorskip("lambda_item_writer", reason="requires psycopg (installed in CI)")
    for status in item_locator.STATUSES:
        name = iw._SUPPORT_METRIC.get(status)
        assert name and name.startswith("Support"), status
    assert len(set(iw._SUPPORT_METRIC.values())) == len(iw._SUPPORT_METRIC)
    assert iw._SUPPORT_METRIC["no_locator"] == "SupportNoLocator"
    assert iw._SUPPORT_METRIC["ambiguous_stamp"] == "SupportAmbiguousStamp"


def test_write_pass_passes_the_support_method(monkeypatch):
    store = pytest.importorskip("repositories.claim_type_store")
    calls = []

    def fake_insert(conn, **kw):
        calls.append(kw)
        return uuid.uuid4()

    monkeypatch.setattr(store.event_links, "insert_supported_by", fake_insert)
    monkeypatch.setattr(store, "_named_turns", lambda *a, **k: {})
    monkeypatch.setattr(store, "write", lambda *a, **k: None)
    lexical = {"status": "linked", "source_stem": "stem-a", "start_sec": 2.0, "end_sec": 3.0,
               "speaker_label": "spk_0", "quote_status": None, "score": 0.9,
               "rule_version": "lexical-test"}
    counts = store.write_pass(
        None, company_id="c", site_id="s", session_base="b", source_s3_key="k",
        recorder_user_id=REC,
        items=[("action_item", 1, _locator_linked()), ("finding", 2, lexical),
               ("decision", 3, _refusal("no_line"))])
    assert [c["method"] for c in calls] == ["locator_v2", "lexical_v1"]
    assert calls[0]["score"] is None and calls[0]["rule_version"] == "locator_v2"
    assert counts["support"]["linked"] == 2 and counts["support"]["no_line"] == 1
    assert counts["links"] == 2
