"""The nightly daily report carries photographs by the same rules as the
template report (report_photos; owner, 2026-10-01), so one day's two
documents never disagree about which pictures it had.

THE test is `past 120 the nightly report says how many, not their filenames`.
"""
import io
import json

import pytest

docx = pytest.importorskip("docx")
PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

import lambda_report_generator as lrg  # noqa: E402


def jpeg():
    out = io.BytesIO()
    Image.new("RGB", (300, 200), "grey").save(out, "JPEG")
    return out.getvalue()


class NoSuchKey(Exception):
    pass


class S3:
    exceptions = type("E", (), {"NoSuchKey": NoSuchKey})

    def __init__(self, excluded=None):
        self.excluded = excluded
        self.fetched = []

    def get_object(self, Bucket, Key):
        if Key.startswith("report_photo_selection/"):
            if self.excluded is None:
                raise NoSuchKey(Key)
            return {"Body": io.BytesIO(json.dumps({"excluded": self.excluded}).encode())}
        self.fetched.append(Key)
        return {"Body": io.BytesIO(jpeg())}


def render(monkeypatch, n, excluded=None):
    s3 = S3(excluded)
    monkeypatch.setattr(lrg, "s3_client", s3)
    monkeypatch.setattr(lrg, "S3_BUCKET", "bucket")
    doc = docx.Document()
    items = [{"name": "p%03d.jpg" % i, "key": "users/F/pictures/2026-10-01/p%03d.jpg" % i}
             for i in range(n)]
    lrg._add_photos_to_document(doc, items)
    texts = [p.text for p in doc.paragraphs if p.text]
    return doc, texts, s3


def test_THE_past_120_the_nightly_report_says_how_many_not_their_filenames(monkeypatch):
    doc, texts, s3 = render(monkeypatch, 130)
    assert len(doc.inline_shapes) == 120 and len(s3.fetched) == 120
    assert texts[-1] == "Photographs: 130 taken this day; 120 included in this report."
    assert "p125.jpg" not in texts, "no filenames for the ones left out"


def test_ninety_all_go_in(monkeypatch):
    doc, texts, _ = render(monkeypatch, 90)
    assert len(doc.inline_shapes) == 90
    assert not any(t.startswith("Photographs:") for t in texts)


def test_the_persons_exclusions_come_off(monkeypatch):
    doc, texts, s3 = render(monkeypatch, 5, excluded=["p001.jpg", "p003.jpg"])
    assert len(doc.inline_shapes) == 3
    assert not any(k.endswith(("p001.jpg", "p003.jpg")) for k in s3.fetched)
    assert "p001.jpg" not in texts


def test_a_name_without_a_key_is_still_listed(monkeypatch):
    monkeypatch.setattr(lrg, "S3_BUCKET", "bucket")
    doc = docx.Document()
    lrg._add_photos_to_document(doc, ["a.jpg", "b.jpg"])
    assert [p.text for p in doc.paragraphs] == ["a.jpg", "b.jpg"]
