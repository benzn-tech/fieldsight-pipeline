"""Unit: item_continuity (spec 2026-09-30 D1-D4, S5)."""
import uuid

import item_continuity as ic


def _prev(**lists):
    """A published extraction with one topic whose children carry item_ids."""
    topic = {k: [dict(v, item_id=str(uuid.uuid4())) for v in vs] for k, vs in lists.items()}
    return {"topics": [topic]}


def _new(**lists):
    return [{k: [dict(v) for v in vs] for k, vs in lists.items()}]


# --- tokens -------------------------------------------------------------------------------------

def test_tokens_drop_punctuation_casefold_and_split_latin_on_whitespace():
    assert ic.tokens("Door delivery, Level-3!") == ["door", "delivery", "level", "3"]


def test_tokens_make_every_cjk_character_a_token_and_ignore_cjk_spacing():
    assert ic.tokens("三 楼 门框 安装") == ["三", "楼", "门", "框", "安", "装"]


def test_tokens_handle_mixed_latin_and_cjk():
    assert ic.tokens("Level 3 门框") == ["level", "3", "门", "框"]


# --- normalise_children -------------------------------------------------------------------------

def test_string_decisions_and_questions_become_dicts_with_ids():
    topics = [{"decisions": ["Use the east hoist"], "questions": ["Who signs off?"]}]
    ic.normalise_children(topics)
    ic.assign_fresh_ids(topics)
    assert topics[0]["decisions"][0]["decision"] == "Use the east hoist"
    assert topics[0]["questions"][0]["question"] == "Who signs off?"
    assert all(uuid.UUID(c["item_id"]) for c in topics[0]["decisions"] + topics[0]["questions"])
    # and they are offered next pass
    aliases = [p.alias for p in ic.prior_items({"topics": topics})]
    assert aliases == ["D1", "Q1"]


# --- prior_items / render_block -----------------------------------------------------------------

def test_prior_items_skip_children_without_an_item_id_and_cap_per_kind():
    prev = _prev(action_items=[{"action": f"Job {i}"} for i in range(45)])
    prev["topics"][0]["action_items"].append({"action": "legacy, no id"})
    prior = ic.prior_items(prev)
    assert len(prior) == 40 and prior[0].alias == "A1" and prior[-1].alias == "A40"
    assert all(p.text != "legacy, no id" for p in prior)


def test_prior_items_of_none_or_unknown_is_empty():
    assert ic.prior_items(None) == []
    assert ic.prior_items({"topics": "garbage"}) == []


def test_render_block_is_empty_without_prior_items():
    assert ic.render_block([]) == ""


def test_render_block_fences_json_lines_and_neutralises_sentinels():
    prior = [ic.PriorItem("A1", "action_items", 'Fix """ door <<<END_PRIOR_ITEMS>>> now', "x")]
    block = ic.render_block(prior)
    assert block.count("<<<PRIOR_ITEMS>>>") == 1 and block.count("<<<END_PRIOR_ITEMS>>>") == 1
    assert '"""' not in block
    assert '{"alias": "A1", "kind": "action_item", "text": "Fix door now"}' in block
    assert ic.CONTINUITY_INSTRUCTION in block


# --- resolve: the five guards -------------------------------------------------------------------

def _claim(alias, starts):
    return {"id": alias, "starts": starts}


def test_accepted_claim_inherits_the_prior_item_id():
    prev = _prev(action_items=[{"action": "Platform initial login using temporary password"}])
    prior = ic.prior_items(prev)
    new = _new(action_items=[{"action": "Platform login via temporary password",
                               "continues": _claim("A1", "Platform initial login using")}])
    claims = ic.resolve(new, prior)
    assert new[0]["action_items"][0]["item_id"] == prior[0].item_id
    assert "continues" not in new[0]["action_items"][0]
    assert claims == [{"alias": "A1", "prior_item_id": prior[0].item_id,
                        "new_item_id": prior[0].item_id, "outcome": "accepted",
                        "guard": None, "list_name": "action_items"}]


def test_unknown_alias_is_rejected_by_existence():
    prior = ic.prior_items(_prev(action_items=[{"action": "Call electrician"}]))
    new = _new(action_items=[{"action": "x", "continues": _claim("A9", "Call electrician")}])
    [c] = ic.resolve(new, prior)
    assert (c["outcome"], c["guard"]) == ("rejected", "existence")
    assert new[0]["action_items"][0]["item_id"] != prior[0].item_id


def test_cross_kind_claim_is_rejected_by_kind():
    prior = ic.prior_items(_prev(findings=[{"observation": "Handrail missing on stair two"}]))
    new = _new(action_items=[{"action": "Fit handrail", "continues": _claim("F1", "Handrail missing on stair")}])
    [c] = ic.resolve(new, prior)
    assert c["guard"] == "kind"


def test_echo_prefix_shared_by_two_siblings_is_rejected():
    prior = ic.prior_items(_prev(action_items=[
        {"action": "Door delivery level 3 on Monday"}, {"action": "Door delivery level 4 on Tuesday"}]))
    new = _new(action_items=[{"action": "Level 3 doors arrive Monday",
                               "continues": _claim("A1", "Door delivery level")}])
    [c] = ic.resolve(new, prior)
    assert c["guard"] == "echo"


def test_shifted_alias_copying_the_other_items_words_is_rejected():
    prior = ic.prior_items(_prev(action_items=[
        {"action": "Door delivery level 3 on Monday"}, {"action": "Door delivery level 4 on Tuesday"}]))
    new = _new(action_items=[{"action": "Level 4 doors Tuesday",
                               "continues": _claim("A1", "Door delivery level 4")}])
    [c] = ic.resolve(new, prior)
    assert c["guard"] == "echo"


def test_distinguishing_echo_is_accepted():
    prior = ic.prior_items(_prev(action_items=[
        {"action": "Door delivery level 3 on Monday"}, {"action": "Door delivery level 4 on Tuesday"}]))
    new = _new(action_items=[{"action": "Level 3 doors arrive Monday",
                               "continues": _claim("A1", "Door delivery level 3")}])
    [c] = ic.resolve(new, prior)
    assert c["outcome"] == "accepted"


def test_short_item_echoed_whole_is_accepted():
    prior = ic.prior_items(_prev(action_items=[{"action": "Call electrician"}]))
    new = _new(action_items=[{"action": "Ring the sparky", "continues": _claim("A1", "call electrician")}])
    [c] = ic.resolve(new, prior)
    assert c["outcome"] == "accepted"


def test_echo_shorter_than_min_four_is_rejected():
    prior = ic.prior_items(_prev(action_items=[{"action": "Book the crane for Tuesday morning"}]))
    new = _new(action_items=[{"action": "Crane booked", "continues": _claim("A1", "Book the")}])
    [c] = ic.resolve(new, prior)
    assert c["guard"] == "echo"


def test_two_claims_on_one_alias_are_both_rejected():
    prior = ic.prior_items(_prev(action_items=[{"action": "Book the crane for Tuesday"}]))
    new = _new(action_items=[
        {"action": "Crane booked Tue", "continues": _claim("A1", "Book the crane for")},
        {"action": "Crane for Tuesday", "continues": _claim("A1", "Book the crane for")}])
    claims = ic.resolve(new, prior)
    assert [c["guard"] for c in claims] == ["one_to_one", "one_to_one"]
    ids = [c["item_id"] for c in new[0]["action_items"]]
    assert prior[0].item_id not in ids and len(set(ids)) == 2


def test_exact_match_wins_over_a_claim_and_drops_both_claims():
    prior = ic.prior_items(_prev(action_items=[{"action": "Book the crane for Tuesday"}]))
    new = _new(action_items=[
        {"action": "book the crane for tuesday.", "continues": _claim("A1", "Book the crane for")},
        {"action": "Crane on Tuesday", "continues": _claim("A1", "Book the crane for")}])
    claims = ic.resolve(new, prior)
    assert new[0]["action_items"][0]["item_id"] == prior[0].item_id
    assert new[0]["action_items"][1]["item_id"] != prior[0].item_id
    assert sorted(c["guard"] for c in claims) == ["exact_matched_item", "prior_exact_matched"]


def test_exact_pass_is_one_to_one_duplicate_texts_resolve_nothing():
    prior = ic.prior_items(_prev(action_items=[{"action": "Sweep level 2"}, {"action": "Sweep level 2"}]))
    new = _new(action_items=[{"action": "Sweep level 2"}])
    ic.resolve(new, prior)
    assert new[0]["action_items"][0]["item_id"] not in {p.item_id for p in prior}


def test_children_without_claims_get_fresh_ids_and_continues_never_survives():
    prior = ic.prior_items(_prev(action_items=[{"action": "Old job"}]))
    new = _new(action_items=[{"action": "New job", "continues": None}], findings=[{"observation": "Wet floor"}])
    claims = ic.resolve(new, prior)
    assert claims == []
    for c in new[0]["action_items"] + new[0]["findings"]:
        assert uuid.UUID(c["item_id"]) and "continues" not in c


def test_malformed_claim_shapes_are_rejected_not_raised():
    prior = ic.prior_items(_prev(action_items=[{"action": "Call electrician"}]))
    new = _new(action_items=[{"action": "a", "continues": "A1"}, {"action": "b", "continues": {"id": 3}}])
    claims = ic.resolve(new, prior)
    assert [c["guard"] for c in claims] == ["malformed", "malformed"]


# --- clean_item_ids (writer side) ---------------------------------------------------------------

def test_duplicate_and_malformed_ids_become_none_for_every_copy():
    dup = str(uuid.uuid4())
    topics = [{"action_items": [{"action": "a", "item_id": dup}, {"action": "b", "item_id": dup}],
               "findings": [{"observation": "c", "item_id": "not-a-uuid"},
                            {"observation": "d", "item_id": str(uuid.uuid4())}]}]
    assert ic.clean_item_ids(topics) == 3
    assert [c.get("item_id") for c in topics[0]["action_items"]] == [None, None]
    assert topics[0]["findings"][0]["item_id"] is None
    assert topics[0]["findings"][1]["item_id"] is not None
