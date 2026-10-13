"""The finalize worker's decisions must reach the log in a real Lambda.

The Lambda runtime leaves the root logger at WARNING. Every choice this module
makes -- which source a final email's rows came from, why a brief request was
skipped -- is logged at INFO. The level used to be raised only as a side effect
of importing `email_sender`, which happens at SEND time, after those choices
were already logged. On TEST (2026-09-18) a final email went out and the line
saying whether it used the brief was dropped, while "[email:ses] sent" printed
right after it. A decision nobody can see is indistinguishable from one that
never ran.

Driven in a fresh interpreter with the root logger pinned to WARNING, because
pytest configures logging itself and would hide exactly this failure.
"""
import os
import subprocess
import sys

SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src")

SCRIPT = r"""
import logging, sys
logging.basicConfig(stream=sys.stdout, level=logging.WARNING, format="%(levelname)s %(message)s")
logging.getLogger().setLevel(logging.WARNING)   # what the Lambda runtime gives us
import lambda_session_finalize as f
f.SESSION_BRIEF = False
f._rows_from_brief_or_request(
    {"kind": "final", "sessionId": "abc", "folder": "Ben_Lin", "date": "2026-09-11"},
    [{"text": "x", "responsible": None, "due": None}])
"""


def test_a_final_emails_row_source_is_logged_without_email_sender_imported():
    env = dict(os.environ, SESSION_BRIEF="false", S3_BUCKET="x", PYTHONIOENCODING="utf-8")
    out = subprocess.run([sys.executable, "-c", SCRIPT], cwd=SRC, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    # The child script never imports email_sender, so nothing but this module can
    # have raised the level -- which is the condition the defect lived in.
    assert "email rows from the request" in out.stdout, (
        "the row-source decision was not logged at the Lambda's default level:\n"
        + out.stdout + out.stderr)
