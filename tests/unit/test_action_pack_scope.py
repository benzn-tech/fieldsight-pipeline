"""The action agent's scope gate (spec 2026-11-05 §3.2.4; plan PR-4 Tasks 4.2-4.4): which
items a draft may change, and where every proposed field must come from. Pure: no models,
no tools, no I/O."""
import copy
from types import SimpleNamespace

import action_pack_scope as scope
import action_pack_stages as st
from action_pack_targets import AMBIGUOUS, NONE, RESOLVED, Resolution

SITE = "22222222-3333-4444-8555-666666666666"
SID_A = "44444444-5555-4666-8777-00000000000a"
SID_B = "44444444-5555-4666-8777-00000000000b"
SID_C = "44444444-5555-4666-8777-00000000000c"
SID_D = "44444444-5555-4666-8777-00000000000d"
SID_DONE = "44444444-5555-4666-8777-00000000000e"
TOPIC_DAY = "2026-10-28"


def make_items():
    """R4..R8 are items; R1 a topic said on TOPIC_DAY, R2 a document, R3 a web answer."""
    out = {}
    for ref, sid, no, status in (("R4", SID_A, 4, "open"), ("R5", SID_B, 5, "open"),
                                 ("R6", SID_C, 6, "in_progress"), ("R7", SID_D, 7, "blocked"),
                                 ("R8", SID_DONE, 8, "done")):
        out[sid] = {"ref": ref, "kind": "item", "stable_id": sid, "task_no": no,
                    "text": f"Item {no}", "status": status, "deadline": None,
                    "deadline_text": None, "raised_on": "2026-10-20", "site_id": SITE,
                    "priority": None, "responsible": None}
    return out


def make_registry(items):
    reg = {"R1": {"ref": "R1", "kind": "topic", "item": {"date": TOPIC_DAY}},
           "R2": {"ref": "R2", "kind": "document", "item": {}},
           "R3": {"ref": "R3", "kind": "web_answer", "item": {}}}
    for it in items.values():
        reg[it["ref"]] = {"ref": it["ref"], "kind": "item", "item": it}
    return reg


def make_plan(scope_="named", language="en", stated=(), delegations=(), targets=("t1",)):
    return SimpleNamespace(change_scope=scope_, language=language,
                           targets=[{"id": t} for t in targets],
                           stated_values=list(stated), delegations=list(delegations))


def ev(*refs, said_on=None):
    return {"source": "evidence", "refs": list(refs), "said_on": said_on}


def upd(sid, proposed, *, ref=None, sources=None, refs=("R1",), atype="update_item",
        rationale="Records say so."):
    if sources is None:
        sources = {f: ev("R1") for f in proposed}
    return {"type": atype, "target_stable_id": sid, "target_ref": ref,
            "proposed": dict(proposed), "field_sources": sources,
            "rationale": rationale, "refs": list(refs)}


def gate(actions, *, plan=None, resolution=None, items=None):
    items = items or make_items()
    draft = {"conclusion": [], "actions": copy.deepcopy(actions)}
    return scope.gate(draft, plan=plan or make_plan(),
                      resolution=resolution if resolution is not None
                      else {"t1": Resolution(RESOLVED, SID_A)},
                      items=items, registry=make_registry(items))


# ---- Task 4.2: rules 1-2 ------------------------------------------------------------------

def test_m1_shape_unasked_updates_dropped():
    actions = [upd(SID_A, {"status": "in_progress"}, ref="t1"),
               upd(SID_B, {"priority": "high"}),
               upd(SID_C, {"status": "blocked"}),
               upd(SID_D, {"status": "in_progress", "priority": "low"})]
    out, counts = gate(actions)
    assert [a["target_stable_id"] for a in out["actions"]] == [SID_A]
    assert counts == {"drop_unasked_target": 3}


def test_c4_lookalike_dropped():
    out, counts = gate([upd(SID_B, {"status": "in_progress"}, ref="t1")])
    assert out["actions"] == []
    assert counts == {"drop_named_mismatch": 1}


def test_ambiguous_target_demoted_to_note():
    res = {"t1": Resolution(AMBIGUOUS, None, [SID_A, SID_B])}
    a = upd(SID_A, {"status": "in_progress"}, ref="t1", refs=("R1", "R4"))
    # The same target without its target_ref is still the ambiguous target's candidate.
    b = upd(SID_B, {"status": "in_progress"}, refs=("R5",))
    out, counts = gate([a, b], resolution=res)
    assert counts == {"demoted_ambiguous": 2}
    note = out["actions"][0]
    assert note == {"type": "note", "target_stable_id": None,
                    "proposed": {"text": "Records say so. (matches more than one item: "
                                         "R4, R5)"},
                    "rationale": "Records say so.", "refs": ["R1", "R4"]}


def test_target_none_demoted_to_note():
    res = {"t1": Resolution(NONE)}
    out, counts = gate([upd(SID_B, {"status": "in_progress"}, ref="t1")], resolution=res)
    assert counts == {"demoted_target_none": 1}
    [note] = out["actions"]
    assert note["type"] == "note" and note["target_stable_id"] is None
    assert note["proposed"]["text"] == "Records say so. (named item not found)"
    assert note["refs"] == ["R1"]


def test_closed_named_target_by_task_no_becomes_already_done_note():
    # Review Focus 5: "#8" resolved by task number to an item that is already done. The
    # close becomes an "already done" note -- never a redirect to an open look-alike.
    res = {"t1": Resolution(RESOLVED, SID_DONE)}
    close = upd(SID_DONE, {"status": "closed"}, ref="t1", atype="close_item")
    lookalike = upd(SID_A, {"status": "done"}, ref="t1", atype="close_item")
    out, counts = gate([close, lookalike], resolution=res)
    assert counts == {"demoted_target_closed": 1, "drop_named_mismatch": 1}
    [note] = out["actions"]
    assert note["type"] == "note"
    assert note["proposed"]["text"] == "Records say so. (already done)"


def test_tidy_allows_unnamed_open_item():
    out, counts = gate([upd(SID_B, {"status": "in_progress"}),
                        upd(SID_C, {"status": "done"}, atype="close_item")],
                       plan=make_plan("tidy", targets=()), resolution={})
    assert [a["target_stable_id"] for a in out["actions"]] == [SID_B, SID_C]
    assert counts == {}


def test_tidy_rejects_closed_item():
    out, counts = gate([upd(SID_DONE, {"priority": "high"})],
                       plan=make_plan("tidy", targets=()), resolution={})
    assert out["actions"] == []
    assert counts == {"drop_target_closed": 1}


def test_demoted_note_suffix_zh():
    res = {"t1": Resolution(AMBIGUOUS, None, [SID_A, SID_B]),
           "t2": Resolution(NONE), "t3": Resolution(RESOLVED, SID_DONE)}
    out, _ = gate([upd(SID_A, {"status": "in_progress"}, ref="t1", rationale="记录如此。"),
                   upd(SID_C, {"status": "blocked"}, ref="t2", rationale="记录如此。"),
                   upd(SID_DONE, {"priority": "high"}, ref="t3", rationale="记录如此。")],
                  plan=make_plan(language="zh-cn", targets=("t1", "t2", "t3")),
                  resolution=res)
    texts = [a["proposed"]["text"] for a in out["actions"]]
    assert texts == ["记录如此。（" + scope.NOTE_SUFFIX["zh"]["ambiguous"].format(
                         refs="R4, R5") + "）",
                     "记录如此。（" + scope.NOTE_SUFFIX["zh"]["none"] + "）",
                     "记录如此。（" + scope.NOTE_SUFFIX["zh"]["closed"] + "）"]
    assert scope.NOTE_SUFFIX["en"] == {"ambiguous": "matches more than one item: {refs}",
                                       "none": "named item not found",
                                       "closed": "already done"}


def test_demoted_note_text_is_capped():
    res = {"t1": Resolution(NONE)}
    out, _ = gate([upd(SID_A, {"status": "blocked"}, ref="t1", rationale="x" * 5000)],
                  resolution=res)
    assert len(out["actions"][0]["proposed"]["text"]) == st.BODY_MAX_CHARS


def test_conclusion_and_untargeted_actions_pass_untouched():
    rfi = {"type": "draft_rfi", "target_stable_id": None,
           "proposed": {"subject": "s", "body": "b"}, "rationale": "r", "refs": ["R1"]}
    draft = {"conclusion": [{"text": "c", "refs": ["R1"], "confidence": "supported"}],
             "actions": [rfi]}
    items = make_items()
    out, counts = scope.gate(copy.deepcopy(draft), plan=make_plan(), resolution={},
                             items=items, registry=make_registry(items))
    assert out == draft and counts == {}
