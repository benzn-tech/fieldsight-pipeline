"""item_text_match: one tokeniser shared by item history and item support.

Chinese must never score zero -- an ASCII-only tokeniser silently makes every
Chinese item look unsupported.
"""
import item_text_match as itm


def test_ascii_words_lowercased_and_stopwords_dropped():
    toks = itm.tokens("The Scaffold IS on the North wall")
    assert toks == ["scaffold", "north", "wall"]


def test_cjk_bigrams_score_chinese_overlap():
    score, matched = itm.overlap("二楼防水膜明天到", "二楼的防水膜明天会到")
    assert score >= 0.5
    assert "防水" in matched


def test_cjk_spaced_turn_text_still_matches():
    # Turn text is space-joined per word; the item is written unspaced.
    score, _ = itm.overlap("防水膜", "防 水 膜")
    assert score == 1.0


def test_mixed_language_tokens():
    toks = itm.tokens("Level 2 防水膜")
    for t in ("level", "2", "防水", "水膜"):
        assert t in toks


def test_single_cjk_char_is_unigram():
    toks = itm.tokens("Level 2 楼 north")
    assert "楼" in toks
    assert toks == ["level", "2", "楼", "north"]


def test_empty_item_scores_zero():
    assert itm.overlap("", "anything at all") == (0.0, [])
    assert itm.overlap("the of and", "the of and") == (0.0, [])
    assert itm.overlap(None, "x") == (0.0, [])


def test_matched_terms_sorted_and_deterministic():
    item = "pour slab Thursday, pour slab"
    turn = "we will pour the slab on thursday"
    a = itm.overlap(item, turn)
    b = itm.overlap(item, turn)
    assert a == b
    assert a == (1.0, ["pour", "slab", "thursday"])


def test_containment_ignores_multiplicity():
    score, matched = itm.overlap("crane crane crane delivery", "crane today")
    assert score == 0.5
    assert matched == ["crane"]


def test_rule_version():
    assert itm.RULE_VERSION == "lexical_v1"


def test_contraction_fragments_are_not_content():
    for apos in ("'", "’"):
        assert itm.overlap(f"Tom{apos}s crane", f"it{apos}s fine")[0] == 0.0
        toks = itm.tokens(f"Tom{apos}s crane won{apos}t arrive, we{apos}re done")
        assert toks == ["tom", "crane", "arrive", "done"]


def test_single_letter_domain_tokens_survive():
    assert itm.tokens("Block B") == ["block", "b"]
    assert itm.tokens("Level 2") == ["level", "2"]


def test_spoken_fillers_dropped():
    toks = itm.tokens("ok yeah so um we just got the crane going right")
    assert toks == ["crane"]
