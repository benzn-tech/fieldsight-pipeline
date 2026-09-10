"""Unit: the Word file and the JSON are two renderings of ONE report.

Until this, they disagreed. The JSON stopped carrying a per-topic timeline
because the device owner asked it to — "作为报告，不需要知道几点几点干了什么" —
and the .docx kept rendering one, complete with the `?` placeholders the JSON had
just dropped and the open questions the JSON had moved. A customer who downloads
the document and a customer who opens the dashboard were reading two different
accounts of the same day, which is worse than either being wrong alone. The
.docx is customer-facing: `lambda_fieldsight_api` serves it and files exist in
both buckets.

`render_sections_into` is driven here through a recording double rather than
python-docx, so these run wherever the suite runs. A skip would mean the one
assertion about the customer's Word file quietly stops being made.
"""
import pytest

rg = pytest.importorskip("lambda_report_generator")


class _Cell:
    def __init__(self):
        self.text = ""
        self.paragraphs = []


class _Row:
    def __init__(self, n):
        self.cells = [_Cell() for _ in range(n)]


class _Table:
    def __init__(self, cols):
        self.style = None
        self._cols = cols
        self.rows = [_Row(cols)]

    def add_row(self):
        row = _Row(self._cols)
        self.rows.append(row)
        return row


class _Doc:
    """Enough of python-docx to record what was asked for."""

    def __init__(self):
        self.headings = []
        self.paragraphs = []
        self.tables = []

    def add_heading(self, text, level=1):
        self.headings.append((level, text))

    def add_paragraph(self, text="", style=None):
        self.paragraphs.append(text)
        return _Para()

    def add_table(self, rows=1, cols=1):
        t = _Table(cols)
        self.tables.append(t)
        return t


class _Para:
    def add_run(self, text=""):
        return _Run()


class _Run:
    bold = False
    font = type("F", (), {"size": None, "color": type("C", (), {"rgb": None})()})()


SECTIONS = [
    {"title": "Summary", "kind": "narrative", "body": "A day on site."},
    {"title": "On Site", "kind": "kpi", "fields": ["recordings"],
     "values": {"recordings": 3}},
    {"title": "Actions", "kind": "table", "fields": ["action", "owner", "due", "priority"],
     "rows": [{"action": "Chase the steel", "owner": "Ben", "due": "Tomorrow",
               "priority": "high", "mentions": 2}]},
    {"title": "Open Questions", "kind": "list", "items": ["Who signs this off?"]},
    {"title": "Photos", "kind": "photos", "items": ["a.jpg", "b.jpg"]},
]


def _render(sections=SECTIONS):
    doc = _Doc()
    rg.render_sections_into(doc, sections)
    return doc


def test_the_document_has_no_timeline():
    doc = _render()
    assert not any("timeline" in h.lower() for _, h in doc.headings)


def test_every_section_becomes_a_heading():
    doc = _render()
    titles = [h for _, h in doc.headings]
    for expected in ("Summary", "Actions", "Open Questions", "Photos"):
        assert expected in titles, titles


def test_the_action_table_carries_its_columns_and_rows():
    doc = _render()
    assert doc.tables, "an Actions section must produce a table"
    table = doc.tables[0]
    header = [c.text for c in table.rows[0].cells]
    assert header == ["Action", "Owner", "Due", "Priority"]
    assert [c.text for c in table.rows[1].cells] == [
        "Chase the steel", "Ben", "Tomorrow", "high"]


def test_the_kpi_panel_is_not_repeated_in_the_document():
    """The session table at the top already states recordings, duration and
    photos. Saying it twice is how a reader starts wondering which to believe."""
    doc = _render()
    assert "On Site" not in [h for _, h in doc.headings]


def test_an_empty_section_writes_nothing_at_all():
    doc = _render([
        {"title": "Safety", "kind": "list", "items": []},
        {"title": "Summary", "kind": "narrative", "body": "   "},
        {"title": "Actions", "kind": "table", "fields": ["action"], "rows": []},
    ])
    assert doc.headings == []
    assert doc.tables == []


def test_a_none_value_in_a_row_is_blank_not_the_word_none():
    doc = _render([{"title": "Actions", "kind": "table",
                    "fields": ["action", "owner"],
                    "rows": [{"action": "Do it", "owner": None}]}])
    assert [c.text for c in doc.tables[0].rows[1].cells] == ["Do it", ""]


def test_an_unknown_kind_is_skipped_rather_than_crashing():
    """A template the Library grows a new kind for must not break the export."""
    doc = _render([{"title": "Gantt", "kind": "chart", "items": [1, 2]}])
    assert doc.headings == []


def test_sections_are_written_in_the_order_given():
    doc = _render()
    assert [h for _, h in doc.headings] == [
        "Summary", "Actions", "Open Questions", "Photos"]
