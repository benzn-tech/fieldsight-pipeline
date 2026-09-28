"""The daily report's weather is decided by code, kept per site and day, and
printed in the Word file.

WHAT CHANGED (owner, 2026-09-29: "the code decides, the model words"). The
nightly report used to append the day's totals to the prompt and ask the model
to "note the linkage" between weather and work, so whether 8 mm mattered, and
to which trade, was decided afresh every night. The Word file did not print the
weather at all -- a compliance record with no weather in it.

Now `build_weather_findings` runs weather_advice on the hourly actuals, the
report carries its lines, the Word file prints them under "Weather", and each
site's day is kept at weather/<site>/<date>/actual.json for later counting.

THE test is `the Word report prints what the code decided about the weather`.
"""
import io

import pytest

import lambda_report_generator as rg

SITE = {"latitude": -43.53, "longitude": 172.63}


def wet_afternoon(lat, lng, date, historical):
    rows = []
    for h in range(24):
        mm = 3.0 if h in (14, 15, 16) else 0.0
        rows.append({"hour": h, "prob": 100 if mm else 0, "mm": mm,
                     "gust_kmh": 15.0, "temp_c": 15.0})
    return rows


def test_findings_are_actuals_for_a_past_day_and_name_generic_trades():
    f = rg.build_weather_findings(SITE, "2026-09-23", "2026-09-29", fetch=wet_afternoon)
    assert f["actual"] is True and f["impact_basis"] == "general"
    assert f["lines"] == [
        "Rain in the afternoon (from 14:00 to 17:00, 9 mm). Impact: exterior painting, "
        "concrete pours. Advice: record any lost time against the weather."]
    assert f["weather_day"] is False


def test_no_coordinate_means_no_findings_not_a_guess():
    assert rg.build_weather_findings({}, "2026-09-23", "2026-09-29", fetch=wet_afternoon) is None


def test_a_failed_fetch_means_no_findings():
    def boom(*a):
        raise RuntimeError("network")
    assert rg.build_weather_findings(SITE, "2026-09-23", "2026-09-29", fetch=boom) is None
    assert rg.build_weather_findings(SITE, "2026-09-23", "2026-09-29", fetch=lambda *a: None) is None


def test_the_record_is_kept_per_site_and_day():
    assert rg.weather_record_key("site-1", "2026-09-23", True) == "weather/site-1/2026-09-23/actual.json"
    assert rg.weather_record_key("site-1", "2026-09-29", False) == "weather/site-1/2026-09-29/forecast.json"


def test_the_prompt_no_longer_asks_the_model_to_judge_the_weather():
    src = open(rg.__file__, encoding="utf-8").read()
    assert "Site Weather (for AI correlation)" not in src
    assert "weather.weather_prompt_block(" not in src


# ---- the Word file --------------------------------------------------------------

needs_docx = pytest.mark.skipif(not rg.DOCX_AVAILABLE, reason="python-docx is not installed here")


def _texts(report):
    from docx import Document
    buf = rg.generate_word_document(report, "Daily Report")
    d = Document(io.BytesIO(buf.getvalue()))
    return [(p.style.name, p.text) for p in d.paragraphs if p.text.strip()]


BASE = {"recording_session": {"date": "2026-09-23", "site": "UC PK", "worker": "Ben_UCPK2",
                              "recordings": 1}, "executive_summary": "A day."}


@needs_docx
def test_THE_the_word_report_prints_what_the_code_decided_about_the_weather():
    f = rg.build_weather_findings(SITE, "2026-09-23", "2026-09-29", fetch=wet_afternoon)
    daily = {"condition_label": "Slight rain", "temp_min_c": 9.1, "temp_max_c": 15.2,
             "precip_mm": 9.0, "windspeed_kmh": 22.0}
    texts = _texts({**BASE, "weather": daily, "weather_findings": f})
    i = texts.index(("Heading 1", "Weather"))
    assert texts[i + 1][1] == "Slight rain · 9.1–15.2°C · 9.0 mm rain · wind to 22.0 km/h"
    assert texts[i + 2] == ("List Bullet", f["lines"][0])


@needs_docx
def test_a_daily_report_without_weather_says_not_recorded():
    texts = _texts(dict(BASE))
    i = texts.index(("Heading 1", "Weather"))
    assert texts[i + 1][1] == "Not recorded for this report."


@needs_docx
def test_a_site_summary_without_weather_prints_no_weather_heading():
    texts = _texts({"recording_session": {"date": "2026-09-23", "site": "UC PK"},
                    "executive_summary": "A day."})
    assert ("Heading 1", "Weather") not in texts
