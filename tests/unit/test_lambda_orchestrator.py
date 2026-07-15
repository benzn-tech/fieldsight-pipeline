"""
Tests for src/lambda_orchestrator.py — NZ query-range timezone fix.

Style mirrors tests/unit/test_lambda_downloader.py: dummy AWS env vars (this
Lambda creates its lambda/S3 clients eagerly at import time), skip cleanly if
boto3 isn't installed.

Bug: the RealPTT query window was anchored to datetime.now(), which is UTC in
Lambda. RealPTT recordings are dated in NZ client time, so during the NZ
morning (UTC still on the previous calendar day) the window excluded the
current NZ day until UTC caught up (~NZ noon). The fix anchors the window to
NZ "now" by adding config['time_difference_ms'] (UTC->NZ offset) before taking
the date. These tests target the pure, extracted helper compute_query_range.
"""
import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

orch = pytest.importorskip("lambda_orchestrator", reason="requires boto3 (installed in CI)")

NZ_OFFSET_MS = 46800000  # +13h, config default


def test_range_anchors_to_nz_date_during_nz_morning():
    # UTC 2026-07-15 22:49 == NZ 2026-07-16 10:49 (the bug case: NZ morning,
    # UTC still on the previous calendar day).
    now_utc = datetime(2026, 7, 15, 22, 49, tzinfo=timezone.utc)

    start, end = orch.compute_query_range(now_utc, 1, NZ_OFFSET_MS)

    assert (start, end) == ('2026-07-15', '2026-07-16')


def test_range_end_is_today_when_utc_and_nz_same_date():
    # UTC 2026-07-16 03:00 == NZ 2026-07-16 16:00 (NZ afternoon, same
    # calendar date in both zones).
    now_utc = datetime(2026, 7, 16, 3, 0, tzinfo=timezone.utc)

    _, end = orch.compute_query_range(now_utc, 1, NZ_OFFSET_MS)

    assert end == '2026-07-16'


def test_range_spans_start_days_ago():
    now_utc = datetime(2026, 7, 16, 3, 0, tzinfo=timezone.utc)

    start, end = orch.compute_query_range(now_utc, 2, NZ_OFFSET_MS)

    start_dt = datetime.strptime(start, '%Y-%m-%d')
    end_dt = datetime.strptime(end, '%Y-%m-%d')
    assert (end_dt - start_dt).days == 2


def test_offset_pushes_across_midnight():
    # UTC 2026-07-15 11:30 + 13h == NZ 2026-07-16 00:30 — proves the NZ
    # offset is actually applied, not just echoing the UTC date (07-15).
    now_utc = datetime(2026, 7, 15, 11, 30, tzinfo=timezone.utc)

    _, end = orch.compute_query_range(now_utc, 1, NZ_OFFSET_MS)

    assert end == '2026-07-16'
