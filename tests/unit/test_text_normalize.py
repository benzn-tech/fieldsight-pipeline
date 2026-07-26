from text_normalize import diff_candidates, first_match_span, normalize, occurrences


def _a(wrong, right):
    return {"wrong_term": wrong, "right_term": right}


def test_whole_word_only_no_partial_corruption():
    # "Mackon" must not be rewritten inside "Mackonsson"
    out = normalize("Mackonsson met Mackon today", [_a("Mackon", "McCahon")])
    assert out == "Mackonsson met McCahon today"


def test_case_aware_preserves_surface_casing():
    aliases = [_a("mackon", "mccahon")]
    assert normalize("mackon", aliases) == "mccahon"          # lower -> lower
    assert normalize("Mackon", aliases) == "Mccahon"          # Title -> Title
    assert normalize("MACKON", aliases) == "MCCAHON"          # UPPER -> UPPER


def test_multiple_aliases_applied_in_order():
    out = normalize("Fyfe poured the slab",
                    [_a("Fyfe", "Fife"), _a("slab", "raft")])
    assert out == "Fife poured the raft"


def test_no_aliases_is_identity():
    assert normalize("unchanged text", []) == "unchanged text"


def test_occurrences_counts_only_whole_words():
    # Same pattern normalize() rewrites with -- a preview count can never
    # promise a change normalize wouldn't make.
    text = "Mackonsson met Mackon, then MACKON left"
    assert occurrences(text, "Mackon") == 2                   # not Mackonsson
    assert occurrences(text, "mackon") == 2                   # case-insensitive
    assert occurrences(text, "Fyfe") == 0
    assert occurrences("", "Mackon") == 0 and occurrences(text, "") == 0


def test_first_match_span_points_at_the_first_whole_word_hit():
    text = "Mackonsson met Mackon today"
    span = first_match_span(text, "Mackon")
    assert text[span[0]:span[1]] == "Mackon" and span[0] == 15  # not index 0
    assert first_match_span(text, "Fyfe") is None


def test_diff_candidates_surfaces_new_proper_nouns_only():
    cands = diff_candidates("the crew from Mackon arrived",
                            "the crew from McCahon arrived early")
    assert "McCahon" in cands
    assert "arrived" not in cands       # already present in before
    assert "the" not in cands           # not proper-noun-like
