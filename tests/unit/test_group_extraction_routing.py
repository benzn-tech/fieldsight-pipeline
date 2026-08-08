"""Unit: routing and producing the MERGED extraction (Phase C, Task 5).

Two things are load-bearing here and both fail silently if got wrong:

  * A group request must not be mistaken for a solo one. Everything under
    extraction_requests/ currently flows into parse_final_request; a group
    artifact reaching it would extract the LEAD alone while the sweep believed
    the whole group had merged — and merged_at is already set, so it would
    never be looked at again.

  * The merged artifact must name each member's own extraction key exactly.
    item-writer deletes by that key, and delete_topics_for_source returns a
    rowcount rather than raising, so a key that differs by one character
    removes nothing and leaves the duplicate the merge exists to eliminate.
"""
import pytest

ex = pytest.importorskip("lambda_extract_session", reason="requires the lambda deps")

GID = "a" * 32
JOINER = "b" * 32


def _artifact():
    return {
        "groupId": GID,
        "leadSessionId": GID,
        "mergedKey": f"extractions/Ben_UCPK/2026-08-07/grp{GID}.json",
        "members": [
            {"userFolder": "Ben_UCPK", "date": "2026-08-07", "sessionBase": "sid" + GID},
            {"userFolder": "Sam_UCPK", "date": "2026-08-08", "sessionBase": "sid" + JOINER},
        ],
    }


def test_a_group_key_routes_to_the_group_path():
    assert ex.is_group_request(f"extraction_requests/group-{GID}.json") is True
    assert ex.is_group_request(f"extraction_requests/{GID}.json") is False
    assert ex.is_group_request(f"extraction_requests/sid{GID}.json") is False


def test_the_merged_artifact_names_every_member_key_exactly():
    keys = ex.merged_member_keys(_artifact())
    assert keys == [
        f"extractions/Ben_UCPK/2026-08-07/sid{GID}.json",
        f"extractions/Sam_UCPK/2026-08-08/sid{JOINER}.json",
    ], "each member uses its OWN date — a group can straddle NZ midnight"


def test_the_merged_key_is_not_the_leads_own_extraction_key():
    # The collision that would destroy the merge: the lead's final pass writes
    # blind, so landing after the merge it would overwrite the merged artifact,
    # and item-writer would then delete the MERGED topics as that key's previous
    # output — with the joiners' own topics already gone.
    art = _artifact()
    lead_solo = ex.extraction_key("Ben_UCPK", "2026-08-07", "sid" + GID)
    assert art["mergedKey"] != lead_solo
    assert lead_solo in ex.merged_member_keys(art), \
        "the lead's solo topics must still be among the ones deleted"


def test_a_group_artifact_is_not_parseable_as_a_solo_request(monkeypatch):
    # Belt and braces behind the routing order: the shapes differ, so a
    # mis-ordered check fails loudly instead of extracting the lead alone.
    monkeypatch.setattr(ex, "s3", lambda: _FakeS3(_artifact()))
    assert ex.parse_final_request("bkt", f"extraction_requests/group-{GID}.json") is None


def test_a_solo_request_still_parses(monkeypatch):
    solo = {"userFolder": "Ben_UCPK", "date": "2026-08-07", "sessionBase": "sid" + GID}
    monkeypatch.setattr(ex, "s3", lambda: _FakeS3(solo))
    parsed = ex.parse_final_request("bkt", f"extraction_requests/{GID}.json")
    assert parsed == ("Ben_UCPK", "2026-08-07", "sid" + GID, 0)


def test_the_handler_routes_a_group_key_to_extract_group(monkeypatch):
    seen = []
    monkeypatch.setattr(ex, "extract_group", lambda bucket, art: seen.append(art))
    monkeypatch.setattr(ex, "read_group_request", lambda bucket, key: _artifact())
    monkeypatch.setattr(ex, "extract_session",
                        lambda *a, **k: pytest.fail("a group must not take the solo path"))
    ex.lambda_handler({"Records": [{"s3": {"object": {
        "key": f"extraction_requests/group-{GID}.json"}}}]}, None)
    assert seen and seen[0]["groupId"] == GID


class _FakeS3:
    def __init__(self, payload):
        import json as _j
        self._body = _j.dumps(payload).encode("utf-8")

    def get_object(self, Bucket=None, Key=None):
        class _B:
            def __init__(self, b): self._b = b
            def read(self): return self._b
        return {"Body": _B(self._body)}
