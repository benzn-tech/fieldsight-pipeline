"""GET /api/org/weather hands the page one site's day of weather, as decided.

The page decides nothing: `lines` were written by weather_advice in the report
generator. This route only finds the two records and enforces the site ACL
(the same resolver the programme route uses)."""
import lambda_org_api as api


def _call(monkeypatch, params, docs=None, resolved=("u-1", None)):
    asked = []
    monkeypatch.setattr(api, "_resolve_site_param", lambda conn, caller, p: resolved)
    monkeypatch.setattr(api, "_get_lake_json", lambda key: asked.append(key) or (docs or {}).get(key))
    out = api.get_site_weather(None, {"company_id": "c"}, {"queryStringParameters": params})
    return out, asked


def test_it_returns_the_forecast_and_the_actual_for_the_day(monkeypatch):
    docs = {"weather/u-1/2026-09-29/forecast.json": {"lines": ["Rain very likely ..."]}}
    out, asked = _call(monkeypatch, {"site": "u-1", "date": "2026-09-29"}, docs)
    assert out["statusCode"] == 200
    body = __import__("json").loads(out["body"])
    assert body["forecast"] == {"lines": ["Rain very likely ..."]}
    assert body["actual"] is None, "not written yet reads as null"
    assert asked == ["weather/u-1/2026-09-29/forecast.json", "weather/u-1/2026-09-29/actual.json"]


def test_the_site_acl_answers_before_anything_is_read(monkeypatch):
    denied = api.error("access denied to this site", 403)
    out, asked = _call(monkeypatch, {"site": "u-9", "date": "2026-09-29"}, resolved=(None, denied))
    assert out["statusCode"] == 403 and asked == []


def test_a_malformed_date_is_refused(monkeypatch):
    out, asked = _call(monkeypatch, {"site": "u-1", "date": "29/09/2026"})
    assert out["statusCode"] == 400 and asked == []


def test_the_route_is_dispatched():
    src = open(api.__file__, encoding="utf-8").read()
    assert 'if route == "/weather" and method == "GET":' in src
