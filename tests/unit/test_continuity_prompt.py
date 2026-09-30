"""The continuity block is a parameter; without it the prompt is today's, and the group prompt
never changes (spec D7, D8)."""
import importlib

import item_continuity as ic
import lambda_extract_session as les

GID = "a" * 32


def _turns():
    return [{"abs_start_str": "09:00:00", "speaker": "spk_0", "text": "Book the crane for Tuesday",
             "source_filename": "f.wav"}]


def _group_artifact():
    return {"groupId": GID, "leadSessionId": GID,
            "members": [{"userFolder": "Ben_UCPK", "date": "2026-08-12",
                        "sessionBase": "sid" + GID}]}


def _group_sources():
    return [{"session_id": "sid" + GID,
             "turns": [{"speaker": "spk_0", "text": "hi", "abs_start_str": "10:00:00"}]}]


def test_prompt_without_block_is_byte_identical_to_the_old_signature():
    old, _ = les.build_extraction_prompt("u", "2026-09-30", "sidx", _turns(), 1)
    new, _ = les.build_extraction_prompt("u", "2026-09-30", "sidx", _turns(), 1, continuity_block="")
    assert new == old


def test_empty_block_leaves_exactly_the_old_single_blank_line_before_instructions():
    """Controller Ruling C3: the test above is a tautology -- both calls go through the new
    function with the same default, so a whitespace regression around `{continuity_block}` would
    pass it. This asserts the seam itself: with continuity_block="" the transcript's closing
    fence is followed by exactly one blank line and then the instructions heading, never two."""
    prompt, _ = les.build_extraction_prompt("u", "2026-09-30", "sidx", _turns(), 1,
                                            continuity_block="")
    assert '"""\n\n## Instructions' in prompt
    assert '"""\n\n\n' not in prompt


def test_block_sits_after_the_transcript_fence_and_before_the_instructions():
    block = ic.render_block([ic.PriorItem("A1", "action_items", "Book the crane", "id")])
    prompt, _ = les.build_extraction_prompt("u", "2026-09-30", "sidx", _turns(), 1,
                                            continuity_block=block)
    transcript_end = prompt.rindex('"""')
    assert transcript_end < prompt.index("<<<PRIOR_ITEMS>>>") < prompt.index("## Instructions")


def test_group_prompt_is_byte_identical_under_both_flag_states(monkeypatch):
    """DECLARE_CONTINUITY does not exist as a real switch until a later task -- this test does
    not depend on it doing anything. It proves the group prompt has no continuity text and is
    identical regardless of what the env looks like, by reloading the module between flag
    states and comparing the output each time."""
    artifact, sources = _group_artifact(), _group_sources()
    monkeypatch.setenv("DECLARE_CONTINUITY", "false")
    importlib.reload(les)
    off = les.build_group_prompt(artifact, sources)
    monkeypatch.setenv("DECLARE_CONTINUITY", "true")
    importlib.reload(les)
    on = les.build_group_prompt(artifact, sources)
    monkeypatch.setenv("DECLARE_CONTINUITY", "false")
    importlib.reload(les)
    assert on == off and "PRIOR_ITEMS" not in on and "continues" not in on
