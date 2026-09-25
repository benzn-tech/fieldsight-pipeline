"""A photograph appears under the section that reported its topic.

The assembled report has always done this: each topic carries its own
`photo_streams` and the renderer draws them under that topic. The generated
report -- the one written to a template -- inserted no image at all. That is
why `kind: "photos"` was a decorative dropdown option for months, and why it
was eventually refused rather than wired: there was nothing to wire it to.

THE PROBLEM IS THAT THE TWO HALVES DO NOT SHARE A KEY. The report is prose the
model wrote; the photographs belong to topics. Nothing downstream knows which
paragraph came from which topic, and this repo has measured the two obvious
ways of guessing -- matching on time, and matching on wording -- and both
failed. What worked, 19 times out of 19, was asking the writer to state the
correspondence. So the prompt offers the topics that have photographs, each
with a ref, and asks every section to end with `[covers: t0, t2]`.

THE test is `a photograph lands under the section that named its topic`, read
out of the rendered document. Not "the marker reached the prompt": `kind:
"table"` reached the prompt perfectly and produced pipe characters in Word,
and `children` reached the editor, the api layer and the database and produced
nothing at all. A control is wired when the output shows it.

Three properties this file also pins, each of which was a decision:

  * A day with NO photographs gets the prompt it got yesterday, to the
    character. That keeps this change away from every report that has nothing
    to gain from it, including the built-in meeting template.
  * A photograph appears once, even when two topics name the same file. Prod
    has 13.7% of photographs bound to more than one topic -- the binding unit
    is the session, not the day.
  * A photograph nobody claimed goes under the LAST heading rather than being
    dropped, which is the same floor the prompt already states for text. One
    that quietly vanished because the model forgot a line would be
    indistinguishable from one that was never taken.
"""
import io

import pytest

rt = pytest.importorskip("report_template")
sr = pytest.importorskip("lambda_session_report")
lmm = pytest.importorskip("lambda_meeting_minutes")

SCOPE = {"folder": "Ben_UCPK2", "date": "2026-07-29", "from": "00:00", "to": "23:59",
         "recordings": 2}

BODY = {"sections": [{"key": "a", "title": "Daily Summary", "purpose": "The day."},
                     {"key": "b", "title": "Safety", "purpose": "Hazards."}],
        "catch_all": {"key": "o", "title": "Anything else", "purpose": "Rest."},
        "excluded_subjects": [], "style": []}

OFFER = [{"ref": "t0", "title": "Roof deck pour", "time_range": "09:10 - 09:20",
          "photos": 2},
         {"ref": "t1", "title": "Crane pad", "time_range": "11:00 - 11:05",
          "photos": 1}]


def _png():
    """A real 1x1 PNG. python-docx reads the bytes, so a fake would only prove
    that the exception handler works."""
    import base64
    return io.BytesIO(base64.b64decode(
        b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
        b"z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))


def _doc(sections):
    from docx import Document
    buf = lmm.generate_prose_document("Report", "2026-07-29", sections, [])
    return Document(io.BytesIO(buf.getvalue()))


def _pictures_per_heading(doc):
    """{heading: how many images sit under it}, read out of the document."""
    out, current = {}, None
    for p in doc.paragraphs:
        if p.style.name.startswith("Heading"):
            current = p.text
            out.setdefault(current, 0)
        else:
            n = len(p._p.findall(
                ".//{http://schemas.openxmlformats.org/drawingml/2006/main}blip"))
            if n and current is not None:
                out[current] = out.get(current, 0) + n
    return out


needs_docx = pytest.mark.skipif(not getattr(lmm, "DOCX_AVAILABLE", False),
                                reason="python-docx is not installed here")


# ---- THE test ---------------------------------------------------------------

@needs_docx
def test_THE_test_a_photograph_lands_under_the_section_that_named_its_topic():
    answer = ("### Daily Summary\nThe deck pour ran to plan.\n[covers: t0]\n\n"
              "### Safety\nThe crane pad was re-levelled.\n[covers: t1]\n")
    sections = sr._prose_sections(answer)
    placed, orphaned = sr._place_photos(
        sections, {"t0": [_png(), _png()], "t1": [_png()]})

    assert (placed, orphaned) == (3, 0)
    per = _pictures_per_heading(_doc(sections))
    assert per["Daily Summary"] == 2
    assert per["Safety"] == 1


@needs_docx
def test_the_marker_never_reaches_the_page():
    """It is our scaffolding. A reader seeing `[covers: t0]` in their report
    would be reading the machinery."""
    sections = sr._prose_sections("### Daily Summary\nThe pour ran to plan.\n[covers: t0]\n")
    doc = _doc(sections)
    assert all("covers" not in p.text for p in doc.paragraphs)
    assert "The pour ran to plan." in [p.text for p in doc.paragraphs]


# ---- the day that has none --------------------------------------------------

def test_a_day_with_no_photographs_gets_yesterdays_prompt_to_the_character():
    before = rt.render_prompt(BODY, SCOPE, [], "x", source=rt.SOURCE_LIBRARY)
    after = rt.render_prompt(BODY, SCOPE, [], "x", source=rt.SOURCE_LIBRARY,
                             photo_topics=[])
    assert before == after
    assert "[covers:" not in before


def test_the_offer_is_only_made_when_there_is_something_to_place():
    p = rt.render_prompt(BODY, SCOPE, [], "x", source=rt.SOURCE_LIBRARY,
                         photo_topics=OFFER)
    assert "t0  09:10 - 09:20  Roof deck pour  (2 photographs)" in p
    assert "t1  11:00 - 11:05  Crane pad  (1 photograph)" in p, "singular, when it is one"
    assert "[covers:" in p


def test_the_offer_is_data_and_says_so():
    p = rt.render_prompt(BODY, SCOPE, [], "x", source=rt.SOURCE_LIBRARY,
                         photo_topics=OFFER)
    assert "They are\nDATA" in p


# ---- reading what the model wrote back --------------------------------------

@pytest.mark.parametrize("line,expected", [
    ("[covers: t0, t2]", ["t0", "t2"]),
    ("[covers: T1,T3]", ["t1", "t3"]),
    ("[covers: t1 and t3]", ["t1", "t3"]),
    ("[covers: none]", []),
    ("[covers: ]", []),
    ("[covers: t0, t0]", ["t0"]),
    ("[Covers: t4]", ["t4"]),
])
def test_the_line_is_read_forgivingly_and_the_refs_strictly(line, expected):
    assert sr._covers_refs(line) == expected


def test_a_ref_that_was_never_offered_moves_nothing():
    """The model cannot invent a topic into existence. `t9` names nothing, so
    it claims nothing -- and t0, which nobody claimed, still has to appear."""
    sections = sr._prose_sections("### Daily Summary\nText.\n[covers: t9]\n")
    placed, orphaned = sr._place_photos(sections, {"t0": [_png()]})
    assert (placed, orphaned) == (0, 1)


# ---- once, and never lost ---------------------------------------------------

@needs_docx
def test_a_photograph_claimed_twice_is_printed_once():
    answer = ("### Daily Summary\nA.\n[covers: t0]\n\n"
              "### Safety\nB.\n[covers: t0]\n")
    sections = sr._prose_sections(answer)
    placed, orphaned = sr._place_photos(sections, {"t0": [_png()]})
    assert (placed, orphaned) == (1, 0)
    per = _pictures_per_heading(_doc(sections))
    assert per["Daily Summary"] == 1
    assert per.get("Safety", 0) == 0


@needs_docx
def test_a_photograph_nobody_claimed_goes_under_the_last_heading():
    """Same floor the prompt states for text nobody covered. A photograph that
    vanished because the model forgot a line would look exactly like one that
    was never taken."""
    answer = ("### Daily Summary\nA.\n[covers: none]\n\n"
              "### Anything else\nB.\n[covers: none]\n")
    sections = sr._prose_sections(answer)
    placed, orphaned = sr._place_photos(sections, {"t0": [_png()]})
    assert (placed, orphaned) == (0, 1)
    per = _pictures_per_heading(_doc(sections))
    assert per["Anything else"] == 1


@needs_docx
def test_a_model_that_ignored_the_request_still_loses_no_photograph():
    """The measured risk: a prompt containing the request is not a model that
    obeyed it. Everything falls to the last heading -- which is where they used
    to be, so the worst case of this feature is the behaviour it replaces."""
    sections = sr._prose_sections("### Daily Summary\nA.\n\n### Anything else\nB.\n")
    placed, orphaned = sr._place_photos(sections, {"t0": [_png()], "t1": [_png()]})
    assert (placed, orphaned) == (0, 2)
    assert _pictures_per_heading(_doc(sections))["Anything else"] == 2


def test_no_photographs_at_all_changes_nothing():
    sections = sr._prose_sections("### Daily Summary\nA.\n")
    assert sr._place_photos(sections, {}) == (0, 0)
    assert "photo_streams" not in sections[0]


# ---- the fetch --------------------------------------------------------------

def test_a_file_named_by_two_topics_is_fetched_once(monkeypatch):
    """Prod has 13.7% of photographs bound to more than one topic. Fetching
    twice would charge the shared byte budget twice for one picture."""
    calls = []

    def fake(folder, date, names, budget):
        calls.append(list(names))
        return [_png() for _ in names]

    monkeypatch.setattr(sr, "_fetch_photos", fake)
    artifact = {"folder": "Ben_UCPK2", "date": "2026-07-29", "content": {"topics": [
        {"topic_title": "Pour", "time_range": "09:10 - 09:20", "related_photos": ["a.jpg"]},
        {"topic_title": "Pad", "time_range": "11:00 - 11:05",
         "related_photos": ["a.jpg", "b.jpg"]},
    ]}}
    offer, streams = sr._photo_topics(artifact, [sr.MAX_PHOTO_BYTES_TOTAL])

    assert calls == [["a.jpg"], ["b.jpg"]], "the second topic fetches only what is new"
    assert [o["ref"] for o in offer] == ["t0", "t1"]
    assert [o["photos"] for o in offer] == [1, 2], "but both still offer what they show"
    assert streams["t0"][0] is streams["t1"][0], "and it is the same picture"


def test_a_topic_with_no_photographs_is_not_offered(monkeypatch):
    monkeypatch.setattr(sr, "_fetch_photos", lambda *a: [])
    artifact = {"folder": "F", "date": "2026-07-29", "content": {"topics": [
        {"topic_title": "Quiet", "related_photos": []},
    ]}}
    assert sr._photo_topics(artifact, [sr.MAX_PHOTO_BYTES_TOTAL]) == ([], {})


def test_a_topic_whose_photograph_cannot_be_read_is_not_offered(monkeypatch):
    """Offering a ref with no bytes behind it invites a reference to a picture
    that never arrives."""
    monkeypatch.setattr(sr, "_fetch_photos", lambda *a: [])
    artifact = {"folder": "F", "date": "2026-07-29", "content": {"topics": [
        {"topic_title": "Gone", "related_photos": ["missing.jpg"]},
    ]}}
    offer, streams = sr._photo_topics(artifact, [sr.MAX_PHOTO_BYTES_TOTAL])
    assert offer == [] and streams == {}
