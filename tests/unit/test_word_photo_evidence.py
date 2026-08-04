"""Photos in the generated Word document.

`generate_word_document` is the one renderer behind both the nightly meeting
minutes and the on-demand session report. It had no picture code at all, so
every `related_photos` the extraction bound to a topic was dropped on the way
into the .docx — the document asserted things and showed none of them.

These tests open the produced file rather than inspecting the builder calls:
python-docx will happily accept an `add_picture` that never lands as a part,
and the question that matters is whether the bytes are in the document a site
manager downloads.
"""
from io import BytesIO
import zipfile

import pytest

docx = pytest.importorskip("docx", reason="python-docx layer not installed here")

from lambda_meeting_minutes import generate_word_document   # noqa: E402


# A 1x1 red PNG — the smallest thing python-docx will accept as a picture.
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c63f8cfc0000003010100c9fe92ef0000000049454e44ae"
    "426082"
)


def _minutes(topics):
    return {"meeting_date": "2026-07-25", "attendees": ["Ben"], "topics": topics}


def _topic(**over):
    t = {"topic_title": "Scaffold edge protection", "category": "safety",
         "summary": "Handrail missing on the east return.",
         "action_items": [{"action": "Fit handrail", "owner": "Ben",
                           "deadline": "Wed", "priority": "high"}]}
    t.update(over)
    return t


def _image_parts(buf):
    """The image parts actually written into the .docx package."""
    with zipfile.ZipFile(BytesIO(buf.getvalue())) as z:
        return [n for n in z.namelist() if n.startswith("word/media/")]


def test_a_topics_photo_reaches_the_document():
    buf = generate_word_document(
        _minutes([_topic(photo_streams=[BytesIO(PNG_1PX)])]), "Session report")
    assert len(_image_parts(buf)) == 1


def test_every_photo_on_a_topic_is_placed():
    buf = generate_word_document(
        _minutes([_topic(photo_streams=[BytesIO(PNG_1PX), BytesIO(PNG_1PX)])]),
        "Session report")
    # Two distinct streams with identical bytes: python-docx dedupes identical
    # images into one part, so assert on the DRAWINGS, not the parts.
    body = buf.getvalue()
    with zipfile.ZipFile(BytesIO(body)) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    assert xml.count("<w:drawing>") == 2


def test_the_photo_sits_inside_its_own_topic_after_the_action_items():
    """A strip at the end of the document would make the reader guess which
    claim each picture backs, which is most of what the picture was for."""
    buf = generate_word_document(_minutes([
        _topic(photo_streams=[BytesIO(PNG_1PX)]),
        _topic(topic_title="Second topic", action_items=[]),
    ]), "Session report")
    with zipfile.ZipFile(BytesIO(buf.getvalue())) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    assert xml.index("Fit handrail") < xml.index("<w:drawing>") < xml.index("Second topic")


def test_a_topic_without_photos_adds_no_empty_paragraph():
    buf = generate_word_document(_minutes([_topic()]), "Session report")
    assert _image_parts(buf) == []


def test_one_unplaceable_photo_does_not_lose_the_document():
    """A truncated JPEG, or a format python-docx does not know, costs its own
    picture — never the report, which is the actual deliverable."""
    buf = generate_word_document(_minutes([
        _topic(photo_streams=[BytesIO(b"not an image at all"), BytesIO(PNG_1PX)]),
    ]), "Session report")
    assert buf is not None
    assert len(_image_parts(buf)) == 1                      # the good one still landed
    with zipfile.ZipFile(BytesIO(buf.getvalue())) as z:
        assert b"Fit handrail" in z.read("word/document.xml")
