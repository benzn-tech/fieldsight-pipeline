"""Site coordinates, published where the report generator can reach them.

THE PROBLEM THIS SOLVES. The report generator already fetches weather, stores
it on the report and feeds it to the prompt with a correlation instruction
("rain -> concrete/paint delays; high wind -> crane work; do not invent impacts
the transcript does not support"). It has never produced a single one, because
`build_weather_block_for_site` takes the coordinate from
`config/user_mapping.json`, where all five sites have null lat/lng -- while the
coordinates people enter in the web UI live in the Aurora `sites` table.

Two sources, one of them empty. The feature was not missing, it was starved.

WHY NOT JUST WRITE THE COORDINATES INTO user_mapping.json. Nothing in this
repository writes that file -- six lambdas read it and humans maintain it, and
it carries the device mapping and the reassignment log alongside the sites
block. Making it machine-written puts a hand-edited file under read-modify-write
from a request handler.

So this is a SEPARATE, SINGLE-PURPOSE, MACHINE-OWNED object. The IAM grant for
it names the one key rather than the `config/` prefix, so a bug here cannot
reach user_mapping.json even by mistake.

KEYED BY SLUG, because that is what the generator looks sites up by
(`sites_info.get(user_site_id)` where `user_site_id` is a primary_site slug).
Verified against production: the four sites present in both sources use
identical slugs -- sb1108-ellesmere, sb1131-northbrook-wanaka,
mangere-wasterwater-treatment-plan, uc-pk.

MERGED, NEVER REPLACED. Each publish sets one site's entry and leaves the rest
alone: a site whose coordinate is cleared should stop reporting weather, but a
site nobody touched must not lose one because an unrelated save happened to run
against a stale read.
"""

KEY = "config/site-coords.json"


def entry_for_site(site):
    """One site row -> the entry to publish, or None when there is nothing to
    say. A row with no coordinate yields None rather than an entry with nulls:
    absent means "we cannot place this site", and an entry full of nulls says
    the same thing in a form every reader has to remember to check."""
    if not site:
        return None
    lat = site.get("latitude")
    lng = site.get("longitude")
    if lat is None or lng is None:
        return None
    return {
        "site_id": str(site.get("id") or site.get("site_id") or ""),
        "name": site.get("name") or "",
        "latitude": lat,
        "longitude": lng,
    }


def slug_for_site(site):
    """The key. Falls back to the id when a site has no slug -- org-api-created
    sites had NULL slug until BUG-39/WS4, and a coordinate published under a
    key nothing looks up is the same as not publishing it, only harder to
    notice."""
    if not site:
        return None
    slug = (site.get("slug") or "").strip()
    if slug:
        return slug
    sid = site.get("id") or site.get("site_id")
    return str(sid) if sid else None


def merge_site(doc, site):
    """Pure. Returns a NEW document with this site's entry set, or removed when
    the site no longer has a coordinate.

    Removal is deliberate and is the reason this is not a plain dict update: a
    coordinate deleted in the UI has to stop producing weather, or the report
    keeps quoting a place the customer has told us is wrong.
    """
    out = dict(doc or {})
    slug = slug_for_site(site)
    if not slug:
        return out
    entry = entry_for_site(site)
    if entry is None:
        out.pop(slug, None)
    else:
        out[slug] = entry
    return out


def coords_for_slug(doc, slug):
    """(lat, lng) for a slug, or (None, None). Never raises on a malformed
    document: this sits in front of a weather fetch that is already optional,
    and a config object with an unexpected shape must not stop a report."""
    try:
        entry = (doc or {}).get(slug) or {}
        lat = entry.get("latitude")
        lng = entry.get("longitude")
        if lat is None or lng is None:
            return None, None
        return lat, lng
    except Exception:  # noqa: BLE001 - see docstring
        return None, None


def overlay_sites_info(sites_info, doc):
    """Fold published coordinates onto the generator's `sites` block.

    ADDITIVE, and user_mapping WINS where it has a value. That direction is
    deliberate: user_mapping.json is hand-maintained, so a coordinate written
    there is a human overriding whatever the UI holds, and silently replacing
    it would make the file's contents a lie. In practice every entry there is
    null today, so in practice this fills all of them.
    """
    # A CONFIG OBJECT OF THE WRONG SHAPE MUST NOT STOP A REPORT. This runs on
    # the daily generation path, in front of a weather fetch that is already
    # optional; a hand-edited or half-written S3 object would otherwise take
    # the whole day's report down with an AttributeError.
    if not isinstance(doc, dict):
        doc = {}
    if not isinstance(sites_info, dict):
        sites_info = {}

    out = {}
    for slug, info in (sites_info or {}).items():
        merged = dict(info or {})
        if merged.get("latitude") is None or merged.get("longitude") is None:
            lat, lng = coords_for_slug(doc, slug)
            if lat is not None and lng is not None:
                merged["latitude"] = lat
                merged["longitude"] = lng
                merged["coord_source"] = "site-coords"
        out[slug] = merged

    # A site that exists in Aurora but not in user_mapping.json still needs to
    # be reachable: the generator looks up by primary_site slug, and if that
    # slug is absent from `sites` it gets {} and no weather. Adding it costs
    # nothing and is the difference between "weather for the sites someone
    # remembered to list" and "weather for the sites that have a coordinate".
    for slug, entry in (doc or {}).items():
        if slug in out:
            continue
        if not isinstance(entry, dict):
            continue
        lat = entry.get("latitude")
        lng = entry.get("longitude")
        if lat is None or lng is None:
            continue
        out[slug] = {"name": entry.get("name") or "", "latitude": lat,
                     "longitude": lng, "coord_source": "site-coords"}
    return out
