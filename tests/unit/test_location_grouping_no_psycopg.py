"""The Locations section must build where psycopg is not installed (Track L Task 6).

SessionReportFunction has no psycopg layer. `report_sections._locations`
imports `location_grouping` lazily inside a try/except, so if that import
reached psycopg the section would be silently absent on every real run --
an import of `report_sections` alone passes either way. So this calls the
builder with real markers and asserts it RETURNS a section, in a fresh
interpreter where `psycopg` and `psycopg.rows` cannot be imported.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROBE = r"""
import sys
sys.modules["psycopg"] = None
sys.modules["psycopg.rows"] = None
sys.path[:0] = [sys.argv[1]]
import location_grouping
import report_sections
markers = [{"at": "09:00", "location": "Level 1"}, {"at": "09:20", "location": "Level 2"}]
topics = [{"topic_title": "Formwork", "time_range": "09:05 - 09:10"},
          {"topic_title": "Rebar", "time_range": "09:25 - 09:30"}]
section = report_sections.locations_section(topics, markers)
assert section is not None, "built nothing"
assert [r["title"] for r in section["items"]] == ["Level 1", "Level 2"], section
assert "psycopg" not in [m for m in sys.modules if sys.modules[m] is not None]
print("ok")
"""


def test_the_locations_section_builds_without_psycopg():
    out = subprocess.run([sys.executable, "-c", PROBE, os.path.join(ROOT, "src")],
                         capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.strip().endswith("ok")


def test_locate_is_still_importable_from_the_repository_module():
    from repositories import location_markers
    import location_locate
    assert location_markers.locate is location_locate.locate
    assert location_markers.CARRY_MINUTES == location_locate.CARRY_MINUTES
