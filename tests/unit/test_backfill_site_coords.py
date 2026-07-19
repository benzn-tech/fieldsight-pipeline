import pytest

bf = pytest.importorskip("backfill_site_coords",
                         reason="requires urllib3 (installed in CI)")


def _fake_geocode(query, http=None, limit=5):
    if "Colombo" in query:
        return {"formatted": "13 Colombo Street, Christchurch",
                "lat": -43.5321, "lng": 172.6362}
    return None


def test_plan_skips_sites_with_existing_coords():
    sites = [{"id": "s1", "address": "13 Colombo Street",
              "latitude": -43.5, "longitude": 172.6}]
    assert bf.plan_coordinate_backfill(sites, geocode_fn=_fake_geocode) == []


def test_plan_skips_sites_without_address():
    sites = [{"id": "s2", "address": None, "latitude": None, "longitude": None},
             {"id": "s3", "address": "", "latitude": None, "longitude": None}]
    assert bf.plan_coordinate_backfill(sites, geocode_fn=_fake_geocode) == []


def test_plan_geocodes_address_and_emits_update():
    sites = [{"id": "s4", "address": "13 Colombo Street",
              "latitude": None, "longitude": None}]
    plan = bf.plan_coordinate_backfill(sites, geocode_fn=_fake_geocode)
    assert plan == [{"site_id": "s4", "address": "13 Colombo Street",
                     "latitude": -43.5321, "longitude": 172.6362,
                     "formatted": "13 Colombo Street, Christchurch"}]


def test_plan_skips_geocode_miss():
    sites = [{"id": "s5", "address": "an address OSM has never heard of",
              "latitude": None, "longitude": None}]
    assert bf.plan_coordinate_backfill(sites, geocode_fn=_fake_geocode) == []
