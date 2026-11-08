"""The confirmation-email worker must build the Where column without psycopg
(Track L Task 7 Step 1).

SessionFinalizeFunction is OUTSIDE the VPC and has no psycopg layer. The place
comes from the pure `location_locate`, never from `repositories.location_markers`
(which imports psycopg). An import that reached psycopg would raise on every
real send -- so this builds the email, column and all, in a fresh interpreter
where `psycopg` cannot be imported (modelled on test_writer_needs_no_numpy: the
block lives in a subprocess so it cannot damage this one).
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROBE = r"""
import os, sys
sys.modules["psycopg"] = None
sys.modules["psycopg.rows"] = None
sys.path[:0] = [sys.argv[1]]
os.environ["EMAIL_WHERE_COLUMN"] = "true"
import lambda_session_finalize as fin
_, text, html = fin.build_confirmation_email(
    date="2026-11-09", open_todos=[{"text": "Strip the forms", "at": "09:05"}],
    location_markers=[{"at": "09:00", "location": "Level 2"}])
assert "| Strip the forms | Level 2 | — | — |" in text, text
assert html.count("<th ") == 4, html
assert "psycopg" not in [m for m in sys.modules if sys.modules[m] is not None]
print("ok")
"""


def test_the_where_column_builds_without_psycopg():
    out = subprocess.run([sys.executable, "-c", PROBE, os.path.join(ROOT, "src")],
                         capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.strip().endswith("ok")


def test_the_blocker_actually_blocks():
    """The guard on the guard: if setting the module to None stopped blocking,
    the test above would pass by importing the real psycopg."""
    probe = ('import sys\nsys.modules["psycopg"] = None\n'
             'try:\n    import psycopg\nexcept ImportError:\n    print("BLOCKED")\n')
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert "BLOCKED" in out.stdout, out.stdout + out.stderr
