"""Structure-first chunking (spec section "Chunking")."""
import project_doc_chunks as c


def H(level, text, page=None):
    return {"kind": "heading", "level": level, "text": text, "page": page}


def P(text, page=None):
    return {"kind": "para", "level": None, "text": text, "page": page}


def test_a_chunk_never_spans_two_top_level_headings():
    out = c.chunk([H(1, "General"), P("a" * 100), H(1, "Concrete"), P("b" * 100)])
    assert [(x["heading"], x["chunk_text"][0]) for x in out] == [("General", "a"), ("Concrete", "b")]


def test_the_heading_path_is_carried_and_trimmed_by_level():
    out = c.chunk([H(1, "3 Concrete"), H(2, "3.2 Formwork"), H(3, "Tolerances"), P("5 mm"),
                   H(2, "3.3 Curing"), P("7 days")])
    assert [x["heading"] for x in out] == ["3 Concrete > 3.2 Formwork > Tolerances",
                                           "3 Concrete > 3.3 Curing"]


def test_a_deeper_heading_is_kept_as_text_not_a_path():
    out = c.chunk([H(1, "A"), H(4, "A.1.1.1 Note"), P("text")])
    assert out[0]["heading"] == "A" and out[0]["chunk_text"].startswith("A.1.1.1 Note")


def test_long_sections_split_near_the_target_with_overlap_and_never_over_the_cap():
    words = " ".join(f"word{i}" for i in range(1200))       # ~10,000 chars
    out = c.chunk([H(1, "Long"), P(words, page=4)])
    assert len(out) > 3
    assert all(len(x["chunk_text"]) <= c.HARD_CAP for x in out)
    assert all(len(x["chunk_text"]) <= c.TARGET + c.OVERLAP + 2 for x in out)
    # overlap: the next chunk starts with the tail of the previous one
    tail = out[0]["chunk_text"][-60:]
    assert tail in out[1]["chunk_text"][:c.OVERLAP + 10]
    assert all(x["page_from"] == 4 and x["page_to"] == 4 for x in out)


def test_no_overlap_crosses_a_heading():
    out = c.chunk([H(1, "A"), P("alpha " * 50), H(1, "B"), P("beta " * 50)])
    assert "alpha" not in out[1]["chunk_text"]


def test_text_without_spaces_is_cut_at_the_cap():
    """Review Focus 3: Chinese has no spaces; a split that only looks for spaces would emit
    one 10,000-character chunk."""
    out = c.chunk([H(1, "规范"), P("混凝土保护层厚度" * 1250)])
    assert out and all(len(x["chunk_text"]) <= c.HARD_CAP for x in out)


def test_page_ranges_span_the_paragraphs_in_the_chunk():
    out = c.chunk([H(1, "A"), P("x" * 500, page=2), P("y" * 500, page=3)])
    assert (out[0]["page_from"], out[0]["page_to"]) == (2, 3)


def test_ordinals_count_from_zero_and_empty_paragraphs_are_dropped():
    out = c.chunk([P("   "), P("one"), H(1, "B"), P("two")])
    assert [x["ordinal"] for x in out] == [0, 1]
    assert out[0]["heading"] is None


def test_the_heading_path_is_embedded_but_not_stored():
    x = c.chunk([H(1, "3.2 Formwork"), P("Tolerance 5 mm")])[0]
    assert x["chunk_text"] == "Tolerance 5 mm"
    assert c.embed_text(x) == "3.2 Formwork\n\nTolerance 5 mm"
    assert c.embed_text(dict(x, heading=None)) == "Tolerance 5 mm"


# --- Fix round 1 --------------------------------------------------------------------

def test_a_cjk_full_stop_is_a_cut_point():
    sentence = "混凝土保护层厚度应符合设计要求" * 6 + "。"        # 91 chars
    out = c.chunk([H(1, "规范"), P(sentence * 60)])
    assert len(out) > 1
    assert out[0]["chunk_text"].endswith("。")


def test_the_cut_point_is_the_target_not_the_hard_cap():
    out = c.chunk([H(1, "A"), P("x" * 5000)])
    assert len(out[0]["chunk_text"]) == c.TARGET


def test_split_long_terminates_and_loses_nothing_on_a_run_with_no_break():
    pieces = c._split_long("x" * 100_000, c.TARGET)
    assert "".join(pieces) == "x" * 100_000
    assert all(len(p) <= c.TARGET for p in pieces)


def _rebuild(chunks):
    """The chunks with each overlap removed, concatenated."""
    parts = []
    for i, x in enumerate(chunks):
        text = x["chunk_text"]
        if i:
            carry = c._tail(chunks[i - 1]["chunk_text"])
            assert text.startswith(carry + "\n\n")
            text = text[len(carry) + 2:]
        parts.append(text)
    return " ".join(parts)


def test_chunks_minus_overlap_rebuild_the_input():
    paras = [" ".join(f"w{j}_{i}" for i in range(60 + 37 * j)) for j in range(12)]
    out = c.chunk([H(1, "A")] + [P(p) for p in paras])
    assert len(out) > 2
    assert _rebuild(out).split() == " ".join(paras).split()


def test_zero_overlap_carries_nothing(monkeypatch):
    monkeypatch.setattr(c, "OVERLAP", 0)
    assert c._tail("alpha beta gamma") == ""
    out = c.chunk([H(1, "A"), P("word " * 1000)])
    assert len(out) > 1
    assert sum(len(x["chunk_text"].split()) for x in out) == 1000
