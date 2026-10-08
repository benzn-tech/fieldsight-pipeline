"""The people-and-sites directory, as the non-VPC pipeline lambdas see it.

WHY THIS EXISTS. The report generator, the meeting-minutes lambda and the
extraction lambda cannot reach Aurora (no VpcConfig, by design: they need
egress for the model and other APIs), yet they need to know who a recording
folder belongs to, what that person's role is, which sites they work on and
what those sites are called. Until 2026-10 that came from
`config/user_mapping.json`, which was hand-edited in August and which nothing
writes. It named 8 devices and 5 sites; production has moved on, and every
report stamped from it carries the August answer.

So org-api publishes ONE machine-owned object, `config/directory.json`, built
from Aurora (see `lambda_org_api._publish_directory`), and this module is the
single place that reads it. Same shape of solution as `site_coords.py`.

SHAPE (keyed by identity -- the recording folder -- not by display name)::

    {"version": 1, "published_at": ISO,
     "people": {"<folder_name>": {"name", "role", "company_id",
                                  "primary_site": "<slug>|null",
                                  "sites": ["<slug>", ...]}},
     "sites":  {"<slug>": {"name", "location", "client", "company_id",
                           "site_id"}}}

TRANSITION FALLBACK. If `config/directory.json` is absent, the old
`config/user_mapping.json` is read and adapted to the same shape, with one
WARNING per container. The fallback is removed once the directory has been
live on production with no such warning for a week. If NEITHER exists that is
an ERROR, not the silent `{}` the readers used to return: an empty directory
makes every report ship without a role or a site, and nothing says so.
"""
import json
import logging

import site_coords

logger = logging.getLogger(__name__)

KEY = "config/directory.json"
LEGACY_KEY = "config/user_mapping.json"
VERSION = 1

# Codes that mean "no such object" for a caller that holds GetObject but not
# ListBucket on the key: S3 answers 403, not 404, for a missing key then.
_ABSENT = ("NoSuchKey", "404", "NotFound", "AccessDenied", "403")

_cache = None


def empty():
    return {"version": VERSION, "people": {}, "sites": {}}


def reset_cache():
    global _cache
    _cache = None


# ----------------------------------------------------------------------
# Building (the publisher's side; pure)
# ----------------------------------------------------------------------
def display_name(first, last):
    return " ".join(p.strip() for p in (first, last) if p and p.strip())


def build(user_rows, membership_rows, site_rows, published_at):
    """Rows from Aurora -> the document.

    `user_rows`: live users (archived already excluded) with folder_name,
    first_name, last_name, global_role, company_id.
    `membership_rows`: live memberships on live sites, user_id + site_id.
    `site_rows`: live sites. Archived users and sites are simply absent, and a
    user without a folder has nothing the pipeline could key them by.
    """
    sites = {}
    slug_by_id = {}
    for s in site_rows:
        slug = site_coords.slug_for_site(s)
        if not slug:
            continue
        slug_by_id[str(s["id"])] = slug
        sites[slug] = {
            "name": s.get("name") or "",
            "location": s.get("location"),
            "client": s.get("client"),
            "company_id": str(s["company_id"]) if s.get("company_id") else None,
            "site_id": str(s["id"]),
        }
    by_user = {}
    for m in membership_rows:
        slug = slug_by_id.get(str(m["site_id"]))
        if slug:
            mine = by_user.setdefault(str(m["user_id"]), [])
            if slug not in mine:
                mine.append(slug)
    people = {}
    for u in user_rows:
        folder = u.get("folder_name")
        if not folder:
            continue
        mine = sorted(by_user.get(str(u["id"]), []))
        people[folder] = {
            "name": display_name(u.get("first_name"), u.get("last_name")) or folder,
            "role": u.get("global_role") or "",
            "company_id": str(u["company_id"]) if u.get("company_id") else None,
            # Exactly one open site is unambiguous; with several, naming one
            # would be a guess that gets stamped on a report.
            "primary_site": mine[0] if len(mine) == 1 else None,
            "sites": mine,
        }
    return {"version": VERSION, "published_at": published_at,
            "people": dict(sorted(people.items())), "sites": dict(sorted(sites.items()))}


def same_content(a, b):
    """True when two documents differ at most in `published_at` -- the check
    that keeps an unchanged directory from being rewritten on every save."""
    def core(d):
        return {k: v for k, v in (d or {}).items() if k != "published_at"}
    return core(a) == core(b)


# ----------------------------------------------------------------------
# Reading (the pipeline lambdas' side)
# ----------------------------------------------------------------------
def adapt_legacy(data):
    """user_mapping.json -> the directory shape. People are keyed by the
    underscored display name, which is the folder convention (`Ben_Lin`); the
    device ids in `mapping` are dropped because nothing downstream is keyed by
    them any more."""
    out = empty()
    if not isinstance(data, dict):
        return out
    for _device, value in (data.get("mapping") or {}).items():
        if not isinstance(value, dict):
            continue
        name = (value.get("name") or "").strip()
        if not name:
            continue
        primary = value.get("primary_site") or None
        sites = list(value.get("sites") or ([primary] if primary else []))
        out["people"][name.replace(" ", "_")] = {
            "name": name, "role": value.get("role") or "", "company_id": None,
            "primary_site": primary, "sites": sites}
    for slug, info in (data.get("sites") or {}).items():
        if isinstance(info, dict):
            out["sites"][slug] = dict(info)
    return out


def _get(s3_client, bucket, key):
    """Parsed JSON, or None when the object is absent. Anything else raises."""
    from botocore.exceptions import ClientError
    try:
        obj = s3_client.get_object(Bucket=bucket, Key=key)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in _ABSENT:
            return None
        raise
    return json.loads(obj["Body"].read().decode("utf-8"))


def load(s3_client, bucket):
    """The directory, cached for the life of the container (a handler that
    needs a fresh read at its top calls `reset_cache()`)."""
    global _cache
    if _cache is not None:
        return _cache
    doc = None
    try:
        doc = _get(s3_client, bucket, KEY)
    except Exception:  # noqa: BLE001 - fall through to the fallback, loudly
        logger.exception("directory: could not read %s", KEY)
    if isinstance(doc, dict) and isinstance(doc.get("people"), dict):
        doc.setdefault("sites", {})
        _cache = doc
        return _cache
    legacy = None
    try:
        legacy = _get(s3_client, bucket, LEGACY_KEY)
    except Exception:  # noqa: BLE001
        logger.exception("directory: could not read %s", LEGACY_KEY)
    if legacy:
        logger.warning("directory: falling back to user_mapping.json -- %s is "
                       "missing or unreadable", KEY)
        _cache = adapt_legacy(legacy)
        return _cache
    logger.error("directory: neither %s nor %s could be read -- every person and "
                 "site lookup will come back empty", KEY, LEGACY_KEY)
    _cache = empty()
    return _cache


def person(doc, folder):
    return ((doc or {}).get("people") or {}).get(folder) or {}


def names_by_folder(doc):
    return {f: p.get("name") or f for f, p in ((doc or {}).get("people") or {}).items()}
