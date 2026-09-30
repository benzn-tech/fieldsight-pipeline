"""The day's weather at a site, as the reports record it: measured totals and
what they meant for the work (weather_advice decides; the model only words).

Moved out of lambda_report_generator so the template-report worker computes
it with the SAME code rather than a second copy that could drift -- a
compliance record that says one thing in the daily report and another in a
customer's template report is worse than either alone.
"""
import logging

import weather
import weather_advice

logger = logging.getLogger(__name__)


def build_weather_block_for_site(site_info, target_date, today_iso,
                                 fetch=weather.fetch_weather):
    """Fetch the normalized (site, date) weather block, or None when the site
    has no coordinate (un-backfilled) or the fetch fails. Non-VPC: this runs in
    ReportGeneratorFunction, which has egress. Coordinate comes from the
    config/user_mapping.json `sites` block (D-COORD option a)."""
    lat = site_info.get("latitude")
    lng = site_info.get("longitude")
    if lat is None or lng is None:
        return None
    try:
        return fetch(lat, lng, target_date, today_iso)
    except Exception as e:
        logger.warning(f"weather fetch failed for {target_date}: {e}")
        return None


def build_weather_findings(site_info, target_date, today_iso,
                           fetch=weather_advice.hourly_forecast,
                           programme=None):
    """What the weather meant for the day's work, DECIDED BY CODE.

    Until 2026-09-29 the report handed the model one sentence of daily totals
    and asked it to "note the linkage" between weather and work -- every
    threshold, every trade, left to the model and decided differently each
    night. weather_advice.assess makes those calls from the hourly actuals;
    the lines here are its fixed template, so the compliance record says the
    same thing about the same weather every time.

    THE DAY'S PROGRAMME decides what is impacted (owner, 2026-09-29): the
    tasks running on the day and not finished are matched against the
    weather; a day whose exposed work is all indoors gets one "no impact"
    line. A site with no programme names the trades weather affects in
    general. `impact_basis` ("planned" | "general") says which it was.
    None when the site has no coordinate or the fetch fails -- the report
    says "not recorded" rather than guessing.
    """
    lat = site_info.get("latitude")
    lng = site_info.get("longitude")
    if lat is None or lng is None:
        return None
    historical = bool(today_iso and target_date < today_iso)
    try:
        hours = fetch(lat, lng, target_date, historical)
    except Exception as e:
        logger.warning(f"hourly weather fetch failed for {target_date}: {e}")
        return None
    if not hours:
        return None
    planned = weather_advice.planned_from_programme(programme, target_date)
    f = weather_advice.assess(hours, planned=planned, actual=historical)
    return {
        "lines": weather_advice.render_template(f),
        "weather_day": f["weather_day"] if historical else None,
        "actual": historical,
        "impact_basis": "general" if planned is None else "planned",
        "planned": planned,
        "items": f["items"],
    }
