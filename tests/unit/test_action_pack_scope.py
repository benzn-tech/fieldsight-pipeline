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
           "R9": {"ref": "R9", "kind": "chunk", "item": {"date": TOPIC_DAY}},
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


def gate(actions, *, plan=None, resolution=None, items=None, index_map=False):
    items = items or make_items()
    draft = {"conclusion": [], "actions": copy.deepcopy(actions)}
    out = scope.gate(draft, plan=plan or make_plan(),
                     resolution=resolution if resolution is not None
                     else {"t1": Resolution(RESOLVED, SID_A)},
                     items=items, registry=make_registry(items))
    return out if index_map else out[:2]


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


def test_tidy_keeps_an_unnamed_ambiguous_candidate():
    # Under tidy any open item may be changed: an action with no target_ref on one of an
    # ambiguous target's candidates is a tidy change, not a demotion. With the target_ref
    # it still becomes a note; under named, even without one.
    res = {"t1": Resolution(AMBIGUOUS, None, [SID_B, SID_C])}
    actions = [upd(SID_B, {"status": "in_progress"}), upd(SID_C, {"status": "blocked"})]
    out, counts = gate(actions, plan=make_plan("tidy"), resolution=res)
    assert [a["target_stable_id"] for a in out["actions"]] == [SID_B, SID_C]
    assert counts == {}
    actions[0]["target_ref"] = "t1"
    out, counts = gate(actions, plan=make_plan("tidy"), resolution=res)
    assert [a["type"] for a in out["actions"]] == ["note", "update_item"]
    assert counts == {"demoted_ambiguous": 1}
    actions[0]["target_ref"] = None
    out, counts = gate(actions, plan=make_plan("named"), resolution=res)
    assert counts == {"demoted_ambiguous": 2}


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
    out, counts, index_map = scope.gate(copy.deepcopy(draft), plan=make_plan(),
                                        resolution={}, items=items,
                                        registry=make_registry(items))
    assert out == draft and counts == {} and index_map == {0: 0}



# ---- Task 4.3: rule 3, field provenance ---------------------------------------------------

def user():
    return {"source": "user", "refs": [], "said_on": None}


def assumed():
    return {"source": "assumed_default", "refs": [], "said_on": None}


def test_priority_without_source_removed():
    a = upd(SID_A, {"status": "in_progress", "priority": "high"}, ref="t1",
            sources={"status": ev("R1"), "priority": {"source": None, "refs": [],
                                                      "said_on": None}})
    b = upd(SID_A, {"priority": "high"}, ref="t1")
    b["field_sources"] = None                          # the writer gave no field_sources
    out, counts = gate([a])
    assert out["actions"][0]["proposed"] == {"status": "in_progress"}
    assert counts == {"field_unsourced__priority": 1}
    out, counts = gate([b])
    assert out["actions"] == []
    assert counts == {"field_unsourced__priority": 1, "drop_no_sourced_fields": 1}


def test_item_own_ref_not_evidence():
    # The target's own row (R4) and another item's row (R5) never justify a change.
    a = upd(SID_A, {"priority": "high", "responsible": "Sam"}, ref="t1",
            sources={"priority": ev("R4"), "responsible": ev("R5")}, refs=("R4", "R5"))
    out, counts = gate([a])
    assert out["actions"] == []
    assert counts == {"field_unsourced__priority": 1, "field_unsourced__responsible": 1,
                      "drop_no_sourced_fields": 1}


def test_deadline_text_own_ref_is_evidence():
    items = make_items()
    items[SID_A]["deadline_text"] = "end of next week"
    a = upd(SID_A, {"deadline": "2026-10-31"}, ref="t1",
            sources={"deadline": ev("R4", said_on="2026-10-20")}, refs=("R4",))
    out, counts = gate([a], items=items)
    assert out["actions"][0]["proposed"] == {"deadline": "2026-10-31"}
    assert counts == {}
    # Without a deadline_text the item's own row is not evidence for its deadline.
    out, counts = gate([a])
    assert out["actions"] == []
    assert counts["field_unsourced__deadline"] == 1


def test_user_value_overrides_writer():
    stated = [{"target": "t1", "field": "deadline", "value": "2026-11-14"},
              {"target": None, "field": "responsible", "value": "Sam Lee"},
              {"target": "t2", "field": "priority", "value": "low"}]
    a = upd(SID_A, {"deadline": "2026-11-13", "responsible": "sam lee", "priority": "high"},
            ref="t1", sources={"deadline": user(), "responsible": user(),
                               "priority": user()}, refs=("R4",))
    out, counts = gate([a], plan=make_plan(stated=stated, targets=("t1", "t2")),
                       resolution={"t1": Resolution(RESOLVED, SID_A),
                                   "t2": Resolution(RESOLVED, SID_B)})
    # The stated value wins; a casefold-equal name is not an override; t2's priority
    # does not cover t1.
    assert out["actions"][0]["proposed"] == {"deadline": "2026-11-14",
                                             "responsible": "Sam Lee"}
    assert counts == {"user_value_overridden__deadline": 1,
                      "field_unsourced__priority": 1}


def test_user_without_stated_value_removed():
    # Nothing stated and no record or document among its refs (only the item's own row).
    a = upd(SID_A, {"priority": "high", "status": "blocked"}, ref="t1",
            sources={"priority": user(), "status": ev("R1")}, refs=("R4",))
    out, counts = gate([a], plan=make_plan(
        stated=[{"target": "t1", "field": "deadline", "value": "2026-11-14"}]))
    assert out["actions"][0]["proposed"] == {"status": "blocked"}
    assert counts == {"field_unsourced__priority": 1}


def test_user_status_still_has_to_be_legal():
    # A stated status replaces the writer's; it must still be a legal change for the type.
    stated = [{"target": "t1", "field": "status", "value": "in_progress"}]
    a = upd(SID_A, {"status": "done"}, ref="t1", atype="close_item",
            sources={"status": user()})
    out, counts = gate([a], plan=make_plan(stated=stated))
    assert out["actions"] == []
    assert counts == {"user_value_overridden__status": 1, "drop_illegal_after_gate": 1}


def test_r3_assumed_default_uses_raised_on():
    # R3: "Nobody has said when Sam's invoice (#4) is due. Just pick a date."
    a = upd(SID_A, {"deadline": "2026-11-21"}, ref="t1", sources={"deadline": assumed()},
            refs=("R4",))
    for deleg in ({"target": "t1", "field": "deadline"},
                  {"target": None, "field": "deadline"}):
        out, counts = gate([a], plan=make_plan(delegations=[deleg]))
        [got] = out["actions"]
        assert got["proposed"] == {"deadline": "2026-10-20"}         # the item's raised_on
        assert got["assumed"] == ["deadline"]
        assert counts == {"date_assumed": 1}


def test_assumed_default_same_day_is_today():
    import datetime as dt
    items = make_items()
    items[SID_A]["raised_on"] = dt.date(2026, 11, 7)                  # raised today
    a = upd(SID_A, {"deadline": "2026-11-14"}, ref="t1", sources={"deadline": assumed()},
            refs=("R4",))
    out, counts = gate([a], items=items,
                       plan=make_plan(delegations=[{"target": "t1", "field": "deadline"}]))
    [got] = out["actions"]
    assert got["proposed"] == {"deadline": "2026-11-07"} and got["assumed"] == ["deadline"]


def test_assumed_default_refused_when_deadline_text_present():
    deleg = [{"target": "t1", "field": "deadline"}]
    a = upd(SID_A, {"deadline": "2026-11-14"}, ref="t1", sources={"deadline": assumed()},
            refs=("R4",))
    for key, value in (("deadline_text", "end of the month"), ("deadline", "2026-11-30"),
                       ("raised_on", None)):
        items = make_items()
        items[SID_A][key] = value
        out, counts = gate([a], items=items, plan=make_plan(delegations=deleg))
        assert out["actions"] == [], key
        assert counts == {"field_unsourced__deadline": 1, "drop_no_sourced_fields": 1}
    # No delegation for this target, or for this field: refused too.
    for d in ([], [{"target": "t2", "field": "deadline"}]):
        out, _ = gate([a], plan=make_plan(delegations=d, targets=("t1", "t2")))
        assert out["actions"] == []
    # Only the deadline may be assumed.
    b = upd(SID_A, {"priority": "low"}, ref="t1", sources={"priority": assumed()})
    out, counts = gate([b], plan=make_plan(delegations=deleg))
    assert counts["field_unsourced__priority"] == 1


def test_web_ref_cannot_source_field():
    a = upd(SID_A, {"priority": "high", "status": "blocked"}, ref="t1",
            sources={"priority": ev("R3"), "status": ev("R2")}, refs=("R2", "R3"))
    out, counts = gate([a])
    assert out["actions"][0]["proposed"] == {"status": "blocked"}        # a document: ok
    assert counts == {"field_unsourced__priority": 1}


def test_close_needs_source():
    close = upd(SID_A, {"status": "done"}, ref="t1", atype="close_item", sources={})
    out, counts = gate([close])
    assert out["actions"] == []
    assert counts == {"field_unsourced__status": 1, "drop_no_sourced_fields": 1}
    close["field_sources"] = {"status": ev("R1")}
    out, counts = gate([close])
    assert out["actions"][0]["proposed"] == {"status": "done"} and counts == {}


def test_create_optional_fields_follow_the_rules():
    create = {"type": "create_item", "target_stable_id": None, "target_ref": None,
              "proposed": {"text": "Book the pump", "priority": "high",
                           "responsible": "Sam", "deadline": "2026-11-20"},
              "field_sources": {"priority": ev("R4"), "responsible": ev("R1"),
                                "deadline": assumed()},
              "rationale": "r", "refs": ["R1"]}
    out, counts = gate([create],
                       plan=make_plan(delegations=[{"target": None, "field": "deadline"}]))
    # The text keeps its refs rule; an assumed deadline needs an item's raised_on.
    assert out["actions"][0]["proposed"] == {"text": "Book the pump", "responsible": "Sam"}
    assert counts == {"field_unsourced__priority": 1, "field_unsourced__deadline": 1}



# ---- Task 4.4: rule 4, deadline sanity ----------------------------------------------------

def test_said_on_mismatch_removes_deadline():
    def deadline(said_on, refs=("R1",)):
        return upd(SID_A, {"deadline": "2026-11-06", "status": "in_progress"}, ref="t1",
                   sources={"deadline": ev(*refs, said_on=said_on), "status": ev("R1")})
    out, counts = gate([deadline(TOPIC_DAY)])                 # R1 was said on TOPIC_DAY
    assert out["actions"][0]["proposed"]["deadline"] == "2026-11-06" and counts == {}
    for said_on, refs in (("2026-10-27", ("R1",)), (None, ("R1",)),
                          ("2026-10-20", ("R1", "R2"))):      # a document has no date
        out, counts = gate([deadline(said_on, refs)])
        assert out["actions"][0]["proposed"] == {"status": "in_progress"}, said_on
        assert counts == {"field_said_on_mismatch": 1}
    # The own item ref's date is its raised_on (a deadline from its deadline_text).
    items = make_items()
    items[SID_A]["deadline_text"] = "in two weeks"
    a = upd(SID_A, {"deadline": "2026-11-03"}, ref="t1", refs=("R4",),
            sources={"deadline": ev("R4", "R1", said_on=TOPIC_DAY)})
    out, counts = gate([a], items=items)
    assert out["actions"][0]["proposed"] == {"deadline": "2026-11-03"} and counts == {}
    a["field_sources"]["deadline"] = ev("R4", said_on="2026-10-21")
    out, counts = gate([a], items=items)
    assert out["actions"] == [] and counts["field_said_on_mismatch"] == 1


def test_deadline_before_source_removed():
    a = upd(SID_A, {"deadline": "2026-10-27", "priority": "high"}, ref="t1",
            sources={"deadline": ev("R1", said_on=TOPIC_DAY), "priority": ev("R1")})
    out, counts = gate([a])
    assert out["actions"][0]["proposed"] == {"priority": "high"}
    assert counts == {"field_deadline_before_source": 1}
    # The same day is not before it.
    a["proposed"]["deadline"] = TOPIC_DAY
    out, counts = gate([a])
    assert out["actions"][0]["proposed"]["deadline"] == TOPIC_DAY and counts == {}


def test_create_deadline_follows_rule_4():
    create = {"type": "create_item", "target_stable_id": None, "target_ref": None,
              "proposed": {"text": "Book the pump", "deadline": "2026-11-02"},
              "field_sources": {"deadline": ev("R1", said_on="2026-10-01")},
              "rationale": "r", "refs": ["R1"]}
    out, counts = gate([create])
    assert out["actions"][0]["proposed"] == {"text": "Book the pump"}
    assert counts == {"field_said_on_mismatch": 1}


# ---- review fixes ------------------------------------------------------------------------

def test_user_without_stated_value_falls_back_to_evidence():
    # "Close #4": the planner recorded no stated status, but the action cites a chunk
    # that says the work is finished -- a stated value OR evidence (spec §3.2.4).
    close = upd(SID_A, {"status": "done"}, ref="t1", atype="close_item",
                sources={"status": user()}, refs=("R4", "R9"))
    out, counts = gate([close])
    assert out["actions"][0]["proposed"] == {"status": "done"}
    assert counts == {"user_value_unstated__status": 1}
    # Only the item's own row: still unsourced.
    close["refs"] = ["R4"]
    out, counts = gate([close])
    assert out["actions"] == []
    assert counts == {"field_unsourced__status": 1, "drop_no_sourced_fields": 1}
    # A deadline falling back meets rule 4 too: no said_on -> removed.
    a = upd(SID_A, {"deadline": "2026-11-14"}, ref="t1", sources={"deadline": user()},
            refs=("R9",))
    out, counts = gate([a])
    assert out["actions"] == [] and counts["field_said_on_mismatch"] == 1


def test_wrong_target_ref_is_corrected_to_the_target_that_resolved_there():
    res = {"t1": Resolution(RESOLVED, SID_A), "t2": Resolution(NONE)}
    close = upd(SID_A, {"status": "done"}, ref="t2", atype="close_item")
    out, counts = gate([close], plan=make_plan(targets=("t1", "t2")), resolution=res)
    assert [(a["type"], a["target_stable_id"]) for a in out["actions"]] == [
        ("close_item", SID_A)]
    assert counts == {"target_ref_corrected": 1}
    # The stated value is the corrected target's.
    stated = [{"target": "t1", "field": "status", "value": "closed"}]
    close["field_sources"] = {"status": user()}
    out, counts = gate([close], plan=make_plan(targets=("t1", "t2"), stated=stated),
                       resolution=res)
    assert out["actions"][0]["proposed"] == {"status": "closed"}
    # C4's look-alike (no other target resolved to it) is still a mismatch.
    out, counts = gate([upd(SID_B, {"status": "done"}, ref="t1", atype="close_item")],
                       plan=make_plan(targets=("t1", "t2")), resolution=res)
    assert counts == {"drop_named_mismatch": 1}


def test_field_refs_join_the_action_only_when_the_field_survives():
    a = upd(SID_A, {"status": "blocked", "priority": "high"}, ref="t1", refs=("R1",),
            sources={"status": ev("R2"), "priority": ev("R4", "R5")})
    out, counts = gate([a])
    assert out["actions"][0]["proposed"] == {"status": "blocked"}
    assert out["actions"][0]["refs"] == ["R1", "R2"]
    assert counts == {"field_unsourced__priority": 1}


# ---- PR-5 Task 5.2: the index map (spec 2026-11-05 §4.2) ---------------------------------

def test_gate_returns_the_index_map_of_kept_demoted_and_dropped_actions():
    res = {"t1": Resolution(RESOLVED, SID_A), "t2": Resolution(NONE)}
    actions = [upd(SID_B, {"priority": "high"}),                          # 0 dropped
               upd(SID_A, {"status": "in_progress"}, ref="t1"),           # 1 kept
               upd(SID_C, {"status": "blocked"}, ref="t2"),               # 2 demoted
               {"type": "note", "target_stable_id": None, "proposed": {"text": "n"},
                "rationale": "r", "refs": ["R1"]},                       # 3 untouched
               upd(SID_A, {"priority": "high"}, ref="t1",
                   sources={"priority": ev("R4")})]                       # 4 unsourced
    out, counts, index_map = gate(actions, plan=make_plan(targets=("t1", "t2")),
                                  resolution=res, index_map=True)
    assert [a["type"] for a in out["actions"]] == ["update_item", "note", "note"]
    assert index_map == {0: None, 1: 0, 2: 1, 3: 2, 4: None}
    assert counts == {"drop_unasked_target": 1, "demoted_target_none": 1,
                      "field_unsourced__priority": 1, "drop_no_sourced_fields": 1}
