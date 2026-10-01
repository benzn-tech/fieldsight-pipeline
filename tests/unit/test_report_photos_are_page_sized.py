"""Photographs go into a report at page size, so an inspection keeps all of them.

Owner, 2026-10-01 (TEST, Ben_Lin_test2): an inspection walk owns seven
photographs and the report carried four -- camera-size files (2-4 MB) under
a 12 MB budget and a per-topic cap of 4. They are now shrunk to
PHOTO_MAX_EDGE before they are counted, and the cap is 12.

THE test is `a camera-size photograph goes in at a fraction of its bytes`.
"""
import io

import pytest

import lambda_session_report as sr

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def jpeg(w, h, orientation=None):
    img = Image.effect_noise((w, h), 60).convert("RGB")      # noise: does not compress away
    out = io.BytesIO()
    exif = None
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
    img.save(out, "JPEG", quality=95, **({"exif": exif} if exif else {}))
    return out.getvalue()


def test_THE_a_camera_size_photograph_goes_in_at_a_fraction_of_its_bytes():
    big = jpeg(4000, 3000)
    small = sr._shrink(big)
    assert len(small) < len(big) / 4
    assert max(Image.open(io.BytesIO(small)).size) == sr.PHOTO_MAX_EDGE


def test_a_sideways_photograph_is_stood_up():
    small = sr._shrink(jpeg(3000, 2000, orientation=6))       # 6 = rotate 90 CW
    w, h = Image.open(io.BytesIO(small)).size
    assert h > w


def test_what_is_not_an_image_goes_in_unchanged():
    assert sr._shrink(b"not a picture") == b"not a picture"


def test_without_pillow_photographs_go_in_as_taken(monkeypatch):
    monkeypatch.setattr(sr, "Image", None)
    big = jpeg(2000, 1500)
    assert sr._shrink(big) == big


def test_a_small_photograph_is_not_made_bigger():
    tiny = jpeg(200, 150)
    assert sr._shrink(tiny) == tiny or len(sr._shrink(tiny)) < len(tiny)


def test_the_budget_is_spent_on_the_shrunk_bytes(monkeypatch):
    big = jpeg(4000, 3000)

    class S3:
        def get_object(self, Bucket, Key):
            return {"Body": io.BytesIO(big)}
    monkeypatch.setattr(sr, "s3", lambda: S3())
    budget = [sr.MAX_PHOTO_BYTES_TOTAL]
    streams = sr._fetch_photos("F", "2026-10-01", ["a.jpg"] * 7, budget)
    assert len(streams) == 7, "seven inspection photographs all fit"
    assert sr.MAX_PHOTO_BYTES_TOTAL - budget[0] == sum(len(s.getvalue()) for s in streams)
    assert all(len(s.getvalue()) < len(big) for s in streams)


def test_sixty_photographs_a_report_and_the_rest_are_counted(monkeypatch):
    """Owner, 2026-10-01: sixty a report, however they divide between topics;
    what does not fit is counted in the result, never silently dropped."""
    import datetime as dt
    tiny = jpeg(200, 150)

    class S3:
        def get_object(self, Bucket, Key):
            return {"Body": io.BytesIO(tiny)}
    monkeypatch.setattr(sr, "s3", lambda: S3())
    names = ["p%02d_13-25-%02d.jpg" % (i, i % 60) for i in range(70)]
    artifact = {"folder": "F", "date": "2026-10-01", "content": {"topics": [
        {"topic_title": "Walk", "time_range": "13:00 - 14:00", "related_photos": names[:50]},
        {"topic_title": "Pour", "time_range": "14:00 - 15:00", "related_photos": names[50:]}]}}
    budget = [sr.MAX_PHOTO_BYTES_TOTAL, sr.MAX_PHOTOS_PER_REPORT]
    offer, streams = sr._offered_topics(artifact, budget, dt.datetime(2026, 10, 1),
                                        dt.datetime(2026, 10, 1, 23, 59))
    assert len(streams["t0"]) == 50, "one topic may take far more than the old 12"
    assert len(streams["t1"]) == 10 and offer[1]["photos_left_out"] == 10
    assert sum(len(v) for v in streams.values()) == sr.MAX_PHOTOS_PER_REPORT == 60


def test_the_worker_spends_the_report_wide_count():
    """Wiring, pinned by source: the generate path hands the count to the walk."""
    import inspect
    assert "photo_budget = [MAX_PHOTO_BYTES_TOTAL, MAX_PHOTOS_PER_REPORT]" in \
        inspect.getsource(sr._generate_document)
