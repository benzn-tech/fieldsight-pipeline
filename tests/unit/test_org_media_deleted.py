"""Unit: a deleted recording stops being playable.

Plan: docs/superpowers/plans/2026-08-14-user-deletes-a-recording.md phase 8.

The most literal derived artifact of all, and the one the spec missed entirely. Four
endpoints list and presign the deleted recording's OWN S3 objects with no topic involved:

    /transcripts   /audio-segments   /video-segments   /media/presigned-url

Every protection built so far routes through topics. None of them touches these. A
customer who deletes a recording and can still press play on it has been told something
untrue, which is precisely the trust failure the request named.

The objects stay on S3 — that is the whole premise. What changes is who may reach them.
"""
import ast
import os
import re

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src")
ORG = os.path.join(SRC, "lambda_org_api.py")

MEDIA_READERS = ("_read_org_transcripts", "_read_org_audio_segments",
                 "_read_org_video_segments", "get_org_media_presigned_url")


def _fn(name):
    text = open(ORG, encoding="utf-8").read()
    tree = ast.parse(text)
    lines = text.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return "\n".join(lines[node.lineno - 1:(node.end_lineno or node.lineno)])
    return None


def test_every_media_reader_consults_the_tombstones():
    """The property. These four are the whole surface — a fifth added later without the
    check would make the delete a lie again, and this is what refuses it."""
    missing = [n for n in MEDIA_READERS
               if not re.search(r"_deleted_sessions_for_day|_presign_target_is_deleted|"
                                r"deleted_source_prefixes|is_source_deleted",
                                _fn(n) or "")]
    assert not missing, (
        "these serve a deleted recording's own media with no tombstone check, so the "
        "customer can still play what they deleted: " + ", ".join(missing))


def test_the_readers_can_actually_reach_the_database():
    """org-api is in-VPC and holds a connection — but these four were written as pure S3
    helpers and take no `conn`. A check that cannot query is a check that cannot fire, and
    it would fail open forever without ever saying so."""
    for name in MEDIA_READERS:
        body = _fn(name) or ""
        sig = body.split("\n")[0]
        assert "conn" in sig, f"{name} has no connection to check with: {sig}"


def test_a_filtered_media_list_says_how_many_it_dropped():
    """Zero included. 'The list was served' and 'the exclusion ran' are otherwise the
    same observation — the shape that hid three separate silent failures in this repo."""
    text = open(ORG, encoding="utf-8").read()
    assert re.search(r"media.{0,40}(deleted|tombston).{0,80}(dropped|excluded|%d)",
                     text, re.I | re.S), "no count is logged anywhere in the media path"
