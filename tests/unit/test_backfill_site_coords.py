import pytest

bf = pytest.importorskip("backfill_site_coords",
                         reason="requires urllib3 (installed in CI)")


CITIES = {
    "Auckland": {"formatted": "Auckland, New Zealand",
                 "lat": -36.8521, "lng": 174.7632},
    "Wanaka": {"formatted": "Wanaka, New Zealand",
               "lat": -44.6942, "lng": 169.1365},
}


def _fake_geocode(query, http=None, limit=5):
    if "Colombo" in query:
        return {"formatted": "13 Colombo Street, Christchurch",
                "lat": -43.5321, "lng": 172.6362}
    return CITIES.get(query)


def test_plan_skips_sites_with_existing_coords():
    sites = [{"id": "s1", "address": "13 Colombo Street",
              "latitude": -43.5, "longitude": 172.6}]
    assert bf.plan_coordinate_backfill(sites, geocode_fn=_fake_geocode) == []


def test_plan_skips_sites_with_nothing_to_geocode():
    sites = [{"id": "s2", "address": None, "location": None,
              "latitude": None, "longitude": None},
             {"id": "s3", "address": "", "location": "",
              "latitude": None, "longitude": None}]
    assert bf.plan_coordinate_backfill(sites, geocode_fn=_fake_geocode) == []


def test_plan_geocodes_address_and_emits_update():
    sites = [{"id": "s4", "address": "13 Colombo Street",
              "latitude": None, "longitude": None}]
    plan = bf.plan_coordinate_backfill(sites, geocode_fn=_fake_geocode)
    assert plan == [{"site_id": "s4", "address": "13 Colombo Street",
                     "query": "13 Colombo Street", "source": "address",
                     "latitude": -43.5321, "longitude": 172.6362,
                     "formatted": "13 Colombo Street, Christchurch"}]


def test_plan_skips_geocode_miss():
    sites = [{"id": "s5", "address": "an address OSM has never heard of",
              "latitude": None, "longitude": None}]
    assert bf.plan_coordinate_backfill(sites, geocode_fn=_fake_geocode) == []


# ---------------------------------------------------------------------------
# `location` fallback.
#
# Measured on production: of the eight active sites five had no coordinate,
# and only ONE of those five had an address at all. The address-only rule was
# not conservative, it was inert -- and the cost was not a blank panel but a
# wrong one, because the weather indicator fell back to a hardcoded
# Christchurch with no label on it.
# ---------------------------------------------------------------------------


def test_plan_falls_back_to_location_when_there_is_no_address():
    """The production majority: a city in `location`, no address at all."""
    sites = [{"id": "mangere", "address": None, "location": "Auckland",
              "latitude": None, "longitude": None}]
    plan = bf.plan_coordinate_backfill(sites, geocode_fn=_fake_geocode)
    assert plan == [{"site_id": "mangere", "address": "",
                     "query": "Auckland", "source": "location",
                     "latitude": -36.8521, "longitude": 174.7632,
                     "formatted": "Auckland, New Zealand"}]


def test_plan_falls_back_to_location_when_the_address_does_not_resolve():
    sites = [{"id": "northbrook", "address": "Lot 4, no street number",
              "location": "Wanaka", "latitude": None, "longitude": None}]
    plan = bf.plan_coordinate_backfill(sites, geocode_fn=_fake_geocode)
    assert len(plan) == 1
    assert plan[0]["source"] == "location"
    assert plan[0]["query"] == "Wanaka"
    assert (plan[0]["latitude"], plan[0]["longitude"]) == (-44.6942, 169.1365)


def test_address_wins_when_both_resolve():
    """A street address places a site to the metre and a city name to the
    city. Both are useful; only one of them is precise, so the order matters
    and is asserted rather than assumed."""
    sites = [{"id": "both", "address": "13 Colombo Street",
              "location": "Auckland", "latitude": None, "longitude": None}]
    plan = bf.plan_coordinate_backfill(sites, geocode_fn=_fake_geocode)
    assert plan[0]["source"] == "address"
    assert plan[0]["latitude"] == -43.5321


def test_the_geocoder_is_not_asked_twice_when_the_address_answers():
    asked = []

    def counting(query, http=None, limit=5):
        asked.append(query)
        return _fake_geocode(query)

    sites = [{"id": "both", "address": "13 Colombo Street",
              "location": "Auckland", "latitude": None, "longitude": None}]
    bf.plan_coordinate_backfill(sites, geocode_fn=counting)
    assert asked == ["13 Colombo Street"]


def test_a_site_with_neither_field_resolving_is_left_alone():
    """Not placed at a default. An un-placed site is a visible gap the panel
    now reports; a site placed at someone else's city is a silent lie."""
    sites = [{"id": "nowhere", "address": "unknown", "location": "unknown",
              "latitude": None, "longitude": None}]
    assert bf.plan_coordinate_backfill(sites, geocode_fn=_fake_geocode) == []


def test_a_longitude_of_zero_counts_as_having_coordinates():
    """`is not None`, never truthiness: 0 is a real meridian."""
    sites = [{"id": "null-island", "address": "13 Colombo Street",
              "location": "Auckland", "latitude": 0, "longitude": 0}]
    assert bf.plan_coordinate_backfill(sites, geocode_fn=_fake_geocode) == []
