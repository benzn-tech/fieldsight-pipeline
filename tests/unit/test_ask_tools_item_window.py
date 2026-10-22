"""ask_tools item window: an item shows the part of a chunk that matches the query
(spec 2026-10-22-ask-tools-item-window, amended by its review)."""
import ask_tools


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
