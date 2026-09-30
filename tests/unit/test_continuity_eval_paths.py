"""Tests for scripts/continuity_eval/paths.py: the shared guard every continuity-eval write
goes through (spec: run output may carry raw session ids and prod customer transcript/item
text, so it must never land outside the gitignored `continuity_eval_runs/` root)."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.continuity_eval import paths


def test_require_run_dir_refuses_a_path_outside_continuity_eval_runs(tmp_path):
    outside = tmp_path / "not_the_gitignored_root" / "run1"
    with pytest.raises(SystemExit):
        paths.require_run_dir(outside)


def test_require_run_dir_accepts_a_path_under_continuity_eval_runs(tmp_path):
    inside = tmp_path / "continuity_eval_runs" / "run1"
    resolved = paths.require_run_dir(inside)
    assert resolved == inside.resolve()


def test_require_run_dir_accepts_continuity_eval_runs_as_a_non_root_component(tmp_path):
    # The component can appear anywhere in the path, not just as the immediate child of the
    # directory being resolved against -- e.g. a nested results file under a run directory.
    nested = tmp_path / "continuity_eval_runs" / "run1" / "results" / "summary.json"
    resolved = paths.require_run_dir(nested)
    assert resolved == nested.resolve()


def test_require_run_dir_error_message_names_the_offending_path(tmp_path):
    outside = tmp_path / "elsewhere"
    with pytest.raises(SystemExit) as exc_info:
        paths.require_run_dir(outside)
    assert "continuity_eval_runs" in str(exc_info.value)
    assert str(Path(outside).resolve()) in str(exc_info.value)
