"""Wiring test (prompt text only; model behaviour is measured on TEST).

A mixed question ("does what we did meet the standard?") is answered by the
records for the site side and by the web for the standard. The classifier used
to be asked whether the records ANSWER the question -- which a mixed question's
records never do, since the standard is not in them -- so compose never ran
(TEST, 2026-10-06). It is now asked whether the records state the site side."""
import pytest

wa = pytest.importorskip("web_answer")


def test_mixed_records_answer_means_the_site_side_is_present():
    p = wa.CLASSIFY_PROMPT
    mixed = p[p.index('- "mixed": true'):]
    assert "PROJECT side" in mixed
    assert "need not say anything about" in mixed


def test_project_records_answer_still_means_answered():
    p = wa.CLASSIFY_PROMPT
    project = p[p.index('- "project": true'):p.index('- "mixed": true')]
    assert "what they asked for from these" in project
