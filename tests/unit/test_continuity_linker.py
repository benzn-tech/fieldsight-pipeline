import hashlib

import item_continuity as ic
import status_suggestions as ss

def _topics():
    return [{"action_items": [{"action": "Book the crane for Tuesday"}, "bare string"],
             "findings": [{"observation": "Scaffold tag missing on level 2"}]},
            {"action_items": [{"action": "Call the electrician"}]}]

def test_refs_follow_children_order_and_skip_non_dicts():
    refs = ic.assign_refs(_topics())
    assert [(r, ln) for r, ln, _ in refs] == [("N1", "action_items"), ("N2", "findings"), ("N3", "action_items")]

def test_linker_prompt_parts_in_spec_order_and_transcript_verbatim():
    prior = [ic.PriorItem("A1", "action_items", "Book crane Tuesday", "id-1")]
    refs = ic.assign_refs(_topics())
    p = ic.render_linker_prompt(prior, refs, "[09:00:00] spk_0: book the crane", ss.LINKER_INSTRUCTION)
    assert p.index(ic.LINKER_INSTRUCTION) < p.index(ss.LINKER_INSTRUCTION) < p.index("<<<PRIOR_ITEMS>>>") \
        < p.index('"ref": "N1"') < p.index("[09:00:00] spk_0: book the crane")
    assert ic.CONTINUITY_INSTRUCTION not in p

def test_attach_links_sets_fields_and_counts_bad_entries():
    topics = _topics(); refs = ic.assign_refs(topics)
    parsed = {"links": [{"ref": "N1", "continues": {"id": "A1", "starts": "Book crane Tuesday"},
                         "status_suggestion": {"status": "done", "at": "09:00:00", "quote": "x"}},
                        {"ref": "N9", "continues": {"id": "A1", "starts": "Book"}},
                        {"ref": "N1", "continues": {"id": "A1", "starts": "Book"}},
                        {"ref": "N2", "status_suggestion": {"status": "done"}},
                        "junk"]}
    dropped = ic.attach_links(parsed, refs)
    assert dropped == 4      # unknown ref, duplicate ref, suggestion on a finding, non-dict
    assert topics[0]["action_items"][0]["continues"]["id"] == "A1"
    assert "status_suggestion" not in topics[0]["findings"][0]

def test_attach_links_tolerates_missing_or_bad_links_key():
    refs = ic.assign_refs(_topics())
    assert ic.attach_links({}, refs) == 0
    assert ic.attach_links({"links": "nope"}, refs) == 0

def test_exact_match_still_wins_over_a_linker_claim():
    prior = [ic.PriorItem("A1", "action_items", "Call the electrician", "11111111-1111-4111-8111-111111111111")]
    topics = [{"action_items": [{"action": "Call the electrician"}]}]
    refs = ic.assign_refs(topics)
    ic.attach_links({"links": [{"ref": "N1", "continues": {"id": "A1", "starts": "Call the electrician"}}]}, refs)
    claims = ic.resolve(topics, prior)
    assert topics[0]["action_items"][0]["item_id"] == prior[0].item_id
    assert claims[0]["guard"] == "exact_matched_item"

def test_linker_question_set_is_distinct_and_hashes_its_own_text():
    assert ic.LINKER_QUESTION_SET.startswith("item_continuity_linker:")
    assert ic.LINKER_QUESTION_SET != ic.QUESTION_SET
    assert ic.LINKER_QUESTION_SET == ("item_continuity_linker:"
        + hashlib.sha256(ic.LINKER_INSTRUCTION.encode()).hexdigest()[:16])

def test_linker_status_instruction_rewords_only_the_opening():
    assert ss.LINKER_INSTRUCTION.startswith("For an action item you link to an earlier item")
    assert ss.LINKER_INSTRUCTION.split("you may add", 1)[1] == ss.INSTRUCTION.split("you may add", 1)[1]

def test_non_string_item_text_is_listed_with_empty_text():
    topics = [{"action_items": [{"action": 5}, {"action": ["a", "b"]}, {"action": "Call the electrician"}]}]
    refs = ic.assign_refs(topics)
    p = ic.render_linker_prompt([], refs, "x")
    assert '{"ref": "N1", "kind": "action_item", "text": ""}' in p
    assert '{"ref": "N2", "kind": "action_item", "text": ""}' in p
    assert '{"ref": "N3", "kind": "action_item", "text": "Call the electrician"}' in p
