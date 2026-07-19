import json

import pytest

weather = pytest.importorskip("weather", reason="requires urllib3 (installed in CI)")


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


_DAILY = {
    "daily": {
        "time": ["2026-07-18"],
        "temperature_2m_max": [12.4],
        "temperature_2m_min": [3.1],
        "weathercode": [61],
        "windspeed_10m_max": [28.0],
        "precipitation_sum": [5.2],
    }
}


def test_normalize_weather_from_daily_block():
    block = weather.normalize_weather(_DAILY, "2026-07-18")
    assert block == {
        "date": "2026-07-18",
        "temp_max_c": 12.4,
        "temp_min_c": 3.1,
        "weathercode": 61,
        "condition_label": "Slight rain",
        "windspeed_kmh": 28.0,
        "precip_mm": 5.2,
        "source": "open-meteo",
    }


def test_normalize_weather_no_daily_returns_none():
    assert weather.normalize_weather({}, "2026-07-18") is None
    assert weather.normalize_weather({"daily": {"time": []}}, "2026-07-18") is None


def test_weather_prompt_block_states_conditions_and_guardrail():
    block = weather.normalize_weather(_DAILY, "2026-07-18")
    text = weather.weather_prompt_block(block)
    assert "Slight rain" in text
    assert "2026-07-18" in text
    # correlation guardrail must be present (grounded, not fabricated)
    assert "do not invent" in text.lower()


def test_fetch_weather_uses_archive_for_historical():
    http = FakeHTTP(FakeResp(200, _DAILY))
    block = weather.fetch_weather(-43.5321, 172.6362, "2026-07-18",
                                  "2026-07-19", http=http)
    assert block["temp_max_c"] == 12.4
    assert "archive-api.open-meteo.com/v1/archive" in http.last_url
    assert "start_date=2026-07-18" in http.last_url


def test_fetch_weather_uses_forecast_for_today():
    today = {
        "current_weather": {"temperature": 9.0, "weathercode": 3, "windspeed": 11.0},
        "daily": {
            "time": ["2026-07-19"],
            "temperature_2m_max": [10.0],
            "temperature_2m_min": [4.0],
            "weathercode": [3],
            "windspeed_10m_max": [15.0],
            "precipitation_sum": [0.0],
        },
    }
    http = FakeHTTP(FakeResp(200, today))
    block = weather.fetch_weather(-43.5321, 172.6362, "2026-07-19",
                                  "2026-07-19", http=http)
    assert block["weathercode"] == 3
    assert "api.open-meteo.com/v1/forecast" in http.last_url
    assert "current_weather=true" in http.last_url


def test_fetch_weather_http_error_returns_none():
    http = FakeHTTP(FakeResp(500, {}))
    assert weather.fetch_weather(-43.5, 172.6, "2026-07-18", "2026-07-19",
                                 http=http) is None
