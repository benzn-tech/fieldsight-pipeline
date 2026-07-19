import json

import pytest

geocode = pytest.importorskip("geocode", reason="requires urllib3 (installed in CI)")


class FakeResp:
    def __init__(self, status, payload):
        self.status = status
        self.data = json.dumps(payload).encode("utf-8")


class FakeHTTP:
    def __init__(self, resp):
        self._resp = resp
        self.last_url = None

    def request(self, method, url, **kw):
        self.last_url = url
        return self._resp


_PHOTON = {
    "type": "FeatureCollection",
    "features": [
        {
            "geometry": {"type": "Point", "coordinates": [172.6362, -43.5321]},
            "properties": {"name": "13 Colombo Street", "housenumber": "13",
                           "street": "Colombo Street", "city": "Christchurch",
                           "postcode": "8011", "countrycode": "NZ"},
        }
    ],
}


def test_parse_photon_features_extracts_latlng_and_formatted():
    feats = geocode.parse_photon_features(_PHOTON)
    assert len(feats) == 1
    assert feats[0]["lat"] == -43.5321
    assert feats[0]["lng"] == 172.6362
    assert "Colombo" in feats[0]["formatted"]


def test_parse_photon_empty_features_returns_empty():
    assert geocode.parse_photon_features({"features": []}) == []
    assert geocode.parse_photon_features({}) == []


def test_geocode_returns_best_feature():
    http = FakeHTTP(FakeResp(200, _PHOTON))
    res = geocode.geocode("13 Colombo Street Christchurch", http=http)
    assert res["lat"] == -43.5321 and res["lng"] == 172.6362
    assert "photon.komoot.io/api" in http.last_url
    assert "q=13" in http.last_url  # query url-encoded


def test_geocode_no_results_returns_none():
    http = FakeHTTP(FakeResp(200, {"features": []}))
    assert geocode.geocode("nowhere at all", http=http) is None


def test_geocode_http_error_returns_none():
    http = FakeHTTP(FakeResp(503, {}))
    assert geocode.geocode("anything", http=http) is None
