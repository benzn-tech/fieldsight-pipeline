"""Report bench, Task 4: candidates pinned by rendered-prompt hash.

The always-on checks read the committed files. The re-render check needs every
candidate commit (CI checks out depth 1 -> skipped there); locally it renders
the 7 candidates cold from `git archive` snapshots, which is slow on purpose.
"""
import importlib.util
import os
import sys
import tempfile

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BENCH_DIR = os.path.join(REPO, "tools", "report-bench")


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(BENCH_DIR, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


common = _load("rb_common")
snap = _load("rb_snapshot")
inv = _load("rb_inventory")
CANDS = common.read_json(inv.CANDIDATES)
IDS = ["X-0901", "X-0917", "X-1005", "X-cur", "T-0928", "T-1001", "T-cur"]


def _prompt(cid):
    with open(os.path.join(inv.PROMPTS, cid + ".txt"), encoding="utf-8", newline="") as f:
        return f.read()


def test_every_candidate_is_pinned():
    assert set(IDS) <= set(CANDS)
    for cid in IDS:
        assert common.sha256_text(_prompt(cid)) == CANDS[cid]["fixed_sha256"], cid
        assert CANDS[cid]["task"] == cid[0]


def test_hashes_pairwise_distinct():
    for task in ("X", "T"):
        hs = [CANDS[c]["fixed_sha256"] for c in IDS if c.startswith(task)]
        assert len(set(hs)) == len(hs), task


def test_x0901_without_names():
    assert CANDS["X-0901"]["names"] is False
    assert all(CANDS[c]["names"] for c in IDS if c != "X-0901")


def test_candidate_shas_match_the_spec():
    assert {c: CANDS[c]["sha"] for c in IDS} == {
        "X-0901": "763b3082", "X-0917": "83abd0d4", "X-1005": "8703e69a", "X-cur": "82036cf2",
        "T-0928": "e4e2ea19", "T-1001": "751481a3", "T-cur": "develop"}


def test_rerender_matches_pinned_hash():
    if not all(snap.have_sha(CANDS[c]["resolved"]) for c in IDS):
        pytest.skip("shallow clone: candidate commits not present")
    cache = os.environ.get("REPORT_BENCH_CACHE") or os.path.join(
        tempfile.gettempdir(), "report-bench-snapshots")
    for cid in IDS:
        e = CANDS[cid]
        prompt = inv.render_at(e["resolved"], e["task"], cache, e["names"])
        assert common.sha256_text(prompt) == e["fixed_sha256"], cid
