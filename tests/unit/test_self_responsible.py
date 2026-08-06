"""Unit: resolving "Speaker" to the person who made the recording.

The transcript never states the wearer's name, so the model writes "Speaker".
That is an honest placeholder and a useless one: a task cannot be assigned to
"Speaker", and it is what a real recording produced —

    responsible=Speaker  | Design quote and program -- follow up with Emily

We know who it is, because the recording belongs to an account. The whole
question is when that knowledge may be applied.
"""
import pytest

iw = pytest.importorskip("lambda_item_writer")


def _items(*responsibles):
    return [{"action": "do a thing", "responsible": r} for r in responsibles]


# ---- who counts as "the speaker" ------------------------------------------

@pytest.mark.parametrize("token", ["Speaker", "speaker", "THE SPEAKER", "me",
                                   "Myself", "self", "I", "spk_0", "the recorder"])
def test_a_self_referential_responsible_becomes_the_recorder(token):
    items = _items(token)
    assert iw._resolve_self_responsible(items, "Ben Lin") == 1
    assert items[0]["responsible"] == "Ben Lin"


def test_a_real_name_is_never_overwritten():
    """Daniel was named by the speaker. Replacing that would reassign someone
    else's task to the person holding the recorder."""
    items = _items("Daniel")
    assert iw._resolve_self_responsible(items, "Ben Lin") == 0
    assert items[0]["responsible"] == "Daniel"


def test_an_empty_responsible_stays_empty():
    """Unassigned is information. Filling it in with whoever was recording
    invents an owner for a task nobody accepted."""
    items = _items("", None)
    assert iw._resolve_self_responsible(items, "Ben Lin") == 0
    assert items[0]["responsible"] == ""
    assert items[1]["responsible"] is None


def test_nothing_to_do_is_not_an_error():
    assert iw._resolve_self_responsible(None, "Ben Lin") == 0
    assert iw._resolve_self_responsible([], "Ben Lin") == 0
    assert iw._resolve_self_responsible(["not a dict"], "Ben Lin") == 0


# ---- the name it resolves to ----------------------------------------------

def test_the_name_is_first_plus_last():
    assert iw._display_name({"first_name": "Ben", "last_name": "Lin"}, "fallback") == "Ben Lin"


def test_a_missing_last_name_does_not_leave_a_trailing_space():
    """`first || ' ' || last` with a NULL last_name once produced "Ben_UCPK ",
    which became a folder that does not exist. Build it by filtering, not by
    concatenating."""
    assert iw._display_name({"first_name": "Ben", "last_name": None}, "fb") == "Ben"
    assert iw._display_name({"first_name": "Ben", "last_name": "  "}, "fb") == "Ben"


def test_a_nameless_row_falls_back_to_the_folder():
    assert iw._display_name({"first_name": None, "last_name": None}, "Ben_UCPK") == "Ben_UCPK"
    assert iw._display_name(None, "Ben_UCPK") == "Ben_UCPK"
