"""Weather advice: the code decides, the model only words it, the code checks.

THE test is the owner's own example, end to end through the code path:

    Rain very likely this afternoon (from 14:00, 75% chance, about 8 mm).
    Impact: exterior painting. Advice: finish the current exterior work
    before 12:00, then move to interior work.

Every value in it -- "very likely", 14:00, 75%, 8 mm, which trade, 12:00 --
is decided by code from the hourly data. None of it is left to a model.
"""
import pytest

import weather_advice as wa


def hours(**by_hour):
    """A dry, calm, mild day; override single hours with dicts."""
    out = []
    for h in range(24):
        row = {"hour": h, "prob": 0, "mm": 0.0, "gust_kmh": 15.0, "temp_c": 15.0}
        row.update(by_hour.get("h%d" % h, {}))
        out.append(row)
    return out


AFTERNOON_RAIN = hours(
    h14={"prob": 75, "mm": 3.0}, h15={"prob": 70, "mm": 3.0}, h16={"prob": 60, "mm": 2.0})


# ---- THE test -----------------------------------------------------------------

def test_THE_owners_example_comes_out_of_the_code():
    f = wa.assess(AFTERNOON_RAIN, planned=["Exterior painting - north elevation", "Level 2 fit-out"])
    assert f["items"] and f["items"][0]["kind"] == "rain"
    assert wa.render_template(f) == [
        "Rain very likely this afternoon (from 14:00, 75% chance, about 8 mm). "
        "Impact: Exterior painting - north elevation. "
        "Advice: finish the current exterior work before 12:00, then move to interior work."]


# ---- fixed wording --------------------------------------------------------------

@pytest.mark.parametrize("pct,word", [
    (100, "almost certain"), (90, "almost certain"), (89, "very likely"), (70, "very likely"),
    (69, "likely"), (50, "likely"), (49, "possible"), (30, "possible"), (29, None), (0, None)])
def test_probability_always_uses_the_same_words(pct, word):
    assert wa.probability_word(pct) == word


def test_the_conclusion_comes_before_the_numbers():
    line = wa.render_template(wa.assess(AFTERNOON_RAIN, planned=["exterior painting"]))[0]
    assert line.index("Rain very likely") < line.index("(")
    assert line.index("Impact:") < line.index("Advice:")


# ---- no impact, not written ----------------------------------------------------

def test_a_dry_calm_day_is_one_line():
    assert wa.render_template(wa.assess(hours(), planned=["concrete pour"])) == [wa.NO_IMPACT_LINE]


def test_rain_that_hits_nothing_planned_is_not_written():
    f = wa.assess(AFTERNOON_RAIN, planned=["Level 2 fit-out", "electrical rough-in"])
    assert f["no_impact"] and wa.render_template(f) == [wa.NO_IMPACT_LINE]


def test_rain_outside_working_hours_is_not_written():
    f = wa.assess(hours(h19={"prob": 90, "mm": 6.0}, h20={"prob": 90, "mm": 6.0}),
                  planned=["exterior painting"])
    assert f["no_impact"]


def test_a_light_unlikely_shower_is_not_written():
    f = wa.assess(hours(h10={"prob": 35, "mm": 0.3}), planned=["exterior painting"])
    assert f["no_impact"]


def test_without_a_plan_the_general_trades_are_named_and_marked_so():
    item = wa.assess(AFTERNOON_RAIN, planned=None)["items"][0]
    assert item["impact_basis"] == "general"
    assert item["impacts"] == ["exterior painting", "concrete pours"]


# ---- the times are computed ------------------------------------------------------

def test_finish_by_is_two_hours_before_onset_and_the_reminder_an_hour_before_that():
    item = wa.assess(AFTERNOON_RAIN, planned=["exterior painting"])["items"][0]
    assert (item["onset"], item["finish_by"], item["remind_at"]) == ("14:00", "12:00", "11:00")


def test_rain_from_the_start_of_the_day_says_plan_indoors_not_a_time_already_past():
    item = wa.assess(hours(h7={"prob": 80, "mm": 2.0}, h8={"prob": 80, "mm": 2.0}),
                     planned=["exterior painting"])["items"][0]
    assert item["finish_by"] is None and item["advice_kind"] == "plan_indoor"
    assert "plan interior work for the day" in wa.template_sentence(item)


def test_gusts_name_the_lift_cut_off():
    f = wa.assess(hours(h13={"gust_kmh": 55}, h14={"gust_kmh": 62}), planned=["Crane lift - steel"])
    line = wa.render_template(f)[0]
    assert line.startswith("Strong gusts this afternoon (from 13:00, up to 62 km/h).")
    assert "complete lifts before 11:00 and hold them from 13:00" in line


# ---- weather day, on actuals -----------------------------------------------------

def test_a_weather_day_is_decided_by_code():
    assert wa.is_weather_day(wa._working(hours(**{"h%d" % h: {"mm": 2.5} for h in range(8, 12)}), 7, 17))
    assert not wa.is_weather_day(wa._working(hours(h10={"mm": 2.0}), 7, 17))


def test_archive_hours_carry_certainty_not_a_forecast_probability():
    data = {"hourly": {"time": ["2026-09-23T14:00", "2026-09-23T15:00"],
                       "precipitation": [3.0, 0.0], "wind_gusts_10m": [20, 20],
                       "temperature_2m": [14, 14]}}
    rows = wa.normalize_hourly(data, historical=True)
    assert [r["prob"] for r in rows] == [100, 0]


# ---- the model words it; the code checks it -----------------------------------------

ITEM = wa.assess(AFTERNOON_RAIN, planned=["exterior painting"])["items"][0]


def test_a_faithful_rewording_is_kept():
    text = ("Afternoon rain is very likely from 14:00 (75% chance, around 8 mm), which will "
            "stop exterior painting; wrap up the current coat by 12:00 and switch to interior work.")
    lines, used = wa.express({"items": [ITEM], "no_impact": False}, phrase=lambda i, ref: text)
    assert used == 1 and lines == [text]


@pytest.mark.parametrize("bad", [
    "Rain very likely this afternoon (from 15:00, 75% chance, about 8 mm). Finish by 12:00.",  # time changed
    "Rain very likely this afternoon (from 14:00, 80% chance, about 8 mm). Finish by 12:00.",  # prob changed
    "Rain very likely this afternoon from 14:00. Finish by 12:00.",                            # numbers dropped
    "Rain very likely (from 14:00, 75%, about 8 mm, 3 hours). Finish by 12:00.",               # number added
])
def test_a_sentence_whose_numbers_or_times_differ_falls_back_to_the_template(bad):
    lines, used = wa.express({"items": [ITEM], "no_impact": False}, phrase=lambda i, ref: bad)
    assert used == 0 and lines == [wa.template_sentence(ITEM)]


def test_a_model_that_fails_or_returns_nothing_falls_back_to_the_template():
    for phrase in (lambda i, ref: None, lambda i, ref: "", lambda i, ref: 1 / 0):
        lines, used = wa.express({"items": [ITEM], "no_impact": False}, phrase=phrase)
        assert used == 0 and lines == [wa.template_sentence(ITEM)]


def test_the_no_impact_line_never_goes_to_the_model():
    called = []
    lines, used = wa.express({"items": [], "no_impact": True}, phrase=lambda i, r: called.append(1))
    assert lines == [wa.NO_IMPACT_LINE] and not called


def test_a_time_is_not_read_as_two_numbers():
    assert wa.facts_in("from 14:00, 75% chance") == ({"14:00"}, {75.0})


# ---- a day that has happened ------------------------------------------------------

def test_actuals_state_what_happened_without_a_probability_or_a_finish_by_time():
    """Found on real data (Christchurch, 2026-09-23): the forecast wording on
    an archive day read "Rain almost certain ... 100% chance ... finish before
    08:00" -- a probability and a deadline for yesterday."""
    f = wa.assess(AFTERNOON_RAIN, planned=["exterior painting"], actual=True)
    assert wa.render_template(f) == [
        "Rain in the afternoon (from 14:00 to 17:00, 8 mm). Impact: exterior painting. "
        "Advice: record any lost time against the weather."]
    assert f["items"][0]["remind_at"] is None


def test_an_actual_weather_day_is_said_once_at_the_end():
    wet = hours(**{"h%d" % h: {"mm": 2.5} for h in range(8, 12)})
    lines = wa.render_template(wa.assess(wet, planned=["exterior painting"], actual=True))
    assert lines[-1] == wa.WEATHER_DAY_LINE


def test_a_forecast_never_claims_a_weather_day_line():
    wet = hours(**{"h%d" % h: {"mm": 2.5, "prob": 90} for h in range(8, 12)})
    assert wa.WEATHER_DAY_LINE not in wa.render_template(wa.assess(wet, planned=["exterior painting"]))


def test_cold_lasts_until_it_is_warm_enough_again_not_one_hour():
    cold = hours(h7={"temp_c": 1.0}, h8={"temp_c": 2.0}, h9={"temp_c": 3.0}, h10={"temp_c": 6.0})
    item = wa.assess(cold, planned=["concrete pour"])["items"][0]
    assert (item["kind"], item["onset"], item["end"]) == ("cold", "07:00", "10:00")


def test_the_general_trades_are_two_different_trades():
    for kind, names in wa.GENERAL_IMPACT.items():
        assert len(set(names)) == len(names)
    assert wa.GENERAL_IMPACT["wind"] == ["crane lifts", "work at height"]


# ---- the looked-up thresholds (2026-09-29) --------------------------------------

def test_thresholds_follow_the_sources():
    """NZS 3109 (5 C / 30 C for concrete), Resene (10 C for paint), EWPA and
    scaffold guidance for wind. Changing one of these is a decision, not a
    tidy-up, so it goes red here first."""
    assert (wa.COLD_C, wa.HEAT_C, wa.PAINT_MIN_C) == (5, 30, 10)
    assert (wa.WIND_GUST_KMH, wa.WIND_STOP_KMH) == (40, 50)


def test_a_cool_morning_affects_painting_only():
    cool = hours(h7={"temp_c": 7.0}, h8={"temp_c": 9.0}, h9={"temp_c": 11.0})
    f = wa.assess(cool, planned=None)
    assert [i["kind"] for i in f["items"]] == ["cool"]
    assert wa.render_template(f) == [
        "Cool start this morning (down to 7°C, until about 09:00). "
        "Impact: exterior painting. Advice: start exterior painting after 09:00."]


def test_a_cold_morning_is_reported_once_not_as_cold_and_cool():
    cold = hours(h7={"temp_c": 2.0}, h8={"temp_c": 6.0}, h9={"temp_c": 11.0})
    kinds = [i["kind"] for i in wa.assess(cold, planned=None)["items"]]
    assert kinds == ["cold"]


def test_29_degrees_is_not_hot_for_concrete_and_30_is():
    assert not wa.assess(hours(h13={"temp_c": 29.0}), planned=["concrete pour"])["items"]
    assert wa.assess(hours(h13={"temp_c": 30.0}), planned=["concrete pour"])["items"][0]["kind"] == "heat"


# ---- the day's programme (owner, 2026-09-29) ------------------------------------

@pytest.mark.parametrize("kind,name,hit", [
    ("rain", "Pour L2 slab", True),
    ("rain", "Roof sheeting - Block B", True),
    ("rain", "External render to west wall", True),
    ("rain", "Internal painting - Level 3", False),      # indoors, whatever the trade
    ("rain", "Level 2 fit-out", False),
    ("wind", "Steel erection gridline 4", True),
    ("wind", "Precast panel install", True),
    ("wind", "Tower crane lifts - roof trusses", True),
    ("wind", "Interior glazing", False),
    ("heat", "Pour L2 slab", True),
    ("cool", "Exterior painting - north elevation", True),
    ("cool", "Electrical rough-in", False),
])
def test_real_task_names_are_matched_by_word_stem(kind, name, hit):
    assert wa._hits(kind, name) is hit


PROGRAMME = {"leaves": [
    {"name": "Pour L2 slab", "start": "2026-09-29", "end": "2026-09-29", "progress_pct": 0},
    {"name": "Roof sheeting - Block B", "start": "2026-09-25", "end": "2026-10-03", "progress_pct": 40},
    {"name": "Excavation - carpark", "start": "2026-09-01", "end": "2026-09-20", "progress_pct": 100},
    {"name": "Internal linings", "start": "2026-09-28", "end": "2026-10-10", "progress_pct": 10},
    {"name": "Done early", "start": "2026-09-28", "end": "2026-10-02", "status": "complete"},
    {"name": "Next week", "start": "2026-10-06", "end": "2026-10-08"},
]}


def test_the_days_plan_is_what_runs_today_and_is_not_finished():
    assert wa.planned_from_programme(PROGRAMME, "2026-09-29") == [
        "Pour L2 slab", "Roof sheeting - Block B", "Internal linings"]


def test_no_programme_is_none_not_an_empty_day():
    assert wa.planned_from_programme(None, "2026-09-29") is None
    assert wa.planned_from_programme({"parents": []}, "2026-09-29") is None
    assert wa.planned_from_programme({"leaves": []}, "2026-09-29") == []


def test_rain_names_the_planned_tasks_it_hits_and_only_those():
    planned = wa.planned_from_programme(PROGRAMME, "2026-09-29")
    item = wa.assess(AFTERNOON_RAIN, planned=planned)["items"][0]
    assert item["impact_basis"] == "planned"
    assert item["impacts"] == ["Pour L2 slab", "Roof sheeting - Block B"]


def test_a_plan_with_nothing_exposed_makes_the_weather_one_line():
    f = wa.assess(AFTERNOON_RAIN, planned=["Internal linings", "Electrical rough-in"])
    assert wa.render_template(f) == [wa.NO_IMPACT_LINE]
