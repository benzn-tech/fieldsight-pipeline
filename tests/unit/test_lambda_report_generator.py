"""Unit tests for the weather seam in lambda_report_generator.

Dummy AWS/Anthropic env vars so the module's eager boto3 client + config
reads don't blow up at import (mirrors tests/unit/test_lambda_extract_session.py).
No test here makes a real AWS, Claude, or Open-Meteo call.
"""
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")

rg = pytest.importorskip("lambda_report_generator",
                         reason="requires boto3/urllib3 (installed in CI)")


def test_build_weather_block_returns_none_without_coords():
    called = []

    def fake_fetch(lat, lng, date, today_iso, http=None):
        called.append((lat, lng))
        return {"date": date, "source": "open-meteo"}

    # site_info with no latitude/longitude -> no fetch, None returned
    assert rg.build_weather_block_for_site({"name": "Depot"}, "2026-07-18",
                                           "2026-07-19", fetch=fake_fetch) is None
    assert called == []


def test_build_weather_block_calls_fetch_with_site_coords():
    seen = {}

    def fake_fetch(lat, lng, date, today_iso, http=None):
        seen.update(lat=lat, lng=lng, date=date, today=today_iso)
        return {"date": date, "condition_label": "Slight rain",
                "source": "open-meteo"}

    site_info = {"name": "Depot", "latitude": -43.5321, "longitude": 172.6362}
    block = rg.build_weather_block_for_site(site_info, "2026-07-18",
                                            "2026-07-19", fetch=fake_fetch)
    assert seen == {"lat": -43.5321, "lng": 172.6362,
                    "date": "2026-07-18", "today": "2026-07-19"}
    assert block["condition_label"] == "Slight rain"


def test_build_weather_block_swallows_fetch_error():
    def boom(*a, **k):
        raise RuntimeError("open-meteo down")

    site_info = {"latitude": -43.5, "longitude": 172.6}
    assert rg.build_weather_block_for_site(site_info, "2026-07-18",
                                           "2026-07-19", fetch=boom) is None
