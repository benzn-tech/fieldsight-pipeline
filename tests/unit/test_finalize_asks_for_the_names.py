"""Unit: a finished session is matched against the company's voiceprints without being asked.

## The gap

Until 2026-09-23 the only producer of a match request was `lambda_org_api.speaker_match`, an
endpoint no frontend has ever called. So naming a speaker propagated the name inside that ONE
meeting, and the next meeting started at `spk_0` again. "The system recognises Ben" described
a code path nothing reached — which is invisible from every direction, because the endpoint
works perfectly when you call it.

## What is tested here, and why this shape

The two producers now build the same artifact through `speaker_match_request`. This file
tests the SEAM rather than either half: the failure this repository keeps paying for is two
components agreeing on a contract neither stands on, each side's tests exercising its own
half, and both green while the hop between them drops a key. `label_map` has already been
lost exactly that way once, and label inheritance was then unreachable code that reported
success.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

try:
    # NOT importorskip: `speaker_match_request` is a module in this repository, so the only
    # ways this import fails are a wrong path or a moved file, and both should be red.
    # Skipped, this whole file reports as a passing suite while none of it has run — which
    # is how the guard on the boundary it protects reported green without ever running once.
    import speaker_match_request as smr
except ImportError as exc:  # pragma: no cover - the message IS the point
    raise AssertionError(f"src/speaker_match_request.py is not importable: {exc}")

SID = "sid" + "a" * 32
BASE = f"ben_ucpk2_2026-09-22_15-32-00_{SID}"


def turn(start, end, label="spk_0", chunk="c0000"):
    return {"source_filename": f"{BASE}_{chunk}_srcwav.json",
            "start_sec": start, "end_sec": end, "speaker_label": label}


def test_a_session_with_no_turns_asks_for_nothing():
    """An artifact that asks the embedder to match nothing still costs an invocation and
    still writes a log line that looks like work. It would make "the matcher ran and found
    nobody" and "there was nothing to run it on" the same observation."""
    assert smr.build("c1", BASE, "Ben_UCPK2", "2026-09-22", [], "shadow", "finalize") == []
    assert smr.build("c1", BASE, "Ben_UCPK2", "2026-09-22", None, "shadow", "finalize") == []


def test_a_recording_with_no_session_id_is_refused_loudly():
    """Legacy RealPTT recordings carry no `sid`. Guessing a session key reads another
    session's turns onto this one, which is worse than not matching at all."""
    with pytest.raises(ValueError, match="sid"):
        smr.build("c1", "RealPTT_2026-03-20_12-18-34", "X", "2026-03-20",
                  [turn(0, 10)], "shadow", "finalize")


def test_the_consumer_gets_every_field_it_derives_a_key_from():
    """`_from_match_artifact` raises when company_id / user_folder / date / session_base are
    missing, because it builds S3 keys from them and a missing one surfaces as a NoSuchKey
    far from its cause. Producing an artifact without them is the same defect one step
    earlier."""
    for missing in ("company_id", "user_folder", "date"):
        kwargs = {"company_id": "c1", "session_base": BASE, "user_folder": "Ben_UCPK2",
                  "date": "2026-09-22", "turns": [turn(0, 10)], "mode": "shadow",
                  "source": "finalize"}
        kwargs[missing] = None
        with pytest.raises(ValueError):
            smr.build(**kwargs)


def test_the_session_key_is_normalised_not_echoed():
    """Two spellings of a session are equal as sessions and not as strings. This repository
    has stored one spelling and queried the other twice in one night, one layer apart."""
    reqs = smr.build("c1", BASE, "Ben_UCPK2", "2026-09-22", [turn(0, 10)], "shadow", "finalize")
    assert reqs[0]["session_base"] == SID, "the filename spelling was passed through"


def test_the_label_map_is_whole_in_every_run():
    """Inheritance is the one thing in this chain whose answer for a turn depends on ANOTHER
    turn. `split_for_budget` chops on cumulative duration with no idea two turns share a
    label, so a group's long turn can land in run 1 and its short turns in run 2 — which
    then inherits nothing and reports no error."""
    turns = [turn(0, 2000, chunk="c0000"), turn(2000, 4000, chunk="c0001"),
             turn(4000, 4010, label="spk_1", chunk="c0002")]
    reqs = smr.build("c1", BASE, "Ben_UCPK2", "2026-09-22", turns, "shadow", "finalize",
                     seconds_per_run=2500)
    assert len(reqs) > 1, "the fixture must actually split, or this proves nothing"
    for r in reqs:
        assert len(r["label_map"]) == len(turns), "a run got a sliced label map"


def test_a_turn_is_never_split_across_runs():
    turns = [turn(i * 100, i * 100 + 100, chunk=f"c{i:04d}") for i in range(10)]
    reqs = smr.build("c1", BASE, "Ben_UCPK2", "2026-09-22", turns, "shadow", "finalize",
                     seconds_per_run=250)
    seen = [t for r in reqs for t in r["turns"]]
    assert seen == turns, "turns were reordered, duplicated or dropped by the split"


def test_the_artifact_says_which_producer_made_it():
    """When a wrong name appears or a company's costs jump, the first question is which
    producer made it, and nothing else in the artifact answers that."""
    api = smr.build("c1", BASE, "B", "2026-09-22", [turn(0, 10)], "on", "api",
                    requested_by="u-1")[0]
    fin = smr.build("c1", BASE, "B", "2026-09-22", [turn(0, 10)], "on", "finalize")[0]
    assert api["source"] == "api" and api["requested_by"] == "u-1"
    assert fin["source"] == "finalize"
    assert fin["requested_by"] is None, "finalize has no user — nobody asked"

    # Required, with no default. A default is right for exactly one caller and silently
    # wrong for the next one added, and the field would then give a confident wrong answer
    # to the only question it exists to answer.
    with pytest.raises(TypeError):
        smr.build("c1", BASE, "B", "2026-09-22", [turn(0, 10)], "on")
    with pytest.raises(ValueError, match="source"):
        smr.build("c1", BASE, "B", "2026-09-22", [turn(0, 10)], "on", "somewhere-else")


def test_an_unresolved_site_means_no_narrowing_not_a_crash():
    """`site_id=None` is a valid answer the matcher reads as "match against everybody in the
    company" — the behaviour before site narrowing existed, and the safe direction."""
    r = smr.build("c1", BASE, "B", "2026-09-22", [turn(0, 10)], "shadow", "finalize",
                  site_id=None)[0]
    assert r["site_id"] is None
    r2 = smr.build("c1", BASE, "B", "2026-09-22", [turn(0, 10)], "shadow", "finalize",
                   site_id=7)[0]
    assert r2["site_id"] == "7", "the site id must be a string — it travels as JSON"


def test_org_api_and_the_writer_build_the_identical_shape():
    """The seam. Both halves call this module, and the assertion is that org-api's private
    helpers are now delegations rather than a second implementation — because a map two
    producers compute separately is a map that drifts, and the drift is silent."""
    src = open(os.path.join(ROOT, "src", "lambda_org_api.py"), encoding="utf-8").read()
    for name in ("_label_map", "_split_for_budget"):
        body = src[src.index(f"def {name}("):]
        body = body[:body.index("\ndef ", 1)]
        assert "speaker_match_request." in body, (
            f"{name} has a second implementation again; the finalize producer will drift "
            f"from it and nothing will fail")


def test_the_finalize_producer_is_switched_separately_from_the_rebind():
    """Three switches, three different things. The re-bind is anonymous and names nobody;
    matching puts a person's name on a passage and costs ONNX per finalized session; the
    mode is the whole feature's rollback. Collapsing any two means the only way to stop
    paying for one is to turn off the other."""
    src = open(os.path.join(ROOT, "src", "lambda_item_writer.py"), encoding="utf-8").read()
    assert 'MATCH_ON_FINALIZE = os.environ.get("MATCH_ON_FINALIZE"' in src
    assert 'REBIND_SPEAKERS = os.environ.get("REBIND_SPEAKERS"' in src
    body = src[src.index("def _request_match("):]
    body = body[:body.index("\ndef ", 1)]
    assert "if not MATCH_ON_FINALIZE" in body, "the switch does not gate the producer"
    assert 'SPEAKER_IDENTITY_MODE == "off"' in body, (
        "the producer queues work even when naming is switched off, which leaves the "
        "feature's rollback meaning nothing")


def test_the_switch_is_wired_all_three_segments():
    """A switch is only real when the repo variable, the workflow override and the template
    Parameter all exist. This repository has shipped a documented rollback that was never
    wired, twice."""
    tpl = open(os.path.join(ROOT, "src", "template.yaml"), encoding="utf-8").read()
    assert "MatchOnFinalize:" in tpl, "no template Parameter"
    assert "MATCH_ON_FINALIZE: !Ref MatchOnFinalize" in tpl, "the env is not wired"
    assert "SPEAKER_IDENTITY_MODE: !Ref SpeakerIdentityMode" in tpl
    for wf, var in ((".github/workflows/deploy.yml", "TEST"),
                    (".github/workflows/deploy-prod.yml", "PROD")):
        text = open(os.path.join(ROOT, wf), encoding="utf-8").read()
        assert f"MatchOnFinalize=${{{{ vars.{var}_MATCH_ON_FINALIZE" in text, (
            f"{wf} never passes the parameter, so the variable is decoration")
