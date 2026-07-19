"""Photon (OSM/Komoot) geocoder -- free, keyless, autocomplete-capable.

NON-VPC ONLY. Photon is a public HTTP endpoint; calling it from an in-VPC
Lambda with no egress black-holes until timeout (BUG-36). This module is used
by the browser (see fieldsight-ui) and by the non-VPC backfill helper
(backfill_site_coords.py).

Photon returns a GeoJSON FeatureCollection; geometry.coordinates is
[lng, lat] (GeoJSON order), not [lat, lng].
"""
import json
from urllib.parse import quote

PHOTON_URL = "https://photon.komoot.io/api"


def parse_photon_features(geojson) -> list:
    """Pure. Extract {lat, lng, formatted, raw} from a Photon GeoJSON
    FeatureCollection. Missing/malformed input yields an empty list."""
    features = (geojson or {}).get("features") or []
    out = []
    for f in features:
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        lng, lat = coords[0], coords[1]
        out.append({
            "lat": lat,
            "lng": lng,
            "formatted": _format(f.get("properties") or {}),
            "raw": f,
        })
    return out


def _format(props) -> str:
    parts = []
    hn, street = props.get("housenumber"), props.get("street")
    if hn and street:
        parts.append(f"{hn} {street}")
    elif street:
        parts.append(street)
    elif props.get("name"):
        parts.append(props["name"])
    for key in ("city", "postcode", "state", "country"):
        if props.get(key):
            parts.append(props[key])
    return ", ".join(parts)


def geocode(query, http=None, limit=5):
    """Return the best {formatted, lat, lng} for `query`, or None.

    `http` is injectable (a urllib3.PoolManager-like object with a
    `.request(method, url, timeout=...) -> resp` method, where `resp` has
    `.status` and `.data`) so tests can pass a fake double -- no real network
    calls happen in unit tests. Defaults to a real urllib3.PoolManager,
    matching the pattern used by claude_utils.py / lambda_report_generator.py
    for other non-VPC outbound HTTP calls.
    """
    if not query or not str(query).strip():
        return None
    if http is None:
        import urllib3
        http = urllib3.PoolManager()
    url = f"{PHOTON_URL}?q={quote(str(query))}&limit={limit}&lang=en"
    try:
        resp = http.request("GET", url, timeout=10.0)
        if resp.status != 200:
            return None
        feats = parse_photon_features(json.loads(resp.data.decode("utf-8")))
    except Exception:
        return None
    if not feats:
        return None
    top = feats[0]
    return {"formatted": top["formatted"], "lat": top["lat"], "lng": top["lng"]}
