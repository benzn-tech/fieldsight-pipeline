"""Unit: the updated email has somewhere to go.

`_enqueue_updated_emails` wrote {kind, sessionId, groupId, summary, openTodos}
and nothing else. `process_finalize_request` skips any request whose recipient
is empty -- so every one of these was silently dropped, with the result
`{"status": "skipped", "reason": "no recipient"}` and no email.

The existing tests did not catch it because they build the artifact by hand and
put a recipient in it (`test_updated_email.py`), which tests the consumer
against a payload the producer never emits. That is the shape of gap worth
naming: a contract tested only from one side is not tested.

It matters now rather than later: `PROD_ENABLE_GROUP_MERGE=true` went on the
night of 2026-08-10/11. Nothing breaks until the first two-device recording,
and then the one thing the merge promises -- every member gets the same record
-- fails without an error anywhere.

The email also renders a date, a time range and a site name. Those were missing
too, so even a recipient alone would have produced an email headed by blanks.
"""
import pytest

iw = pytest.importorskip("lambda_item_writer", reason="requires the lambda deps")

GID = "g" * 32
S1, S2 = "1" * 32, "2" * 32


def _artifact():
    return {"tier": "group", "groupId": GID, "memberSessions": [S1, S2],
            "summary": "The merged summary.",
            "topics": [{"topic_title": "Slab",
                        "action_items": [{"action": "renew the cards",
                                          "responsible": "Ben",
                                          "deadline": "This week"}]}]}


def _contexts():
    return {S1: {"recipient": "a@example.com", "date": "2026-08-07",
                 "timeRange": "14:17–15:20", "siteName": "UC PK"},
            S2: {"recipient": "b@example.com", "date": "2026-08-07",
                 "timeRange": "14:17–15:20", "siteName": "UC PK"}}


def _capture():
    written = {}
    return written, lambda key, body: written.__setitem__(key, body)


def test_every_member_request_carries_a_recipient():
    written, put = _capture()
    iw._enqueue_updated_emails(_artifact(), _contexts(), put=put)
    assert len(written) == 2
    for body in written.values():
        assert body.get("recipient"), \
            "no recipient means process_finalize_request drops it in silence"


def test_the_request_carries_what_the_email_renders():
    # build_confirmation_email takes date, time_range, site_name. Without them
    # the member gets an email headed by blanks -- delivered, and wrong.
    written, put = _capture()
    iw._enqueue_updated_emails(_artifact(), _contexts(), put=put)
    body = written[f"session_finalize_requests/{S1}-updated.json"]
    assert body["date"] == "2026-08-07"
    assert body["timeRange"] == "14:17–15:20"
    assert body["siteName"] == "UC PK"


def test_the_merged_todos_still_ride_along():
    written, put = _capture()
    iw._enqueue_updated_emails(_artifact(), _contexts(), put=put)
    body = written[f"session_finalize_requests/{S1}-updated.json"]
    assert body["summary"] == "The merged summary."
    assert body["openTodos"] == [{"text": "renew the cards",
                                  "responsible": "Ben", "due": "This week"}]


def test_a_member_with_no_email_is_skipped_loudly_not_silently(caplog):
    ctx = _contexts()
    ctx[S2]["recipient"] = ""
    written, put = _capture()
    with caplog.at_level("WARNING"):
        iw._enqueue_updated_emails(_artifact(), ctx, put=put)
    assert f"session_finalize_requests/{S1}-updated.json" in written
    assert f"session_finalize_requests/{S2}-updated.json" not in written, \
        "a request that cannot be delivered should not be written at all"
    assert S2[:8] in caplog.text or S2 in caplog.text, \
        "which member has no email is the one thing an operator needs"


def test_a_member_with_no_resolved_context_is_skipped():
    written, put = _capture()
    iw._enqueue_updated_emails(_artifact(), {S1: _contexts()[S1]}, put=put)
    assert len(written) == 1, "an unresolvable member must not get a blank email"
