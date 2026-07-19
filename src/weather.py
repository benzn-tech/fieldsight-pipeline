"""Open-Meteo weather (free, keyless) for the AI daily report.

NON-VPC ONLY (BUG-36). Same provider the UI weather indicator already uses:
archive API for past dates, forecast API for today/future. Normalizes to a
single block cached once per (site, date) on the report record. WMO 4677 codes
map to labels (mirrors the UI's WMO_WEATHER_CODES in
fieldsight-ui/scripts/app-shell.js).
"""
import json

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
_DAILY_VARS = "temperature_2m_max,temperature_2m_min,weathercode,windspeed_10m_max,precipitation_sum"

WMO_LABELS = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    56: "Light freezing drizzle", 57: "Dense freezing drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Slight showers", 81: "Moderate showers", 82: "Violent showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm, slight hail", 99: "Thunderstorm, heavy hail",
}


def _first(daily, key):
    seq = daily.get(key)
    return seq[0] if isinstance(seq, list) and seq else None


def normalize_weather(data, date):
    """Pure. Reads Open-Meteo `daily` arrays (index 0) into a normalized
    block, or None if there is no daily row for the requested date."""
    daily = (data or {}).get("daily") or {}
    times = daily.get("time") or []
    if not times:
        return None
    code = _first(daily, "weathercode")
    code = int(code) if code is not None else None
    return {
        "date": date,
        "temp_max_c": _first(daily, "temperature_2m_max"),
        "temp_min_c": _first(daily, "temperature_2m_min"),
        "weathercode": code,
        "condition_label": WMO_LABELS.get(code, "Unknown"),
        "windspeed_kmh": _first(daily, "windspeed_10m_max"),
        "precip_mm": _first(daily, "precipitation_sum"),
        "source": "open-meteo",
    }


def weather_prompt_block(weather):
    """Pure. Factual weather sentence + correlation guardrail injected into
    the Claude report prompt -- ground any observation linkage in the actual
    conditions, never invent impacts the transcript doesn't support."""
    if not weather:
        return ""
    return (
        f"Site weather for {weather['date']} was: {weather['condition_label']}, "
        f"{weather['temp_min_c']}–{weather['temp_max_c']}°C, wind up to "
        f"{weather['windspeed_kmh']} km/h, precipitation {weather['precip_mm']} mm. "
        "Where an observation plausibly relates to weather (rain → "
        "concrete/paint/earthworks delays; high wind → crane/height work; "
        "heat/cold → pours/curing), note the linkage explicitly; do not "
        "invent impacts the transcript doesn't support."
    )


def fetch_weather(lat, lng, date, today_iso, http=None):
    """One Open-Meteo call for (lat, lng, date). Archive API when
    date < today_iso (BUG-19: plain string compare, both 'YYYY-MM-DD' --
    never new Date()-parse), else forecast API. Returns a normalized block
    or None on any failure (non-200, network error, missing daily row).

    `http` is injectable (a urllib3.PoolManager-like object with a
    `.request(method, url, timeout=...) -> resp` method, where `resp` has
    `.status` and `.data`) so tests can pass a fake double -- no real
    network calls happen in unit tests. Defaults to a real
    urllib3.PoolManager, matching geocode.py / claude_utils.py. NON-VPC
    only (BUG-36).
    """
    if lat is None or lng is None:
        return None
    if http is None:
        import urllib3
        http = urllib3.PoolManager()
    historical = bool(today_iso and date < today_iso)
    if historical:
        url = (f"{ARCHIVE_URL}?latitude={lat}&longitude={lng}"
               f"&start_date={date}&end_date={date}"
               f"&daily={_DAILY_VARS}&timezone=Pacific/Auckland")
    else:
        url = (f"{FORECAST_URL}?latitude={lat}&longitude={lng}"
               f"&start_date={date}&end_date={date}"
               f"&daily={_DAILY_VARS}&current_weather=true&timezone=Pacific/Auckland")
    try:
        resp = http.request("GET", url, timeout=10.0)
        if resp.status != 200:
            return None
        return normalize_weather(json.loads(resp.data.decode("utf-8")), date)
    except Exception:
        return None
