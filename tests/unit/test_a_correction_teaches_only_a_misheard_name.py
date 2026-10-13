"""Only a misheard name is learned from an edit (owner, 2026-10-02).

THE test is `one word for a name spelled like it is learned; a change of
meaning is not`.
"""
import pytest

from text_normalize import learned_pairs


@pytest.mark.parametrize("before,after,learned", [
    ("the Tikaha room inspections", "the TEKAHA room inspections", [("Tikaha", "TEKAHA")]),
    ("the Tikaha room", "the Te Kaha room", [("Tikaha", "Te Kaha")]),
    ("Mackon delivered the steel", "Macon delivered the steel", [("Mackon", "Macon")]),
    ("call Jak tomorrow", "call Jack tomorrow", [("Jak", "Jack")]),
])
def test_THE_one_word_for_a_name_spelled_like_it_is_learned(before, after, learned):
    assert learned_pairs(before, after) == learned


@pytest.mark.parametrize("before,after", [
    ("Meeting on Monday", "Meeting on Friday"),               # a different day, not a mishearing
    ("Ben spoke to Sam", "Ben spoke to the client about the pour"),
    ("levels done", "Levels done"),                           # case only
    ("the pour was late", "the pour was delayed"),            # not a name
    ("Level 2 slab", "Level 3 slab"),                         # digits are not names
    ("", "TEKAHA room"),
])
def test_a_change_of_meaning_is_not(before, after):
    assert learned_pairs(before, after) == []
