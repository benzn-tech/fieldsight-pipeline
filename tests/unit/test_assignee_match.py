"""The server's "is this task mine?" test must agree with the client's.

`action_items.responsible` is free text the extraction model transcribed from
speech; there is no responsible_user_id FK. The client (fieldsight-ui
scripts/api/mine-team.js) buckets a task as Mine on three rules, and the server
must accept writes on exactly the same three -- if the server is stricter, the
UI shows a task as yours with a live checkbox and the write then 403s, i.e. the
interface lies to the user.

Rule 3 (unassigned + I recorded the topic) grants authority that strict string
equality never did, so the tests below pin its scope hard: it must key on the
TOPIC's user_id, never the site, never the caller's name, and it must fail
closed when the owner is unknown.
"""

import pytest

import lambda_org_api as api


def _caller(first="Ben", last="Lin", uid="u-1", folder=None):
    return {
        "id": uid,
        "first_name": first,
        "last_name": last,
        "folder_name": folder,
        "global_role": "worker",
        "company_id": "c-1",
    }


def _row(responsible=None, topic_user_id=None):
    return {"responsible": responsible, "topic_user_id": topic_user_id}


# ---------------------------------------------------------------- rule 1


@pytest.mark.parametrize(
    "responsible",
    ["Ben Lin", "ben lin", "BEN LIN", "  Ben Lin  ", "Ben  Lin"],
)
def test_normalized_name_variants_are_the_same_person(responsible):
    assert api._is_assignee(_row(responsible), _caller()) is True


def test_a_different_person_is_not_me():
    assert api._is_assignee(_row("Jarley Trainor"), _caller()) is False


def test_first_name_alone_does_not_match():
    """"Ben" would match Ben Lin AND Ben Carter on the same site -- refusing it
    is deliberate, and mirrors the client's explicit no-fuzzy-match rule."""
    assert api._is_assignee(_row("Ben"), _caller()) is False


# ---------------------------------------------------------------- rule 2


def test_folder_form_matches_the_display_name():
    assert api._is_assignee(_row("Ben_Lin"), _caller()) is True


def test_folder_form_matches_the_callers_folder_name():
    caller = _caller(first="Ben_UCPK", last="", folder="Ben_UCPK")
    assert api._is_assignee(_row("Ben_UCPK"), caller) is True


def test_folder_match_does_not_fire_on_an_empty_folder_name():
    """A caller with no folder must not match a row whose responsible is also
    falsy-ish through the folder branch."""
    caller = _caller(first="", last="", folder=None)
    assert api._is_assignee(_row("   "), caller) is False


# ---------------------------------------------------------------- rule 3


@pytest.mark.parametrize("responsible", [None, "", "   "])
def test_unassigned_task_on_my_own_topic_is_mine(responsible):
    row = _row(responsible, topic_user_id="u-1")
    assert api._is_assignee(row, _caller(uid="u-1")) is True


@pytest.mark.parametrize("responsible", [None, "", "   "])
def test_unassigned_task_on_SOMEONE_ELSES_topic_is_not_mine(responsible):
    """The blast-radius guarantee for rule 3. Being able to see a task, or
    sharing a site with its author, must never be enough."""
    row = _row(responsible, topic_user_id="u-2")
    assert api._is_assignee(row, _caller(uid="u-1")) is False


def test_unassigned_task_with_unknown_owner_fails_closed():
    assert api._is_assignee(_row(None, topic_user_id=None), _caller()) is False


def test_unassigned_rule_does_not_fire_for_a_caller_without_an_id():
    row = _row(None, topic_user_id="u-1")
    caller = _caller(uid=None)
    assert api._is_assignee(row, caller) is False


def test_owner_match_is_string_compared_so_uuid_objects_work():
    import uuid

    oid = uuid.uuid4()
    row = _row(None, topic_user_id=oid)
    assert api._is_assignee(row, _caller(uid=str(oid))) is True


# ---------------------------------------------------------------- scope


def test_an_assigned_task_never_falls_through_to_the_owner_rule():
    """A task assigned to someone else stays theirs even on my own topic --
    otherwise recording a session would hand me everyone's tasks in it."""
    row = _row("Jarley Trainor", topic_user_id="u-1")
    assert api._is_assignee(row, _caller(uid="u-1")) is False


def test_nameless_caller_cannot_claim_an_assigned_task():
    caller = _caller(first="", last="", uid="u-1")
    assert api._is_assignee(_row("Ben Lin"), caller) is False


def test_get_action_item_selects_the_topic_owner():
    """Rule 3 reads row['topic_user_id']; if the repo query stops selecting it
    the rule degrades to always-False and nobody notices. Lock the column in."""
    import inspect

    import repositories.action_items as repo

    src = inspect.getsource(repo.get_action_item)
    assert "topic_user_id" in src, "get_action_item must expose the topic owner"
    assert "LEFT JOIN topics" in src, "topic owner comes from a LEFT JOIN on topics"
