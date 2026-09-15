"""The in-app update manifest: which app build is newest for this stage, and how to fetch it.

Each stack serves the flavour that talks to it -- the prod stack's bucket holds prod-flavour
builds, the test stack's bucket holds dev-flavour builds -- so there is no flavour parameter to
get wrong. A release is two objects in the stage's data bucket:

    app-releases/<versionCode>/<file>.apk     the APK
    app-releases/latest.json                  this manifest, written LAST by the release script

WHY THIS EXISTS. Every update used to be a one-to-one remote install: someone on site opening an
APK by hand, on a 320-dp screen, for each of twenty devices rotated between clients. The app now
asks this endpoint, downloads the APK itself, verifies it and offers to install it.

Pure, so every rule is unit-tested. The handler in lambda_org_api reads the object and presigns.
"""
import json
import logging
import re

logger = logging.getLogger(__name__)

MANIFEST_KEY = "app-releases/latest.json"
KEY_PREFIX = "app-releases/"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_NOTES = 500


def _int(value):
    """An int that JSON really sent as a number. `True` is an int in Python and must not pass."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"not an integer: {value!r}")
    return value


def parse_manifest(raw):
    """The manifest as a validated dict, or None -- logged -- when it cannot be used.

    None rather than a partial answer: the device installs what this describes, and it verifies
    the download against `sha256` before offering it. A manifest with a missing or malformed field
    is a broken release, and a broken release must read as "nothing to install", never as an
    install of something the fields do not describe.
    """
    try:
        m = json.loads(raw)
    except (TypeError, ValueError) as e:
        logger.warning("app-release manifest is not valid JSON: %s", e)
        return None
    if not isinstance(m, dict):
        logger.warning("app-release manifest is not an object")
        return None
    try:
        version_code = _int(m["versionCode"])
        min_version_code = _int(m.get("minVersionCode", 0))
        size_bytes = _int(m["sizeBytes"])
        version_name = m["versionName"]
        sha256 = m["sha256"]
        apk_key = m["apkKey"]
        notes = m.get("notes") or ""
        if version_code <= 0 or size_bytes <= 0:
            raise ValueError("versionCode and sizeBytes must be positive")
        if not 0 <= min_version_code <= version_code:
            raise ValueError("minVersionCode must be between 0 and versionCode")
        if not isinstance(version_name, str) or not version_name.strip():
            raise ValueError("versionName must be a non-empty string")
        if not isinstance(sha256, str) or not _SHA256.match(sha256.lower()):
            raise ValueError("sha256 must be 64 hex characters")
        if not isinstance(apk_key, str) or not apk_key.startswith(KEY_PREFIX) \
                or not apk_key.endswith(".apk") or "//" in apk_key \
                or any(p in (".", "..") for p in apk_key.split("/")):
            # The key is presigned as-is. Only ever inside app-releases/, and canonical, so a
            # manifest cannot be used to mint a link to any other object in the bucket.
            raise ValueError(f"apkKey must be a canonical {KEY_PREFIX}*.apk key")
        if not isinstance(notes, str):
            raise ValueError("notes must be a string")
    except (KeyError, ValueError) as e:
        logger.warning("app-release manifest rejected: %s", e)
        return None
    return {
        "versionCode": version_code,
        "versionName": version_name.strip(),
        "minVersionCode": min_version_code,
        "sha256": sha256.lower(),
        "sizeBytes": size_bytes,
        "apkKey": apk_key,
        "notes": notes[:_MAX_NOTES],
    }


def response_for(manifest, presign, expires_in):
    """What GET /app/latest returns. `presign(key)` mints the download URL."""
    if manifest is None:
        return {"available": False}
    return {
        "available": True,
        "versionCode": manifest["versionCode"],
        "versionName": manifest["versionName"],
        "minVersionCode": manifest["minVersionCode"],
        "sha256": manifest["sha256"],
        "sizeBytes": manifest["sizeBytes"],
        "notes": manifest["notes"],
        "url": presign(manifest["apkKey"]),
        "expiresIn": expires_in,
    }
