"""The weather feature was never missing. It was starved.

`lambda_report_generator` fetches weather, stores it on the report and feeds it
to the prompt with a correlation instruction. Not one production report carries
a weather block, because `build_weather_block_for_site` takes the coordinate
from `config/user_mapping.json`, where all five sites have null lat/lng --
while the coordinates people enter in the web UI live in the Aurora `sites`
table, which that Lambda cannot read (no VpcConfig, deliberately: it needs
egress for the model and the weather API).

org-api publishes them to a small machine-owned S3 object instead. These tests
cover the merge rules and, more importantly, the overlay: the seam where the
published document has to actually reach the generator's site lookup.
"""
import json

import pytest

sc = pytest.importorskip("site_coords")


UC_PK = {
    "id": "6508b661-266c-426b-968c-b075a9ee1f7c",
    "slug": "uc-pk", "name": "UC PK",
    "latitude": -43.5227319, "longitude": 172.5852876,
}
MANGERE = {
    "id": "aaaaaaaa-0000-0000-0000-000000000001",
    "slug": "mangere-wasterwater-treatment-plan",
    "name": "MANGERE WASTEWATER TREATMENT",
    "latitude": None, "longitude": None,
}


# ---------------------------------------------------------------- entries


def test_a_site_with_coordinates_becomes_an_entry():
    assert sc.entry_for_site(UC_PK) == {
        "site_id": "6508b661-266c-426b-968c-b075a9ee1f7c",
        "name": "UC PK",
        "latitude": -43.5227319, "longitude": 172.5852876,
    }


def test_a_site_without_coordinates_is_absent_not_null():
    """An entry full of nulls says "cannot place this site" in a form every
    reader has to remember to check. Absence says it once."""
    assert sc.entry_for_site(MANGERE) is None


def test_a_longitude_of_zero_is_a_coordinate():
    site = dict(UC_PK, latitude=0, longitude=0)
    assert sc.entry_for_site(site)["longitude"] == 0


def test_a_site_with_no_slug_is_keyed_by_its_id():
    """org-api-created sites had NULL slug until BUG-39/WS4. A coordinate
    published under a key nothing looks up is the same as not publishing it,
    only harder to notice."""
    site = dict(UC_PK); site.pop("slug")
    assert sc.slug_for_site(site) == UC_PK["id"]


# ---------------------------------------------------------------- merging


def test_publishing_one_site_leaves_the_others_alone():
    doc = {"sb1108-ellesmere": {"site_id": "x", "name": "SB1108",
                                "latitude": -43.6, "longitude": 172.2}}
    out = sc.merge_site(doc, UC_PK)
    assert set(out) == {"sb1108-ellesmere", "uc-pk"}
    assert out["sb1108-ellesmere"] == doc["sb1108-ellesmere"]


def test_clearing_a_coordinate_removes_the_entry():
    """A coordinate deleted in the UI has to stop producing weather, or the
    report keeps quoting a place the customer has told us is wrong."""
    doc = sc.merge_site({}, UC_PK)
    cleared = dict(UC_PK, latitude=None, longitude=None)
    assert sc.merge_site(doc, cleared) == {}


def test_merge_does_not_mutate_the_document_it_was_given():
    doc = {}
    sc.merge_site(doc, UC_PK)
    assert doc == {}


def test_a_site_that_cannot_be_keyed_changes_nothing():
    doc = {"uc-pk": {"latitude": 1, "longitude": 2}}
    assert sc.merge_site(doc, {"latitude": 1, "longitude": 2}) == doc


# ---------------------------------------------------------------- the seam


def test_the_overlay_fills_the_nulls_that_starved_the_feature():
    """This is the whole point. Production `sites` block, verbatim in shape."""
    sites_info = {
        "uc-pk": {"name": "UC PK", "latitude": None, "longitude": None},
        "sb1131-northbrook-wanaka": {"name": "SB1131 - Northbrook Wanaka",
                                     "latitude": None, "longitude": None},
    }
    doc = sc.merge_site({}, UC_PK)
    out = sc.overlay_sites_info(sites_info, doc)

    assert out["uc-pk"]["latitude"] == -43.5227319
    assert out["uc-pk"]["coord_source"] == "site-coords"
    # Northbrook has no coordinate anywhere; it must stay unplaced rather than
    # inherit somebody else's.
    assert out["sb1131-northbrook-wanaka"]["latitude"] is None


def test_a_hand_written_coordinate_wins():
    """user_mapping.json is maintained by people. A value written there is a
    human overriding what the UI holds; replacing it silently would make the
    file's contents a lie."""
    sites_info = {"uc-pk": {"name": "UC PK", "latitude": -1.0, "longitude": 2.0}}
    out = sc.overlay_sites_info(sites_info, sc.merge_site({}, UC_PK))
    assert out["uc-pk"]["latitude"] == -1.0
    assert "coord_source" not in out["uc-pk"]


def test_a_site_missing_from_user_mapping_still_gets_weather():
    """The generator looks up by primary_site slug; a slug absent from the
    hand-maintained `sites` block returns {} and no weather. Aurora is the
    fuller list -- eight sites against five."""
    out = sc.overlay_sites_info({}, sc.merge_site({}, UC_PK))
    assert out["uc-pk"]["latitude"] == -43.5227319


def test_the_overlay_never_invents_a_site_without_a_coordinate():
    out = sc.overlay_sites_info({}, {"ghost": {"name": "Ghost"}})
    assert out == {}


def test_a_malformed_document_does_not_stop_a_report():
    """This sits in front of an already-optional weather fetch. A config object
    with an unexpected shape must not take the day's report with it."""
    for junk in (None, [], "nope", {"uc-pk": "not a dict"}, {"uc-pk": None}):
        assert sc.coords_for_slug(junk, "uc-pk") == (None, None)
    assert sc.overlay_sites_info({"uc-pk": {"latitude": None}}, "nope") == {
        "uc-pk": {"latitude": None}}


def test_the_published_document_is_json_serialisable():
    """It is written with json.dumps in a request handler; a value that cannot
    be encoded would turn a successful site save into a logged exception."""
    doc = sc.merge_site({}, UC_PK)
    assert json.loads(json.dumps(doc)) == doc


def test_the_key_is_a_single_object_not_a_prefix():
    """The IAM grant names this one key precisely so a bug here cannot reach
    config/user_mapping.json, which is hand-maintained and read by six
    lambdas. If this constant grows a prefix, that argument stops holding."""
    assert sc.KEY == "config/site-coords.json"
    assert "*" not in sc.KEY
