"""The shared candidate rule (status_candidates): one place decides whether a re-read item may
still be offered for a status -- used by the suggestion writer and the pick-card routes."""
import status_candidates as sc

SITE = "6f1c2b3a-0d4e-4f5a-8b6c-7d8e9f0a1b2c"
OTHER = "0a1b2c3d-4e5f-4a6b-8c7d-8e9f0a1b2c3d"


def test_canon_uuid_forms():
    assert sc.canon_uuid(SITE.upper()) == SITE
    assert sc.canon_uuid("{" + SITE + "}") == SITE
    assert sc.canon_uuid(SITE.replace("-", "")) == SITE
    assert sc.canon_uuid(None) is None
    assert sc.canon_uuid("x") is None
    assert sc.canon_uuid(123) is None


def test_terminal_set():
    assert sc.TERMINAL == frozenset({"done", "closed"})


def test_drop_reason_order():
    assert sc.drop_reason(None, SITE, "done") == "gone"
    # Site is checked before status: a moved item is "moved_site" even when also terminal.
    assert sc.drop_reason({"site_id": OTHER, "status": "done"}, SITE, "done") == "moved_site"
    # A malformed event site (canonicalised to None) never matches.
    assert sc.drop_reason({"site_id": SITE, "status": "open"}, None, "done") == "moved_site"
    assert sc.drop_reason({"site_id": SITE.upper(), "status": "done"}, SITE, "blocked") == "terminal"
    assert sc.drop_reason({"site_id": SITE, "status": "closed"}, SITE, "done") == "terminal"
    assert sc.drop_reason({"site_id": SITE, "status": "blocked"}, SITE, "blocked") == "same"
    assert sc.drop_reason({"site_id": SITE, "status": "open"}, SITE, "done") is None
