"""Unit: a switch you cannot set is not a switch.

A boolean CloudFormation Parameter is how this repo ships a change that can be
turned off without a code change — that is the stated rollback for the loudness
normalisation, the silence drop, the VAD threshold and the audio-event tag
filter. The mechanism has three parts, and the middle one is easy to forget:

    repo variable  ->  workflow --parameter-overrides  ->  template Parameter

Miss the workflow line and the Parameter silently takes its default forever.
Setting the value on the live Lambda appears to work and is then erased by the
next CloudFormation reconcile, so the failure surfaces days later as "we turned
it off and it came back".

That is not hypothetical: it shipped on 2026-08-08. `FILTER_AUDIO_EVENT_TAGS`
was documented in its own PR as the rollback while no workflow passed it.
`TRANSCRIBE_WHOLE_CHUNK` is the same shape one step further gone — hard-coded in
the template with no Parameter at all.

Two properties are pinned here:

1. **Every name in `--parameter-overrides` exists in the template.** Renaming a
   Parameter without updating both workflows leaves SAM being handed a key it
   does not know, which it accepts quietly.
2. **Every boolean Parameter is passed by both workflows** — unless it is in
   `UNWIRED_BY_DESIGN` below, which exists so the exceptions are a visible,
   deliberate list rather than an unexamined silence.
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATE = os.path.join(REPO, "src", "template.yaml")
WORKFLOWS = {
    "prod": os.path.join(REPO, ".github", "workflows", "deploy-prod.yml"),
    "test": os.path.join(REPO, ".github", "workflows", "deploy.yml"),
}

# Boolean Parameters deliberately not passed by a workflow, with the reason.
# Adding to this list should be a decision, not a reflex: a toggle in here can
# only ever hold its template default.
UNWIRED_BY_DESIGN = {
    # Legacy ingest switches from the pre-VAD pipeline. Nothing sets them and
    # the paths they gate are no longer the ones in use.
    "DownloadAudio": {"prod", "test"},
    "DownloadVideo": {"prod", "test"},
    "DownloadFiles": {"prod", "test"},
    "EnableTranscribe": {"prod", "test"},
    # The data bucket lives in the prod account and its policy is owned there;
    # the test stack must never try to manage it.
    "ManageDataBucketPolicy": {"test"},
}


def _template_parameters():
    text = open(TEMPLATE, encoding="utf-8").read()
    block = re.search(r"^Parameters:\n(.*?)^[A-Za-z]", text, re.S | re.M).group(1)
    names = re.findall(r"^  (\w+):\s*$", block, re.M)
    booleans = set()
    for name in names:
        seg = re.search(rf"^  {name}:\s*$(.*?)(?=^  \w+:\s*$|\Z)", block,
                        re.S | re.M).group(1)
        if re.search(r"AllowedValues:\s*\['true',\s*'false'\]", seg):
            booleans.add(name)
    return set(names), booleans


def _overrides(path):
    """Names passed in the `--parameter-overrides` continuation block.

    Scoped to that block on purpose: `"Key=..."` appears elsewhere in these
    workflows (resource tags), and a whole-file scan reports it as a phantom
    parameter."""
    lines = open(path, encoding="utf-8").read().splitlines()
    start = next(i for i, ln in enumerate(lines) if "--parameter-overrides" in ln
                 and ln.strip().startswith("--parameter-overrides"))
    names = set()
    for ln in lines[start + 1:]:
        stripped = ln.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(r'"(\w+)=', stripped)
        if m:
            names.add(m.group(1))
            continue
        if not stripped.endswith("\\"):
            break
    return names


def test_every_override_names_a_real_template_parameter():
    names, _ = _template_parameters()
    for env, path in WORKFLOWS.items():
        unknown = _overrides(path) - names
        assert not unknown, (
            f"{env} deploy passes parameters the template does not declare: "
            f"{sorted(unknown)}. SAM accepts these quietly, so the value goes "
            f"nowhere.")


def test_every_boolean_toggle_is_reachable_from_a_repo_variable():
    _, booleans = _template_parameters()
    passed = {env: _overrides(path) for env, path in WORKFLOWS.items()}
    broken = []
    for name in sorted(booleans):
        for env in WORKFLOWS:
            if name in passed[env]:
                continue
            if env in UNWIRED_BY_DESIGN.get(name, set()):
                continue
            broken.append(f"{name} (not passed by {env})")
    assert not broken, (
        "These boolean Parameters can only ever hold their template default, so "
        "the documented 'set a repo variable and redeploy' rollback does not "
        "work for them: " + ", ".join(broken) + ". Either add the "
        "--parameter-overrides line, or record the exception in "
        "UNWIRED_BY_DESIGN with a reason.")


def test_the_audio_event_tag_filter_is_wired_in_both_environments():
    """The specific regression this file was written for."""
    for env, path in WORKFLOWS.items():
        assert "FilterAudioEventTags" in _overrides(path), (
            f"{env} does not pass FilterAudioEventTags, so "
            f"FILTER_AUDIO_EVENT_TAGS=false cannot be applied by redeploying")


def test_the_exception_list_does_not_rot():
    """An entry that no longer names a boolean Parameter is stale and hides
    nothing — it should be deleted rather than left to look load-bearing."""
    _, booleans = _template_parameters()
    stale = sorted(set(UNWIRED_BY_DESIGN) - booleans)
    assert not stale, (
        f"UNWIRED_BY_DESIGN names things that are no longer boolean template "
        f"parameters: {stale}")
