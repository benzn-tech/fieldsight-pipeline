"""Non-VPC backfill: geocode sites that have an address but no coordinates.

Deploy WITHOUT VpcConfig (mirrors ExtractSessionFunction). The pure planner
below is fully unit-tested; the thin runner geocodes coord-less sites and
writes the coordinates back through the org-api PATCH endpoint (Task 2) --
the in-VPC org-api persists them (BUG-36: geocoding stays out here,
persistence stays in-VPC). Runs on demand / low frequency; never per request.
"""
import geocode as _geocode


def plan_coordinate_backfill(sites, geocode_fn=_geocode.geocode):
    """Pure. For each site with null lat/lng, geocode its address and, failing
    that, its `location`, and emit an update dict. Skips sites that already
    have coordinates and sites where neither field resolves.

    WHY `location` IS TRIED AT ALL. This planner used to consider `address`
    only, and measured against production that covered almost nothing: of the
    eight active sites, five had no coordinate and only ONE of those five had
    an address. The rest carry a city in `location` -- "Auckland", "Wanaka",
    "Christchurch" -- and every one of them geocodes. The address-only rule was
    not conservative, it was inert.

    The cost of leaving them un-placed is not a blank panel. The weather
    indicator used to fall back to a hardcoded Christchurch with no label, so
    MANGERE WASTEWATER (Auckland) and Northbrook Wanaka were both reporting
    Christchurch's forecast: 760km out, and coastal weather standing in for
    alpine.

    ADDRESS STILL WINS, and the `source` field records which one answered. A
    street address places a site to the metre; a city name places it to the
    city, which is the right granularity for a forecast but the wrong one for
    anything that later wants to know where the crew actually was. A consumer
    that needs precision can tell the two apart instead of having to assume.
    """
    updates = []
    for s in sites or []:
        if s.get("latitude") is not None or s.get("longitude") is not None:
            continue
        address = (s.get("address") or "").strip()
        location = (s.get("location") or "").strip()

        hit = None
        source = None
        query = None
        for candidate, kind in ((address, "address"), (location, "location")):
            if not candidate:
                continue
            hit = geocode_fn(candidate)
            if hit:
                source, query = kind, candidate
                break

        if not hit:
            continue
        updates.append({
            "site_id": s.get("id") or s.get("site_id"),
            "address": address,
            # What was actually asked, and which field it came from. Without
            # these a row backfilled from "Auckland" is indistinguishable from
            # one surveyed on site.
            "query": query,
            "source": source,
            "latitude": hit["lat"],
            "longitude": hit["lng"],
            "formatted": hit.get("formatted", ""),
        })
    return updates


def run_backfill(fetch_sites_fn, persist_fn, geocode_fn=_geocode.geocode):
    """Thin orchestration (I/O edges injected -- verified by manual invoke,
    not unit tests). `fetch_sites_fn() -> list[site dict]`; `persist_fn(update)`
    PATCHes /api/org/sites/{site_id} with {latitude, longitude} (admin token)."""
    plan = plan_coordinate_backfill(fetch_sites_fn(), geocode_fn=geocode_fn)
    results = []
    for update in plan:
        results.append(persist_fn(update))
    return results
