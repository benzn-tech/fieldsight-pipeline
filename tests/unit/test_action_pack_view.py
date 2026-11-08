"""Unit: action_pack_view -- the pure half of org-api's action pack routes (Task 6)."""
import json

import pytest

import action_pack_view as v


@pytest.mark.parametrize("raw,mode", [(None, "off"), ("", "off"), ("off", "off"),
                                      ("internal", "internal"), (" ON ", "on"),
                                      ("true", "off"), ("1", "off")])
def test_mode_unknown_is_off(monkeypatch, raw, mode):
    if raw is None:
        monkeypatch.delenv("ACTION_PACKS", raising=False)
    else:
        monkeypatch.setenv("ACTION_PACKS", raw)
    assert v.mode() == mode
    assert v.enabled() is (mode != "off")


def test_cites_only_removed():
    assert v.cites_only_removed(["R1", "R2"], {"R1", "R2"}) is True
    assert v.cites_only_removed(["R1", "W1"], {"R1"}) is False
    assert v.cites_only_removed([], {"R1"}) is False          # cites nothing removed


def test_request_object_never_carries_the_instruction():
    obj = v.request_object({"id": "p", "site_id": "s", "instruction": "SECRET"}, "sub")
    assert obj == {"pack_id": "p", "caller_sub": "sub", "site_id": "s"}
    assert "SECRET" not in json.dumps(obj)


def test_store_payload_cap_boundary():
    pad = v.STORE_PAYLOAD_MAX_BYTES - v.payload_size({"reading": ""})
    assert v.store_payload_error({"reading": "x" * pad}) is None
    assert v.store_payload_error({"reading": "x" * (pad + 1)}) == {"error": "payload_too_large"}
    assert v.STORE_PAYLOAD_MAX_BYTES < 6 * 1024 * 1024


@pytest.mark.parametrize("msg,code", [("unknown pack keys: ['x']", "unknown_keys"),
                                      ("action seq 1 cites refs with no source: ['R9']",
                                       "dangling_ref"),
                                      ("other", "invalid_payload")])
def test_invalid_detail_codes(msg, code):
    assert v.invalid_detail(msg) == code


def _pack(**over):
    p = {"id": "p", "site_id": "s", "created_by": "u", "title": "Doors", "state": "ready",
         "archived_at": None, "reading": "Close doors", "instruction": "i",
         "conclusion": [{"text": "gone", "refs": ["R2"]}, {"text": "kept", "refs": ["R1"]}],
         "sources": [{"ref": "R1", "kind": "topic", "provenance": "records",
                      "title": "T1", "excerpt": "seen"},
                     {"ref": "R2", "kind": "topic", "provenance": "records",
                      "title": "HIDDEN", "excerpt": "HIDDEN"}],
         "actions": [{"id": "a1", "seq": 1, "type": "close_item", "refs": ["R1"],
                      "verifier": "weak", "state": "proposed", "target_stable_id": "st",
                      "target_snapshot": {"text": "t"}, "proposed": {"status": "done"}},
                     {"id": "a2", "seq": 2, "type": "note", "refs": ["R2"],
                      "verifier": "supported", "state": "proposed"}]}
    p.update(over)
    return p


def test_shape_pack_applies_removal_and_flags():
    view = v.shape_pack(_pack(), removed_refs={"R2"}, site_name="Site",
                        targets={"a1": {"task_no": 69, "visible": True}},
                        applicable={"a1": True}, viewer_id="u")
    assert [c["text"] for c in view["conclusion"]] == ["kept"]
    assert [a["id"] for a in view["actions"]] == ["a1"]
    a1 = view["actions"][0]
    assert a1["applicable"] is True and a1["target_task_no"] == 69
    assert a1["verifier"] == "weak" and a1["target_snapshot"] == {"text": "t"}
    assert view["sources"][1] == {"ref": "R2", "kind": "topic", "provenance": "records",
                                  "removed": True}
    assert "HIDDEN" not in json.dumps(view) and view["is_creator"] is True
    md = v.render_markdown(view)
    assert "HIDDEN" not in md and "[R2] source removed" in md and "#69" in md
    assert "_(uncertain)_" in md


def test_target_snapshot_withheld_when_target_not_visible():
    view = v.shape_pack(_pack(), removed_refs=set(), site_name=None,
                        targets={"a1": {"task_no": 69, "visible": False}},
                        applicable={}, viewer_id="x")
    assert view["actions"][0]["target_snapshot"] is None
    assert view["actions"][0]["target_task_no"] is None       # names an item they cannot see
    assert "#69" not in v.render_markdown(view)
    assert view["actions"][0]["applicable"] is False and view["is_creator"] is False


@pytest.mark.parametrize("over,expected", [
    ({}, True),
    ({"verifier": "unsupported"}, False),
    ({"type": "draft_email"}, False),
    ({"state": "applied"}, False),
    ({"state": "failed"}, True),
])
def test_applicable_candidate(over, expected):
    a = dict({"type": "close_item", "verifier": "weak", "state": "proposed"}, **over)
    assert v.applicable_candidate(a, {"state": "ready", "archived_at": None}) is expected
    assert v.applicable_candidate(a, {"state": "ready", "archived_at": "x"}) is False
    assert v.applicable_candidate(a, {"state": "running", "archived_at": None}) is False


# ---------------------------------------------------------------- Task 13 fix round 2

def test_shape_pack_carries_the_run_trace_counts_and_model_ids():
    """Spec §1 lists the run trace as part of the pack; the eval harness reads latency,
    tokens and the verifier's counts from GET. Counts and model ids only."""
    trace = {"tokens": 1234, "truncated": False, "truncated_reasons": ["time"],
             "ms": {"total": 61000, "write": 2000}, "verify": {"drop_refs": 1},
             "reading": "The pour at C4 was late",            # free text: dropped
             "ms_bad": {"nested": {"too": "deep"}}}           # not counts-shaped: dropped
    models = {"planner": "deepseek/deepseek-v4.1-flash", "writer": "gemini-2.5-flash",
              "writer_served": "google/gemini-2.5-flash-001", "verifier": None,
              "note": "free text here", "planner_served": "has spaces in it"}
    view = v.shape_pack(_pack(run_trace=trace, models=models), removed_refs=set(),
                        site_name=None, targets={}, applicable={}, viewer_id="u")
    assert view["run_trace"] == {"tokens": 1234, "truncated": False,
                                 "truncated_reasons": ["time"],
                                 "ms": {"total": 61000, "write": 2000},
                                 "verify": {"drop_refs": 1}}
    assert view["models"] == {"planner": "deepseek/deepseek-v4.1-flash",
                              "writer": "gemini-2.5-flash",
                              "writer_served": "google/gemini-2.5-flash-001",
                              "verifier": None}
    assert "C4" not in json.dumps(view) and "free text" not in json.dumps(view)


def test_shape_pack_without_trace_or_models_returns_nulls():
    view = v.shape_pack(_pack(), removed_refs=set(), site_name=None, targets={},
                        applicable={}, viewer_id="u")
    assert view["run_trace"] is None and view["models"] is None
    view = v.shape_pack(_pack(run_trace="not a dict", models=["x"]), removed_refs=set(),
                        site_name=None, targets={}, applicable={}, viewer_id="u")
    assert view["run_trace"] is None and view["models"] is None


def test_an_oversized_public_trace_is_replaced_by_a_marker():
    """Each kept key is counts-shaped, but many of them can still add up: over
    FAIL_TRACE_MAX_BYTES (the cap a failed pack's trace has) GET returns {"dropped": "size"}
    instead of the trace -- a size marker the eval harness can see, never a silent cut."""
    big = {f"k{i:03d}": list(range(16)) for i in range(200)}
    assert all(v.fail_trace_error({k: x}) is None for k, x in big.items())
    assert len(json.dumps(big)) > v.FAIL_TRACE_MAX_BYTES
    assert v.public_trace(big) == {"dropped": "size"}
    small = {"tokens": 1, "ms": {"total": 2}}
    assert v.public_trace(small) == small


# ---------------------------------------------------------------- PR-5 (spec 2026-11-05 §9.2)

def _store_payload(**over):
    p = {"title": "t", "conclusion": [],
         "sources": [{"ref": "R1", "kind": "topic", "provenance": "records"}],
         "actions": [{"seq": 1, "type": "note", "refs": ["R1"], "verifier": "weak",
                      "proposed": {"text": "n"}}]}
    p.update(over)
    return p


def test_payload_shape_accepts_sub_tasks_and_verifier_detail():
    sub = [{"id": "s1", "ask": "Close #1", "kind": "close", "status": "done",
            "actions": [1], "conclusion": [], "reason_code": None, "reason": None}]
    p = _store_payload(sub_tasks=sub)
    p["actions"][0]["verifier_detail"] = {"reason": "date_assumed"}
    assert v.payload_shape_error(p) is None
    assert v.payload_shape_error(_store_payload(sub_tasks=None)) is None
    assert v.payload_shape_error(_store_payload(sub_tasks={"s1": 1})) == "pack_field_type"
    bad = _store_payload()
    bad["actions"][0]["verifier_detail"] = ["weak"]
    assert v.payload_shape_error(bad) == "action_field_type"


_SUBS = [{"id": "s1", "ask": "Close #69", "kind": "close", "status": "done", "actions": [1],
          "conclusion": [0], "reason_code": None, "reason": None},
         {"id": "s2", "ask": "Email the client", "kind": "draft", "status": "not_done",
          "actions": [], "conclusion": [], "reason_code": "not_answered", "reason": None}]


def test_shape_pack_returns_sub_tasks():
    view = v.shape_pack(_pack(sub_tasks=_SUBS), removed_refs=set(), site_name=None,
                        targets={}, applicable={}, viewer_id="u")
    assert view["sub_tasks"] == _SUBS
    # An old pack (stored before migration 0097, or a plan without sub-tasks): None.
    for pack in (_pack(), _pack(sub_tasks=None)):
        view = v.shape_pack(pack, removed_refs=set(), site_name=None, targets={},
                            applicable={}, viewer_id="u")
        assert "sub_tasks" in view and view["sub_tasks"] is None


def test_sub_task_positions_follow_the_reader_visible_conclusion():
    # Stored conclusion [c0 cites R2, c1 cites R1]; a sub-task points at both, and at both
    # actions (a2 cites only R2). The reader cannot see R2: c0 and a2 are hidden, c1 is
    # now position 0.
    subs = [{"id": "s1", "ask": "x", "kind": "close", "status": "done", "actions": [1, 2],
             "conclusion": [0, 1], "reason_code": None, "reason": None}]
    view = v.shape_pack(_pack(sub_tasks=subs), removed_refs={"R2"}, site_name=None,
                        targets={}, applicable={}, viewer_id="u")
    assert [c["text"] for c in view["conclusion"]] == ["kept"]
    assert view["sub_tasks"][0]["conclusion"] == [0]
    assert view["sub_tasks"][0]["actions"] == [1]
    assert subs[0]["conclusion"] == [0, 1]          # the stored pack is not mutated



# ---- verifier_detail on GET (spec 2026-11-05 §7.2, §9.2; Task 7.3) -----------------------

_DETAIL = {"model": "claude-opus-5-5", "served": "claude-opus-5-5-20261001", "attempt": 2,
           "reason_code": "partly_stated", "reason": "Only the wall is named."}


def test_get_omits_served():
    pack = _pack(sub_tasks=[{"id": "s1", "ask": "x", "kind": "close", "status": "done",
                             "actions": [1], "conclusion": [1], "reason_code": None,
                             "reason": None}])
    pack["actions"][0]["verifier_detail"] = dict(_DETAIL)
    pack["conclusion"][1]["verifier_detail"] = dict(_DETAIL)
    view = v.shape_pack(pack, removed_refs=set(), site_name=None, targets={},
                        applicable={}, viewer_id="u")
    public = {k: x for k, x in _DETAIL.items() if k != "served"}
    assert view["actions"][0]["verifier_detail"] == public
    assert view["actions"][1]["verifier_detail"] is None        # an old pack's action
    assert view["conclusion"][1]["verifier_detail"] == public
    assert "verifier_detail" not in view["conclusion"][0]
    assert "claude-opus-5-5-20261001" not in json.dumps(view, default=str)
    # the stored pack is untouched and the sub-task still points at its line
    assert pack["actions"][0]["verifier_detail"]["served"] == _DETAIL["served"]
    assert pack["conclusion"][1]["verifier_detail"]["served"] == _DETAIL["served"]
    assert view["sub_tasks"][0]["conclusion"] == [1]


def test_payload_shape_checks_a_conclusion_lines_verifier_detail():
    p = _store_payload(conclusion=[{"text": "c", "refs": ["R1"], "confidence": "uncertain",
                                    "verifier_detail": dict(_DETAIL)}])
    assert v.payload_shape_error(p) is None
    p["conclusion"][0]["verifier_detail"] = "weak"
    assert v.payload_shape_error(p) == "conclusion_shape"
