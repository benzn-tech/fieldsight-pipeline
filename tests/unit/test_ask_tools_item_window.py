"""ask_tools item window: an item shows the part of a chunk that matches the query
(spec 2026-10-22-ask-tools-item-window, amended by its review)."""
import random
import re

import ask_tools
from session_index import _window_line


# --- terms -------------------------------------------------------------------

def test_terms_drop_stopwords_and_asking_words_keep_order():
    assert ask_tools._window_terms("Why did the roofing subcontractor pull out?") == [
        "roofing", "subcontractor", "pull"]
    assert ask_tools._window_terms("When did we last talk about the crane?") == ["crane"]


def test_terms_keep_cjk_bigrams_unchanged():
    assert ask_tools._window_terms("屋面分包商为什么退出了") == [
        "屋面", "面分", "分包", "包商", "商为", "为什", "什么", "么退", "退出", "出了"]


def test_terms_mixed_identifier_and_cjk():
    assert ask_tools._window_terms("P3 方案") == ["p3", "方案"]


def test_terms_deduplicated():
    assert ask_tools._window_terms("crane crane pour") == ["crane", "pour"]


def test_terms_empty_inputs():
    for q in (None, "", "when did we last talk about it?", 42):
        assert ask_tools._window_terms(q) == [], q


# --- _window (pure) ------------------------------------------------------------

CAP = 1000
FILLER = ("we went over the general plan for the week and the rest of it",
          "the guys finished the formwork on the east side before lunch",
          "Tom said the delivery docket was missing a page again today",
          "nothing else from the site meeting worth writing down really")


def _line(i, text, speaker="spk_1"):
    return _window_line({"abs_start_str": f"08:{10 + i // 60:02d}:{i % 60:02d}",
                         "speaker": speaker, "text": text})


def _transcript(n, at=None):
    """n window lines built like session_index builds them; `at` maps a line index
    to its text."""
    at = at or {}
    return "\n".join(_line(i, at.get(i, FILLER[i % len(FILLER)])) for i in range(n))


def _units(shown, sep="\n"):
    return [u for u in shown.split(sep) if u != "…"]


def test_text_within_cap_is_returned_unchanged():
    for text in ("short", "line one\r\nline two\r", "混凝土浇筑\n钢筋" * 20, "x" * CAP):
        assert ask_tools._window(text, ["crane"], CAP) == (text, 0, "whole")


def test_no_hit_gives_a_line_aligned_head():
    text = _transcript(40)
    shown, first, mode = ask_tools._window(text, ["crane"], CAP)
    assert (first, mode) == (0, "head")
    assert len(shown) <= CAP
    lines = shown.split("\n")
    assert lines[-1] == "…"
    assert lines[:-1] == text.split("\n")[:len(lines) - 1]
    assert len(shown) > CAP - 100                     # filled, not one line


def test_evidence_past_char_2000_is_kept_on_whole_lines():
    text = _transcript(40, {30: "the crane hire is booked for Thursday at seven"})
    assert len(text) > 2500 and text.index("crane") > 1900
    shown, first, mode = ask_tools._window(text, ["crane"], CAP)
    assert mode == "window" and len(shown) <= CAP
    assert "the crane hire is booked" in shown
    assert shown.split("\n")[0] == "…"
    units = text.split("\n")
    assert all(u in units for u in _units(shown))
    assert first == units.index(_units(shown)[0])


def test_a_lone_hit_is_extended_around_it_not_shown_alone():
    """Review BLOCKER 1a: one hit on line 20 of 40 gives ~cap characters around it."""
    text = _transcript(40, {20: "the crane hire is booked for Thursday at seven"})
    units = text.split("\n")
    shown, first, _ = ask_tools._window(text, ["crane"], CAP)
    kept = _units(shown)
    assert len(shown) >= CAP - max(len(u) for u in units) - 4
    assert units[19] in kept and units[21] in kept
    # Alternating extension: the hit sits in the middle, give or take one line.
    pos = kept.index(units[20])
    assert abs(pos - (len(kept) - 1 - pos)) <= 1


def test_a_tie_goes_to_the_earlier_region():
    text = _transcript(40, {5: "crane on site", 35: "crane off site"})
    shown, _, _ = ask_tools._window(text, ["crane"], CAP)
    assert "crane on site" in shown and "crane off site" not in shown


def test_coverage_beats_hits():
    text = _transcript(40, {3: "pour pour pour pour", 30: "the pour is Friday",
                            31: "and the crane comes at six"})
    shown, _, _ = ask_tools._window(text, ["pour", "crane"], CAP)
    assert "the pour is Friday" in shown and "crane comes" in shown
    assert "pour pour pour" not in shown


def test_an_oversize_line_holding_the_only_hit_is_cut_around_it():
    """Review BLOCKER 1b: a unit longer than the cap is a candidate on its own."""
    long_text = ("blah " * 150 + "the crane hire is booked for Thursday " + "blah " * 120)
    text = _transcript(12, {6: long_text})
    assert len(text.split("\n")[6]) > 1300
    shown, first, mode = ask_tools._window(text, ["crane"], CAP)
    assert mode == "window" and first == 6
    assert len(shown) <= CAP
    assert "the crane hire is booked" in shown
    lines = shown.split("\n")
    assert lines[0] == "…" and lines[-1] == "…"
    cut = lines[1]
    assert cut.startswith("[08:10:06] spk_1: …") and cut.endswith("…")
    # Snapped to word boundaries: no half "blah" at either cut end.
    assert re.fullmatch(r"\[08:10:06\] spk_1: …(blah |the crane hire is booked for Thursday )*"
                        r"(blah|Thursday)…", cut), cut


def test_one_unit_with_no_newline_is_cut_around_the_hit():
    text = "word " * 400 + "crane " + "word " * 200
    shown, first, mode = ask_tools._window(text, ["crane"], CAP)
    assert (first, mode) == (0, "window") and len(shown) <= CAP
    assert "crane" in shown and shown.startswith("…") and shown.endswith("…")
    assert " wor…" not in shown and "…ord" not in shown


def test_cjk_unit_is_cut_hard_when_no_space_is_near():
    text = "浇筑" * 700 + "塔吊" + "浇筑" * 400
    shown, _, _ = ask_tools._window(text, ["塔吊"], CAP)
    assert len(shown) <= CAP and "塔吊" in shown and len(shown) > CAP - 5


def test_keep_first_holds_line_one():
    header = "[09:00 - 09:40] Level 3 slab pour (progress)"
    body = [f"Decision: item {i} " + FILLER[i % 4] for i in range(40)]
    body[33] = "Action: confirm the crane booking with Smith Cranes (due: Friday)"
    text = "\n".join([header] + body)
    shown, first, mode = ask_tools._window(text, ["crane"], CAP, keep_first=True)
    lines = shown.split("\n")
    assert mode == "window" and len(shown) <= CAP
    assert lines[0] == header and lines[1] == "…"
    assert body[33] in lines


def test_keep_first_without_a_hit_is_the_head():
    text = "\n".join(["Session 08:00–12:00"] + [f"- [08:{i:02d}] topic {i} " + FILLER[i % 4]
                                                 for i in range(40)])
    shown, _, mode = ask_tools._window(text, ["crane"], CAP, keep_first=True)
    assert mode == "head" and shown.startswith("Session 08:00–12:00\n- [08:00]")
    assert shown.endswith("\n…") and len(shown) <= CAP


def test_documents_centre_on_a_paragraph():
    paras = [f"Clause {i}. " + ("General requirement text that applies. " * 12) for i in range(6)]
    paras[4] = "Clause 4. Scaffold ties shall be inspected weekly by a competent person."
    text = "\n\n".join(paras)
    shown, _, mode = ask_tools._window(text, ["scaffold"], CAP, sep="\n\n")
    assert mode == "window" and len(shown) <= CAP
    assert paras[4] in shown.split("\n\n")
    assert shown.startswith("…\n\n")


def test_a_long_document_paragraph_is_cut_with_no_prefix_rule():
    para = "[12:00:00] Note: " + "general text " * 60 + "scaffold ties weekly " + "more text " * 60
    text = "\n\n".join(["Intro paragraph.", para, "Closing paragraph."])
    shown, _, _ = ask_tools._window(text, ["scaffold"], CAP, sep="\n\n")
    assert len(shown) <= CAP and "scaffold ties weekly" in shown
    assert "[12:00:00] Note:" not in shown


def test_chinese_query_over_chinese_text_centres_on_the_bigram():
    lines = {i: "今天上午在二号楼三层完成了楼板混凝土浇筑现场工程师检查了钢筋保护层" for i in range(40)}
    lines[30] = "屋面分包商说下周退出这个项目"
    text = _transcript(40, lines)
    shown, _, mode = ask_tools._window(text, ask_tools._window_terms("屋面分包商为什么退出了"), CAP)
    assert mode == "window" and "屋面分包商说下周退出" in shown


def test_chinese_query_over_english_text_is_the_head():
    text = _transcript(40, {30: "the roofing subcontractor is pulling out next week"})
    shown, _, mode = ask_tools._window(text, ask_tools._window_terms("屋面分包商为什么退出了"), CAP)
    assert mode == "head" and shown == ask_tools._window(text, [], CAP)[0]


def test_mixed_query_hits_through_its_latin_token():
    text = _transcript(40, {28: "so P3 is the option we priced last week"})
    shown, _, mode = ask_tools._window(text, ask_tools._window_terms("P3 方案"), CAP)
    assert mode == "window" and "so P3 is the option" in shown


def test_window_property_cap_units_determinism():
    rng = random.Random(20261022)
    alphabet = "abcdefghij klmnop qrstuvwxyz 浇筑钢筋塔吊混凝土 "
    for _ in range(200):
        sep = rng.choice(["\n", "\n", "\n\n"])
        units = ["".join(rng.choice(alphabet) for _ in range(rng.randint(5, 1600)))
                 for _ in range(rng.randint(1, 60))]
        if sep == "\n" and rng.random() < 0.5:
            units = [_line(i, u) for i, u in enumerate(units)]
        text = sep.join(units)
        terms = rng.sample(["abc", "klm", "浇筑", "塔吊", "zz", "qrs", "钢筋"], rng.randint(0, 3))
        cap = rng.choice([CAP, CAP, rng.randint(120, 1500)])
        keep_first = rng.random() < 0.3
        out = ask_tools._window(text, terms, cap, sep=sep, keep_first=keep_first)
        assert out == ask_tools._window(text, terms, cap, sep=sep, keep_first=keep_first)
        shown, first, mode = out
        assert len(shown) <= cap, (cap, len(shown))
        assert 0 <= first < len(units)
        if len(text) <= cap:
            assert (shown, mode) == (text, "whole")
            continue
        assert mode in ("window", "head")
        for u in _units(shown, sep):
            if u not in units:                       # a cut of one oversize unit
                piece = max(u.split("…"), key=len)
                assert any(piece in w for w in units), u[:60]
