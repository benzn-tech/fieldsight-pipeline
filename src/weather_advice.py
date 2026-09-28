"""Weather advice for site work: the code decides, the model only words it.

WHY THIS MODULE EXISTS. The nightly report hands the model one sentence of
daily totals and asks it to "note the linkage" between weather and work. That
puts every judgement in the model: whether 8 mm matters, which trade it hits,
what time to finish by. Each is a threshold or a clock sum, and a model asked
to do them does them differently every day -- which is also how a report came
to say "no site construction progress" on a day whose one progress item was
recorded (2026-09-23, Ben_UCPK2): the model decided where things go.

The split this module holds to (owner, 2026-09-28):

  code   -- fetch the hourly forecast/actuals, apply thresholds, find the
            conflict with the day's planned work, decide a weather day,
            compute the finish-by and reminder times;
  model  -- turn the finished structure into a short natural sentence;
  code   -- check every number and time in that sentence against the
            structure, and fall back to the fixed template if any differ.

EXPRESSION RULES (owner):
  * each item is three parts, in order: fact -> impact -> advice;
  * the conclusion comes first and the numbers follow in brackets;
  * probability always uses the same words (PROBABILITY_WORDS), never a
    phrase the model picks that day;
  * weather with no impact is not written -- or one line, NO_IMPACT_LINE.

Everything here is pure (no network, no clock) except `hourly_forecast`,
which takes an injectable `http`, so the rules are testable to the minute.
"""
import json
import re

# ---------------------------------------------------------------------------
# Fixed wording
# ---------------------------------------------------------------------------

# One phrase per band, used every day. Below the last band a chance is not
# reported at all: "a 20% chance" is noise a site manager learns to skip, and
# once he skips one line he skips the next.
PROBABILITY_WORDS = (
    (90, "almost certain"),
    (70, "very likely"),
    (50, "likely"),
    (30, "possible"),
)
MIN_REPORTED_PROBABILITY = PROBABILITY_WORDS[-1][0]

NO_IMPACT_LINE = "Weather: no impact on site work expected."


def probability_word(pct):
    """The fixed phrase for a probability, or None below the reporting floor."""
    if pct is None:
        return None
    for floor, word in PROBABILITY_WORDS:
        if pct >= floor:
            return word
    return None


def part_of_day(hour):
    if hour < 12:
        return "this morning"
    if hour < 17:
        return "this afternoon"
    return "this evening"


# ---------------------------------------------------------------------------
# Thresholds -- the judgements, in one place
# ---------------------------------------------------------------------------

WORK_START_HOUR = 7
WORK_END_HOUR = 17

RAIN_HOUR_MM = 0.2          # an hour counts as wet at or above this
RAIN_REPORT_MM = 1.0        # a spell is reported at or above this total...
RAIN_REPORT_PROB = 50       # ...or at or above this peak probability
WIND_GUST_KMH = 40          # gusts that affect lifting and work at height
WIND_STOP_KMH = 60          # gusts at which lifting stops
HEAT_C = 28
COLD_C = 3

FINISH_BUFFER_HOURS = 2     # finish sensitive work this long before onset
REMIND_BEFORE_MINUTES = 60  # remind this long before the finish-by time

# A lost ("weather") day, judged on ACTUALS, not a forecast.
WEATHER_DAY_RAIN_MM = 10.0
WEATHER_DAY_WET_HOURS = 5    # half the 07:00-17:00 day; three wet hours is not a lost day
WEATHER_DAY_GUST_HOURS = 3

# What to name when there is no plan to match against: two distinct trades,
# not two spellings of one ("crane lift, lifting" read as a stammer).
GENERAL_IMPACT = {
    "rain": ["exterior painting", "concrete pours"],
    "wind": ["crane lifts", "work at height"],
    "heat": ["concrete pours"],
    "cold": ["concrete pours", "coatings"],
}

# Which work each kind of weather hits. Matched against the day's planned
# work (programme tasks, open items) to find a conflict; used as-is only when
# there is no plan to match against.
SENSITIVE_WORK = {
    "rain": ("exterior painting", "coating", "concrete pour", "earthworks",
             "roofing", "membrane", "waterproofing", "blocklaying", "bricklaying"),
    "wind": ("crane lift", "lifting", "work at height", "scaffold", "roofing",
             "cladding"),
    "heat": ("concrete pour", "asphalt"),
    "cold": ("concrete pour", "curing", "coating", "sealant"),
}


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
_HOURLY_FORECAST = "precipitation_probability,precipitation,wind_gusts_10m,temperature_2m"
_HOURLY_ARCHIVE = "precipitation,wind_gusts_10m,temperature_2m"


def hourly_forecast(lat, lng, date, historical, http=None):
    """[{hour, prob, mm, gust_kmh, temp_c}] for one local day, or None.

    Archive (actuals) for a past date carries no probability -- the rain
    either fell or it did not -- so `prob` is 100 for a wet hour and 0 for a
    dry one there. `http` is injectable like weather.fetch_weather's."""
    if lat is None or lng is None:
        return None
    if http is None:
        import urllib3
        http = urllib3.PoolManager()
    base, fields = (ARCHIVE_URL, _HOURLY_ARCHIVE) if historical else (FORECAST_URL, _HOURLY_FORECAST)
    url = (f"{base}?latitude={lat}&longitude={lng}&start_date={date}&end_date={date}"
           f"&hourly={fields}&timezone=Pacific/Auckland")
    try:
        resp = http.request("GET", url, timeout=10.0)
        if resp.status != 200:
            return None
        return normalize_hourly(json.loads(resp.data.decode("utf-8")), historical)
    except Exception:
        return None


def normalize_hourly(data, historical):
    h = (data or {}).get("hourly") or {}
    times = h.get("time") or []
    out = []
    for i, t in enumerate(times):
        def at(key):
            seq = h.get(key) or []
            return seq[i] if i < len(seq) else None
        mm = at("precipitation")
        prob = at("precipitation_probability")
        if historical:
            prob = 100 if (mm or 0) >= RAIN_HOUR_MM else 0
        out.append({"hour": int(t[11:13]), "prob": prob, "mm": mm,
                    "gust_kmh": at("wind_gusts_10m"), "temp_c": at("temperature_2m")})
    return out


# ---------------------------------------------------------------------------
# Judgement
# ---------------------------------------------------------------------------

def _working(hours, start, end):
    return [h for h in hours if start <= h["hour"] < end]


def _spells(hours, wet):
    """Contiguous runs of hours for which `wet(h)` holds."""
    runs, cur = [], []
    for h in hours:
        if wet(h):
            if cur and h["hour"] != cur[-1]["hour"] + 1:
                runs.append(cur)
                cur = []
            cur.append(h)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return runs


def _conflicts(kind, planned):
    """(impacted work, basis). With a plan: the planned items this weather hits,
    and an empty list means no impact. Without one: the general list."""
    words = SENSITIVE_WORK[kind]
    if planned is None:
        return list(GENERAL_IMPACT[kind]), "general"
    hit = [p for p in planned if any(w in (p or "").lower() for w in words)]
    return hit, "planned"


def _clock(hour):
    return "%02d:00" % hour


def _minus_minutes(hhmm, minutes):
    h, m = int(hhmm[:2]), int(hhmm[3:])
    total = max(0, h * 60 + m - minutes)
    return "%02d:%02d" % (total // 60, total % 60)


def assess(hours, planned=None, work_start=WORK_START_HOUR, work_end=WORK_END_HOUR,
           actual=False):
    """The day's weather findings, decided here and nowhere else.

    `hours` is normalize_hourly's list. `planned` is the day's planned work as
    short strings, or None when there is no plan to check against.

    `actual` is True for a day that has happened (archive data). Its items
    carry no probability and no advice times -- advising a finish-by time for
    yesterday is noise -- and `weather_day` is a finding rather than a risk.

    Returns {"items": [...], "weather_day": bool, "no_impact": bool,
    "actual": bool}. Each item:
      kind, onset, end, prob, prob_word, mm, gust_kmh, temp_c, period,
      impacts, impact_basis, finish_by, remind_at, advice_kind
    """
    work = _working(hours or [], work_start, work_end)
    items = []

    for spell in _spells(work, lambda h: (h["mm"] or 0) >= RAIN_HOUR_MM
                         and (h["prob"] or 0) >= MIN_REPORTED_PROBABILITY):
        total = round(sum(h["mm"] or 0 for h in spell), 1)
        peak = max(h["prob"] or 0 for h in spell)
        if total < RAIN_REPORT_MM and peak < RAIN_REPORT_PROB:
            continue
        impacts, basis = _conflicts("rain", planned)
        if not impacts:
            continue
        onset = spell[0]["hour"]
        items.append(_item("rain", onset, spell[-1]["hour"] + 1, peak, total,
                           None, None, impacts, basis, work_start))

    for spell in _spells(work, lambda h: (h["gust_kmh"] or 0) >= WIND_GUST_KMH):
        impacts, basis = _conflicts("wind", planned)
        if not impacts:
            continue
        gust = round(max(h["gust_kmh"] or 0 for h in spell))
        items.append(_item("wind", spell[0]["hour"], spell[-1]["hour"] + 1, None, None,
                           gust, None, impacts, basis, work_start))

    temps = [h["temp_c"] for h in work if h["temp_c"] is not None]
    if temps and max(temps) >= HEAT_C:
        impacts, basis = _conflicts("heat", planned)
        if impacts:
            peak_h = max(work, key=lambda h: h["temp_c"] if h["temp_c"] is not None else -99)
            items.append(_item("heat", peak_h["hour"], peak_h["hour"] + 1, None, None,
                               None, round(max(temps)), impacts, basis, work_start))
    if temps and min(temps) <= COLD_C:
        impacts, basis = _conflicts("cold", planned)
        if impacts:
            low_h = min(work, key=lambda h: h["temp_c"] if h["temp_c"] is not None else 99)
            # "Until" is when it is warm enough again, not the hour after the
            # coldest one: the first working hour after the low that is above
            # the threshold, or the end of the day if it never gets there.
            warm = [h["hour"] for h in work
                    if h["hour"] > low_h["hour"] and (h["temp_c"] or -99) > COLD_C]
            items.append(_item("cold", low_h["hour"], warm[0] if warm else work_end, None, None,
                               None, round(min(temps)), impacts, basis, work_start))

    items.sort(key=lambda i: i["onset"])
    if actual:
        for i in items:
            i.update(prob=None, prob_word=None, finish_by=None, remind_at=None,
                     advice_kind="record")
    return {"items": items, "weather_day": is_weather_day(work), "no_impact": not items,
            "actual": bool(actual)}


def _item(kind, onset, end, prob, mm, gust, temp, impacts, basis, work_start):
    onset_s = _clock(onset)
    finish_by = None
    advice_kind = "plan_indoor"
    if kind in ("rain", "wind"):
        if onset - FINISH_BUFFER_HOURS > work_start:
            finish_by = _clock(onset - FINISH_BUFFER_HOURS)
            advice_kind = "finish_before"
        elif kind == "wind" and onset > work_start:
            finish_by = onset_s
            advice_kind = "finish_before"
    elif kind == "heat":
        advice_kind = "pour_early"
    elif kind == "cold":
        advice_kind = "delay_start"
    return {
        "kind": kind, "onset": onset_s, "end": _clock(end), "period": part_of_day(onset),
        "prob": prob, "prob_word": probability_word(prob) if prob is not None else None,
        "mm": mm, "gust_kmh": gust, "temp_c": temp,
        "impacts": impacts, "impact_basis": basis,
        "finish_by": finish_by,
        "remind_at": _minus_minutes(finish_by, REMIND_BEFORE_MINUTES) if finish_by else None,
        "advice_kind": advice_kind,
    }


def is_weather_day(work_hours):
    """A lost day on ACTUALS: enough rain, enough wet hours, or enough wind.
    For a forecast this answers "would be", and callers must say so."""
    wet = [h for h in work_hours if (h["mm"] or 0) >= 1.0]
    rain = sum(h["mm"] or 0 for h in work_hours)
    windy = [h for h in work_hours if (h["gust_kmh"] or 0) >= WIND_STOP_KMH]
    return (rain >= WEATHER_DAY_RAIN_MM or len(wet) >= WEATHER_DAY_WET_HOURS
            or len(windy) >= WEATHER_DAY_GUST_HOURS)


# ---------------------------------------------------------------------------
# Expression -- the fixed template, and the check on the model's version
# ---------------------------------------------------------------------------

def _impact_text(item):
    return ", ".join(item["impacts"])


def _advice_text(item):
    k = item["advice_kind"]
    if k == "record":
        return "record any lost time against the weather"
    if item["kind"] == "rain":
        if k == "finish_before":
            return ("finish the current exterior work before %s, then move to "
                    "interior work" % item["finish_by"])
        return "plan interior work for the day"
    if item["kind"] == "wind":
        if k == "finish_before":
            return "complete lifts before %s and hold them from %s" % (item["finish_by"], item["onset"])
        return "hold lifts and work at height until the wind drops"
    if item["kind"] == "heat":
        return "pour early and keep crews hydrated"
    return "start temperature-sensitive work after %s" % item["end"]


def template_sentence(item):
    """Fact -> impact -> advice, conclusion first, numbers in brackets. The
    fallback, and the reference the model's sentence is checked against."""
    # A day that has happened is "in the morning", not "this morning".
    per = item["period"].replace("this ", "in the ") if item["advice_kind"] == "record"         else item["period"]
    if item["kind"] == "rain" and item["prob"] is None:
        fact = "Rain %s (from %s to %s, %s mm)." % (per, item["onset"], item["end"], _num(item["mm"]))
    elif item["kind"] == "rain":
        fact = "Rain %s %s (from %s, %d%% chance, about %s mm)." % (
            item["prob_word"], per, item["onset"], item["prob"], _num(item["mm"]))
    elif item["kind"] == "wind":
        fact = "Strong gusts %s (from %s, up to %d km/h)." % (per, item["onset"], item["gust_kmh"])
    elif item["kind"] == "heat":
        fact = "Hot %s (around %s, up to %d°C)." % (per, item["onset"], item["temp_c"])
    else:
        fact = "Cold start %s (down to %d°C, until about %s)." % (per, item["temp_c"], item["end"])
    return "%s Impact: %s. Advice: %s." % (fact, _impact_text(item), _advice_text(item))


WEATHER_DAY_LINE = ("Weather day: rain or wind in working hours reached the lost-day "
                    "threshold.")


def render_template(findings):
    lines = [NO_IMPACT_LINE] if findings["no_impact"] else         [template_sentence(i) for i in findings["items"]]
    if findings.get("actual") and findings.get("weather_day"):
        lines.append(WEATHER_DAY_LINE)
    return lines


def _num(x):
    return ("%g" % x) if isinstance(x, (int, float)) else str(x)


_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_NUM_RE = re.compile(r"(?<![\d:])(\d+(?:\.\d+)?)(?![\d:])")


def facts_in(text):
    """(times, numbers) mentioned in a sentence, normalised: times as HH:MM,
    numbers as floats. Times are taken out before numbers are read, so 14:00
    is one time and not the numbers 14 and 00."""
    times = {"%02d:%s" % (int(h), m) for h, m in _TIME_RE.findall(text or "")}
    rest = _TIME_RE.sub(" ", text or "")
    nums = {float(n) for n in _NUM_RE.findall(rest)}
    return times, nums


def expected_facts(item):
    """The times and numbers a correct sentence about `item` carries."""
    return facts_in(template_sentence(item))


def phrasing_is_faithful(text, item):
    """True only when the model's sentence mentions exactly the item's times
    and numbers: none missing, none added, none changed."""
    return facts_in(text) == expected_facts(item)


def express(findings, phrase=None):
    """Sentences for the day. `phrase(item, template) -> str | None` is the
    model; each of its sentences is kept only if phrasing_is_faithful, and is
    replaced by the template otherwise. Returns (lines, used_model_count)."""
    if findings["no_impact"]:
        return [NO_IMPACT_LINE], 0
    lines, used = [], 0
    for item in findings["items"]:
        ref = template_sentence(item)
        out = None
        if phrase is not None:
            try:
                out = phrase(item, ref)
            except Exception:
                out = None
        if out and phrasing_is_faithful(out, item):
            lines.append(out.strip())
            used += 1
        else:
            lines.append(ref)
    return lines, used
