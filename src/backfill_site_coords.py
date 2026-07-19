"""Non-VPC backfill: geocode sites that have an address but no coordinates.

Deploy WITHOUT VpcConfig (mirrors ExtractSessionFunction). The pure planner
below is fully unit-tested; the thin runner geocodes coord-less sites and
writes the coordinates back through the org-api PATCH endpoint (Task 2) --
the in-VPC org-api persists them (BUG-36: geocoding stays out here,
persistence stays in-VPC). Runs on demand / low frequency; never per request.
"""
import geocode as _geocode


def plan_coordinate_backfill(sites, geocode_fn=_geocode.geocode):
    """Pure. For each site with a non-empty address and null lat/lng that
    geocodes successfully, emit an update dict. Skips everything else
    (sites that already have coords, sites without an address, and geocode
    misses)."""
    updates = []
    for s in sites or []:
        if s.get("latitude") is not None or s.get("longitude") is not None:
            continue
        address = (s.get("address") or "").strip()
        if not address:
            continue
        hit = geocode_fn(address)
        if not hit:
            continue
        updates.append({
            "site_id": s.get("id") or s.get("site_id"),
            "address": address,
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
