"""lambda_extract_session._dedup_turn_boundaries — removes the ~2s chunk-overlap
duplication the mobile chunk-session contract introduces, ONLY at a device seam
(adjacent turns whose time ranges overlap). A no-op on non-overlapping (legacy /
VAD / sequential) turns, which is what keeps the pre-chunk pipeline unchanged."""
import os
from datetime import datetime, timedelta

import pytest

# ANTHROPIC_API_KEY (and the AWS dummies) are read ONCE at import time; this file
# sorts before test_lambda_extract_session, so it may import the module first —
# set the same dummy env now or the module caches an unset key and every
# extract_session() call skips (mirrors that file's own top-of-module setup).
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")

les = pytest.importorskip("lambda_extract_session")

_BASE = datetime(2026, 7, 25, 10, 0, 0)


def T(text, start_s, end_s, speaker="spk_0"):
    st = _BASE + timedelta(seconds=start_s)
    en = _BASE + timedelta(seconds=end_s)
    return {
        "speaker": speaker, "text": text,
        "abs_start": st, "abs_end": en,
        "abs_start_str": st.strftime("%H:%M:%S"),
    }


def test_trims_the_overlap_run_on_a_time_seam():
    # segment N tail "...pour the slab"; segment N+1 head "the slab needs..." —
    # B starts (38) before A ends (40): a device seam. The shared "the slab" goes.
    a = T("morning pour the slab", 10, 40)
    b = T("the slab needs rebar", 38, 60)
    out = les._dedup_turn_boundaries([a, b])
    assert [t["text"] for t in out] == ["morning pour the slab", "needs rebar"]


def test_no_op_when_turns_are_sequential_not_overlapping():
    # B starts (41) at/after A ends (40): normal back-to-back speech, NOT a seam —
    # even a coincidental shared word must NOT be trimmed.
    a = T("pour the slab", 10, 40)
    b = T("the slab needs", 41, 60)
    out = les._dedup_turn_boundaries([a, b])
    assert [t["text"] for t in out] == ["pour the slab", "the slab needs"]


def test_drops_a_fully_overlapping_turn():
    a = T("check the slab", 10, 40)
    b = T("the slab", 38, 42)            # entirely a repeat of A's tail
    out = les._dedup_turn_boundaries([a, b])
    assert [t["text"] for t in out] == ["check the slab"]


def test_case_and_punctuation_insensitive_at_the_seam():
    a = T("pour the Slab.", 10, 40)
    b = T("slab then cure", 38, 60)
    out = les._dedup_turn_boundaries([a, b])
    assert [t["text"] for t in out] == ["pour the Slab.", "then cure"]


def test_does_not_mutate_the_input_turns():
    a = T("check the slab", 10, 40)
    b = T("the slab cracked", 38, 60)
    les._dedup_turn_boundaries([a, b])
    assert b["text"] == "the slab cracked"   # caller's object untouched (copy-on-trim)


def test_single_and_empty():
    assert les._dedup_turn_boundaries([]) == []
    one = [T("solo", 10, 20)]
    assert les._dedup_turn_boundaries(one) == one
