"""Unit: the in-app update manifest.

The device installs what this manifest describes, after checking the download against its sha256.
So the property that matters most is the negative one: a manifest that is malformed in any way
reads as "nothing to install", never as a half-described release.
"""
import json

import app_release as ar

GOOD = {
    "versionCode": 41,
    "versionName": "0.7.15",
    "minVersionCode": 40,
    "sha256": "a" * 64,
    "sizeBytes": 26_000_000,
    "apkKey": "app-releases/41/fieldsight-PROD-abc1234.apk",
    "notes": "Update prompts.",
}


def raw(**changes):
    m = dict(GOOD)
    for k, v in changes.items():
        if v is ...:
            m.pop(k, None)
        else:
            m[k] = v
    return json.dumps(m)


def test_a_complete_manifest_is_accepted():
    m = ar.parse_manifest(raw())
    assert m["versionCode"] == 41 and m["minVersionCode"] == 40
    assert m["apkKey"] == GOOD["apkKey"]


def test_min_version_defaults_to_zero():
    assert ar.parse_manifest(raw(minVersionCode=...))["minVersionCode"] == 0


def test_sha256_is_normalised_to_lowercase():
    assert ar.parse_manifest(raw(sha256="AB" * 32))["sha256"] == "ab" * 32


import pytest


@pytest.mark.parametrize("changes", [
    {"versionCode": ...},
    {"versionName": ...},
    {"sha256": ...},
    {"sizeBytes": ...},
    {"apkKey": ...},
    {"versionCode": 0},
    {"versionCode": "41"},
    {"versionCode": True},           # bool is an int in Python; JSON true is not a version
    {"sizeBytes": -1},
    {"minVersionCode": 42},          # above the version it ships with
    {"minVersionCode": -1},
    {"versionName": "   "},
    {"sha256": "z" * 64},
    {"sha256": "a" * 63},
    {"notes": 5},
])
def test_a_malformed_manifest_is_nothing_to_install(changes):
    assert ar.parse_manifest(raw(**changes)) is None


@pytest.mark.parametrize("key", [
    "users/Ben_Lin/video/2026-09-15/x.mp4",        # any other object in the bucket
    "app-releases/../users/Ben_Lin/x.apk",
    "app-releases/41/./x.apk",
    "app-releases//x.apk",
    "app-releases/41/x.zip",
    "/app-releases/41/x.apk",
])
def test_the_presigned_key_cannot_leave_app_releases(key):
    assert ar.parse_manifest(raw(apkKey=key)) is None


@pytest.mark.parametrize("text", ["not json", "[]", "null", "", None])
def test_unreadable_manifest_is_nothing_to_install(text):
    assert ar.parse_manifest(text) is None


def test_the_response_presigns_exactly_the_manifest_key():
    seen = []
    resp = ar.response_for(ar.parse_manifest(raw()), lambda k: seen.append(k) or "https://signed", 900)
    assert seen == [GOOD["apkKey"]]
    assert resp == {
        "available": True, "versionCode": 41, "versionName": "0.7.15", "minVersionCode": 40,
        "sha256": "a" * 64, "sizeBytes": 26_000_000, "notes": "Update prompts.",
        "url": "https://signed", "expiresIn": 900,
    }


def test_no_manifest_says_so_and_presigns_nothing():
    called = []
    assert ar.response_for(None, lambda k: called.append(k), 900) == {"available": False}
    assert called == []


def test_long_notes_are_trimmed():
    assert len(ar.parse_manifest(raw(notes="x" * 2000))["notes"]) == 500
