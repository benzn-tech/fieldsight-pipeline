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


def test_the_whole_chunk_mode_is_wired_in_both_environments():
    """It was hard-coded 'true' in the template with no Parameter at all, so the
    comparison it gates — whole-chunk sent 348s of 498s to the transcriber on the
    2026-08-08 session, segment mode would have sent 134.5s — needed a code change
    to run. It defaults to the CURRENT value, so wiring it changes nothing."""
    for env, path in WORKFLOWS.items():
        assert "TranscribeWholeChunk" in _overrides(path), (
            f"{env} does not pass TranscribeWholeChunk, so the setting cannot be "
            f"compared without editing the template")


def test_the_audio_event_tag_filter_is_wired_in_both_environments():
    """The specific regression this file was written for."""
    for env, path in WORKFLOWS.items():
        assert "FilterAudioEventTags" in _overrides(path), (
            f"{env} does not pass FilterAudioEventTags, so "
            f"FILTER_AUDIO_EVENT_TAGS=false cannot be applied by redeploying")


def test_the_upload_verify_mode_is_wired_in_both_environments():
    """Not a boolean, so the sweep above cannot see it — and it is the one
    switch where being stuck on a default is worst in both directions: stuck on
    `off` keeps losing 0.9% of recordings, stuck on `enforce` rejects every
    upload. It must be settable from a repo variable in both environments."""
    for env, path in WORKFLOWS.items():
        assert "UploadVerifyMode" in _overrides(path), (
            f"{env} does not pass UploadVerifyMode, so UPLOAD_VERIFY_MODE can "
            f"only ever hold its template default and neither rolling forward "
            f"to enforce nor rolling back to off would work")


def test_the_exception_list_does_not_rot():
    """An entry that no longer names a boolean Parameter is stale and hides
    nothing — it should be deleted rather than left to look load-bearing."""
    _, booleans = _template_parameters()
    stale = sorted(set(UNWIRED_BY_DESIGN) - booleans)
    assert not stale, (
        f"UNWIRED_BY_DESIGN names things that are no longer boolean template "
        f"parameters: {stale}")


# ---- test must not spend prod's ASR allowance --------------------------

def _secret_bound_to(env, var):
    """Which repo secret a workflow binds to an env var, or None."""
    text = open(WORKFLOWS[env], encoding="utf-8").read()
    m = re.search(rf"^\s*{var}:\s*\$\{{\{{\s*secrets\.(\w+)\s*\}}\}}\s*$",
                  text, re.MULTILINE)
    return m.group(1) if m else None


def test_test_and_prod_use_different_elevenlabs_keys():
    """They were the SAME key until 2026-08-08.

    ElevenLabs bills a shared allowance per key, so one key across both
    environments means every test recording spends prod's budget. A spent
    allowance presents as transcription simply stopping -- indistinguishable
    from a backend fault, and it very nearly cost a demo.

    Splitting them is a one-line change that a later edit could undo without
    anything failing until the morning someone runs out of credit, which is why
    it is pinned here rather than left to a comment.
    """
    prod = _secret_bound_to("prod", "ELEVENLABS_API_KEY")
    test = _secret_bound_to("test", "ELEVENLABS_API_KEY")
    assert prod and test, f"could not find the binding (prod={prod}, test={test})"
    assert prod != test, (
        f"both workflows read secrets.{prod} — test recordings would spend "
        "prod's ElevenLabs allowance")


def test_the_test_key_does_not_silently_fall_back_to_prods():
    """`${{ secrets.A || secrets.B }}` is valid and would look like a kindness.

    It is not: a missing test secret would silently restore the shared-quota
    behaviour, and nothing would fail until prod ran out of credit. A deploy
    that fails loudly is the better outcome.
    """
    text = open(WORKFLOWS["test"], encoding="utf-8").read()
    line = [ln for ln in text.splitlines()
            if re.match(r"\s*ELEVENLABS_API_KEY:", ln)]
    assert len(line) == 1, f"expected one binding, found {len(line)}"
    assert "||" not in line[0], "no silent fallback to prod's key"


# ---- the group-merge tunables ------------------------------------------

_MERGE_TUNABLES = {
    # env var            : (template Parameter, functions that read it)
    "GROUP_MERGE_CAP":     ("GroupMergeCap",
                            ("ItemWriterFunction", "FinalizeSweepFunction")),
    "STUCK_MERGE_SECONDS": ("StuckMergeSeconds", ("FinalizeSweepFunction",)),
    "GROUP_MAX_SPAN_SECONDS": ("GroupMaxSpanSeconds", ("FinalizeSweepFunction",)),
}


def _function_block(text, name):
    start = text.index(f"\n  {name}:\n")
    nxt = re.search(r"\n  [A-Za-z][A-Za-z0-9]*:\n", text[start + 1:])
    return text[start:start + 1 + nxt.start()] if nxt else text[start:]


def test_the_merge_tunables_exist_as_template_parameters():
    """They were code-only defaults, which is the unwired-toggle trap.

    Setting one on a live Lambda appears to work and is erased by the next
    CloudFormation reconcile, with nothing logged.
    """
    text = open(TEMPLATE, encoding="utf-8").read()
    for env, (param, _) in _MERGE_TUNABLES.items():
        assert re.search(rf"\n  {param}:\n", text), \
            f"{env} has no {param} Parameter — it can only ever hold its code default"


def test_every_function_that_reads_the_cap_gets_the_same_one():
    """GROUP_MERGE_CAP is read by TWO functions.

    item-writer uses it to decide whether a late member may re-arm; the sweep
    uses it to decide when a failing merge has spent its budget. If only one is
    given a value, they disagree silently -- one keeps re-arming past the point
    the other has given up, and the group's behaviour depends on which code path
    reaches it first.
    """
    text = open(TEMPLATE, encoding="utf-8").read()
    for env, (param, fns) in _MERGE_TUNABLES.items():
        for fn in fns:
            assert f"{env}: !Ref {param}" in _function_block(text, fn), \
                f"{fn} reads {env} but is not given it"


def test_both_workflows_pass_the_merge_tunables():
    for env_name in ("prod", "test"):
        wf = open(WORKFLOWS[env_name], encoding="utf-8").read()
        for _, (param, _) in _MERGE_TUNABLES.items():
            assert f"{param}=" in wf, \
                f"{env_name} does not pass {param}; the Parameter holds its default forever"


# ---- the evidence-matcher tunables -------------------------------------
#
# These are not booleans, so the boolean sweep above never covered them, and
# they were code-only defaults until 2026-08-10: `EVIDENCE_WINDOW_SEC` did not
# appear in the template at all. That was found while calibrating W against the
# first real multi-chunk sessions -- the measurement said 300s was ~50x wider
# than any honest match needed, and there was no way to apply the answer. A
# calibration whose result cannot be deployed is not a calibration.

_EVIDENCE_TUNABLES = {
    # env var                    : (template Parameter, functions that read it)
    "EVIDENCE_WINDOW_SEC":        ("EvidenceWindowSec", ("ExtractSessionFunction",)),
    "EVIDENCE_FUZZY_THRESHOLD":   ("EvidenceFuzzyThreshold", ("ExtractSessionFunction",)),
    "EVIDENCE_FLOOR_TOKENS":      ("EvidenceFloorTokens", ("ExtractSessionFunction",)),
}


def test_the_evidence_tunables_exist_as_template_parameters():
    text = open(TEMPLATE, encoding="utf-8").read()
    for env, (param, _) in _EVIDENCE_TUNABLES.items():
        assert re.search(rf"\n  {param}:\n", text), \
            f"{env} has no {param} Parameter — it can only ever hold its code default"


def test_every_function_that_reads_an_evidence_tunable_is_given_it():
    """The middle segment of the three. A Parameter that no function references
    is as inert as no Parameter at all, and reads as wired from the template's
    Parameters block alone."""
    text = open(TEMPLATE, encoding="utf-8").read()
    for env, (param, fns) in _EVIDENCE_TUNABLES.items():
        for fn in fns:
            assert f"{env}: !Ref {param}" in _function_block(text, fn), \
                f"{fn} reads {env} but is not given it"


def test_both_workflows_pass_the_evidence_tunables():
    for env_name in ("prod", "test"):
        for _, (param, _) in _EVIDENCE_TUNABLES.items():
            assert param in _overrides(WORKFLOWS[env_name]), \
                (f"{env_name} does not pass {param}; the Parameter holds its "
                 f"default forever and the calibrated value cannot be applied")


def test_the_window_default_is_not_the_uncalibrated_one():
    """300s was the provisional value shipped before there was a distribution.

    Across 42 citations from two real multi-chunk sessions every honest match
    landed within 6.1s of its cited time. This does not pin a specific number --
    a later calibration may move it again -- it pins that the value stopped
    being the one chosen before any measurement existed.
    """
    for env_name in ("prod", "test"):
        wf = open(WORKFLOWS[env_name], encoding="utf-8").read()
        line = [ln for ln in wf.splitlines() if "EvidenceWindowSec=" in ln]
        assert len(line) == 1, f"{env_name}: expected one line, found {len(line)}"
        assert "'300'" not in line[0], (
            f"{env_name} still defaults W to the pre-measurement 300s")


def test_the_fuzzy_default_is_not_the_uncalibrated_one():
    """0.9 was chosen before any distribution existed to look at.

    Measured 2026-08-10 against two populations -- real quotes scored against
    windows they do not belong to (n=1,886), and real quotes rewritten the way
    a model tidies (n=109) -- it rejects 59.6% of honest-but-tidied quotes, and
    30.3% of those carrying a single tidy, filing every one as the fabrication
    signal this feature exists to measure.

    Like the window test, this pins that the value stopped being the
    pre-measurement one, not a particular number.
    """
    for env_name in ("prod", "test"):
        wf = open(WORKFLOWS[env_name], encoding="utf-8").read()
        line = [ln for ln in wf.splitlines() if "EvidenceFuzzyThreshold=" in ln]
        assert len(line) == 1, f"{env_name}: expected one line, found {len(line)}"
        assert "'0.9'" not in line[0], (
            f"{env_name} still defaults the fuzzy cut to the pre-measurement 0.9")


def test_the_code_defaults_match_the_template_defaults():
    """When they disagree the environment wins silently, and the number in the
    source reads like the one in force. That is how a calibrated value gets
    quietly reverted by someone reading only the module."""
    import re as _re
    tpl = open(TEMPLATE, encoding="utf-8").read()
    src = open(os.path.join(REPO, "src", "lambda_extract_session.py"),
               encoding="utf-8").read()
    pairs = [("EvidenceWindowSec", "EVIDENCE_WINDOW_SEC"),
             ("EvidenceFuzzyThreshold", "EVIDENCE_FUZZY_THRESHOLD"),
             ("EvidenceFloorTokens", "EVIDENCE_FLOOR_TOKENS")]
    for param, env in pairs:
        block = _re.search(rf"\n  {param}:\n(.*?)(?=\n  \w+:\n)", tpl, _re.S).group(1)
        tpl_default = _re.search(r"Default:\s*'([^']+)'", block).group(1)
        code_default = _re.search(
            rf"os\.environ\.get\('{env}',\s*'([^']+)'\)", src).group(1)
        assert float(tpl_default) == float(code_default), (
            f"{env}: template default {tpl_default!r} != code default "
            f"{code_default!r}")


# ----------------------------------------------------------
# Batched transcription (spec 2026-08-11). Two functions read BATCH_TRANSCRIPTION and both
# must be given it: the transcriber accumulates members, and the sweep seals a session's
# LAST run — the one that has no fourth chunk coming. Give it to only one and the failure
# is silent in the worse direction: members accumulate, nothing ever seals the tail, and
# the end of every session goes untranscribed with no error anywhere.
# ----------------------------------------------------------

_BATCH_READERS = ("TranscribeFunction", "FinalizeSweepFunction")


def test_the_batch_switch_is_wired_in_both_environments():
    for env, path in WORKFLOWS.items():
        assert "BatchTranscription" in _overrides(path), (
            f"{env} does not pass BatchTranscription, so BATCH_TRANSCRIPTION can only "
            f"ever hold its template default and neither turning batching on nor rolling "
            f"it back would work")


def test_every_function_that_reads_the_batch_switch_is_given_it():
    text = open(TEMPLATE, encoding="utf-8").read()
    for fn in _BATCH_READERS:
        assert "BATCH_TRANSCRIPTION: !Ref BatchTranscription" in _function_block(text, fn), \
            (f"{fn} reads BATCH_TRANSCRIPTION but is not given it — it would silently take "
             f"the code default and disagree with the other function")


def test_the_transcriber_is_told_whether_chunks_are_whole():
    """Batching's precondition. The function refuses to batch per-VAD-segment units, so it
    has to be able to see which mode the VAD is in — reading a code default here would let
    it batch fragments the moment someone flips whole-chunk off."""
    text = open(TEMPLATE, encoding="utf-8").read()
    assert "TRANSCRIBE_WHOLE_CHUNK: !Ref TranscribeWholeChunk" in \
        _function_block(text, "TranscribeFunction"), \
        "TranscribeFunction reads TRANSCRIBE_WHOLE_CHUNK but is not given it"
