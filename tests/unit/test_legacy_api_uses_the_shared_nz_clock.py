"""Unit: the legacy gateway handler's default dates are NZ dates, DST and all.

The audit that prompted this corrected a claim of mine. I had said these three
literals sat in a lambda with zero invocations -- true of `fieldsight-api`
(0 calls in 14 days, reachable only through the retired gateway), but the SAME
handler is also deployed as `fieldsight-prod-api`, which served 1,227 calls in
the same fortnight, 129 of them `GET /api/timeline`. `get_timeline` derives
yesterday from the clock whenever `?date` is omitted, so the literal was live.

No wrong answer was actually served: measured traffic clusters at UTC hours
19/22/23/01 and the two offsets disagree about the date only in [11:00, 12:00).
That is luck, not design, and it is the kind of luck a schedule change or one
early-morning user removes.
"""
from datetime import datetime, timezone

import pytest

api = pytest.importorskip("lambda_fieldsight_api")


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        # 11:30 UTC on 6 September is 23:30 the SAME day in Auckland (NZST).
        # A fixed +13 makes it 00:30 on the 7th -- so "yesterday" becomes the
        # 6th instead of the 5th, and the caller gets the wrong day's timeline.
        return datetime(2026, 9, 6, 11, 30, tzinfo=timezone.utc)


def test_timeline_defaults_to_nz_yesterday(monkeypatch):
    """The date it computes is observed, not inferred.

    An admin with no ?user and no ?date takes the summary-report branch, whose
    S3 key carries the computed date -- so capturing the key is a direct read of
    the answer. An earlier version of this test called the handler inside a
    bare try/except and asserted nothing at all; it passed with the bug in.
    """
    monkeypatch.setattr(api.nz_time, "datetime", _Frozen)
    monkeypatch.setattr(api, "_any_folder_deleted_on", lambda date: False)

    seen = {}

    class _S3:
        class exceptions:
            NoSuchKey = KeyError

        def get_object(self, Bucket=None, Key=None):
            seen["key"] = Key
            raise KeyError("no summary")

    monkeypatch.setattr(api, "s3_client", _S3())
    monkeypatch.setattr(api, "find_any_report",
                        lambda date, caller: seen.setdefault("fallback_date", date))

    api.get_timeline({}, {"role": "admin", "company_id": "c"})

    # 11:30 UTC on the 6th is 23:30 the same day in NZ (NZST), so yesterday is
    # the 5th. A fixed +13 would have said the 6th.
    assert seen["key"].endswith("2026-09-05/summary_report.json"), seen["key"]
    assert seen["fallback_date"] == "2026-09-05"


def test_the_clock_it_reads_is_the_shared_one(monkeypatch):
    """Driven at the seam that matters: whatever get_timeline computes, it must
    come from nz_time, so a DST fix lands here too rather than being repeated."""
    monkeypatch.setattr(api.nz_time, "datetime", _Frozen)
    assert api.nz_time.nz_now() == datetime(2026, 9, 6, 23, 30)
    assert (api.nz_time.nz_now().date()).isoformat() == "2026-09-06"


def test_no_fixed_offset_remains_in_this_module():
    """A backstop, not the assertion: the two tests above pin behaviour. This
    one catches a fourth site being ADDED with the same literal, which is how
    all eleven of them appeared in the first place."""
    import inspect
    src = inspect.getsource(api)
    assert "hours=13" not in src
