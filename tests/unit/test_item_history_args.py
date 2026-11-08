"""Unit: find_items rejects a malformed request before it touches the database.

`conn=None` throughout: every case here must raise in argument validation, so a call
that reached SQL would fail with AttributeError instead of the ValueError asserted.
"""
import pytest

from repositories.item_history import find_items

CO = "00000000-0000-0000-0000-00000000000c"
SITE = "00000000-0000-0000-0000-00000000000s"


def _call(**kw):
    args = {"company_id": CO, "site_id": SITE, "kinds": ("action_item",)}
    args.update(kw)
    return find_items(None, **args)


def test_empty_status_list_rejected():
    with pytest.raises(ValueError, match="empty"):
        _call(statuses={"action_item": []})


def test_status_filter_on_decision_rejected():
    with pytest.raises(ValueError, match="decision"):
        _call(kinds=("decision",), statuses={"decision": ["open"]})


def test_status_filter_on_finding_rejected():
    with pytest.raises(ValueError, match="finding"):
        _call(kinds=("finding",), statuses={"finding": ["open"]})


def test_unknown_kind_rejected():
    with pytest.raises(ValueError, match="unknown"):
        _call(kinds=("action_item", "safety"))


def test_empty_kinds_rejected():
    with pytest.raises(ValueError, match="kinds"):
        _call(kinds=())


def test_bare_string_kinds_rejected():
    """'finding' as a str would iterate to characters; say so instead of 'unknown kind f'."""
    with pytest.raises(ValueError, match="kinds"):
        _call(kinds="finding")


def test_unknown_status_kind_rejected():
    with pytest.raises(ValueError, match="unknown"):
        _call(statuses={"safety": ["open"]})


def test_bad_since_rejected():
    with pytest.raises(ValueError):
        _call(since="last tuesday")


def test_non_positive_limit_rejected():
    with pytest.raises(ValueError, match="limit"):
        _call(limit=0)


def test_missing_scope_rejected():
    with pytest.raises(ValueError, match="company_id"):
        _call(company_id=None)
    with pytest.raises(ValueError, match="site_id"):
        _call(site_id="")
