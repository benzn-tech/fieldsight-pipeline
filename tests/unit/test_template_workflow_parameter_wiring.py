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


def _raw_override_lines(path):
    """The `--parameter-overrides` continuation block verbatim, including the
    `$SHELL_VAR` entries `_overrides` skips because they carry no literal name."""
    lines = open(path, encoding="utf-8").read().splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if ln.strip().startswith("--parameter-overrides"))
    out = []
    for ln in lines[start + 1:]:
        stripped = ln.strip()
        out.append(stripped)
        if not stripped.endswith("\\"):
            break
    return "\n".join(out)


def test_no_override_can_evaluate_to_a_bare_key():
    """`sam deploy` REJECTS `Key=` with an empty value — it does not fall back to
    the template default.

    So an override whose expression can produce nothing is not "unset", it is a
    deploy that never runs. On 2026-08-12 `"LlmTemperature=${{ vars.X || '' }}"`
    shipped to `main` and the prod deploy died at argument parsing with the whole
    release still un-deployed and every check downstream reading the OLD stack.
    The idiom for optional is the conditional `$SOME_PARAM` shell variable a few
    lines above it, appended only when the repo variable is non-empty.

    Scanning for the shape rather than the one name, because the two existing
    conditional overrides prove this is a repeated temptation.
    """
    for env, path in WORKFLOWS.items():
        lines = open(path, encoding="utf-8").read().splitlines()
        start = next(i for i, ln in enumerate(lines)
                     if ln.strip().startswith("--parameter-overrides"))
        for ln in lines[start + 1:]:
            stripped = ln.strip()
            if not stripped.endswith("\\") and not stripped.startswith('"'):
                break
            assert not re.search(r"\|\|\s*''\s*\}\}", stripped), (
                f"{env} deploy: {stripped!r} evaluates to a bare Key= when the "
                f"repo variable is unset, and SAM exits 2 on that. Use the "
                f"conditional $PARAM idiom instead.")


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


def test_the_temperature_knob_reaches_every_function_that_calls_an_llm():
    """`call_llm` is shared. A knob wired to some of its callers and not others
    means the extraction runs at one temperature and the summary at another,
    with nothing saying so."""
    text = open(TEMPLATE, encoding="utf-8").read()
    assert re.search(r"\n  LlmTemperature:\n", text), "no LlmTemperature Parameter"
    providers = text.count("LLM_PROVIDER: !Ref LlmProvider")
    temps = text.count("LLM_TEMPERATURE: !Ref LlmTemperature")
    assert providers == temps, (
        f"{providers} functions carry LLM_PROVIDER but {temps} carry "
        f"LLM_TEMPERATURE -- every LLM caller must get both or neither")


def test_both_workflows_can_set_the_temperature():
    """Test passes it inline; prod passes it conditionally. Either way the repo
    variable must be able to reach CloudFormation — a Parameter no workflow can
    set holds its default forever."""
    assert "LlmTemperature" in _overrides(WORKFLOWS["test"]), (
        "test does not pass LlmTemperature; the Parameter holds its default forever")
    prod = open(WORKFLOWS["prod"], encoding="utf-8").read()
    assert 'LLM_TEMP_PARAM="LlmTemperature=${{ vars.PROD_LLM_TEMPERATURE }}"' in prod, (
        "PROD_LLM_TEMPERATURE cannot reach the stack")
    assert "$LLM_TEMP_PARAM" in _raw_override_lines(WORKFLOWS["prod"]), (
        "the conditional is built and then never appended")


def test_prod_temperature_defaults_to_unset():
    """Unset means the override is ABSENT, not empty.

    Absent lets the template default (`''`) through, which is what makes the call
    omit `temperature` entirely — deliberate, because the 2x2 behind this knob
    covered one task on one session while call_llm serves the rolling summary,
    finalize, the matcher and the ask agent. Test runs it at 0 so the effect is
    observed somewhere before prod inherits it.

    Empty means the deploy does not happen at all; that is the 2026-08-12 failure
    and `test_no_override_can_evaluate_to_a_bare_key` is what stops it returning.
    """
    prod = open(WORKFLOWS["prod"], encoding="utf-8").read()
    assert "LlmTemperature" not in _overrides(WORKFLOWS["prod"]), (
        "prod must not pass the parameter unconditionally")
    assert 'if [ -n "${{ vars.PROD_LLM_TEMPERATURE }}" ]' in prod


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


# ----------------------------------------------------------
# The batch ledger lives in the transcriber's existing table, and TWO functions now reach
# it. Found by running phase 4 on TEST, 2026-08-12: the sweep queried
# `fieldsight-transcripts` -- the PROD table -- from the TEST stack, because the env var was
# never wired and the code default names prod's table. IAM refused the Query, which is the
# only reason it was a stack trace instead of a test environment writing prod's ledger.
#
# A default that names another environment's resource is not an inert default.
# ----------------------------------------------------------

_LEDGER_READERS = ("TranscribeFunction", "FinalizeSweepFunction")


def test_every_function_that_reaches_the_batch_ledger_is_told_which_table():
    text = open(TEMPLATE, encoding="utf-8").read()
    for fn in _LEDGER_READERS:
        assert "TRANSCRIPT_TABLE: !Ref TranscriptTableName" in _function_block(text, fn), (
            f"{fn} reads TRANSCRIPT_TABLE but is not given it, so it falls back to the code "
            f"default -- which names the PROD table and would be used from test")


def test_every_function_that_reaches_the_batch_ledger_may_actually_use_it():
    """The grant, not just the name. Without it the failure is an AccessDeniedException
    deep inside the seal, caught by the guard and visible only in a log line."""
    text = open(TEMPLATE, encoding="utf-8").read()
    for fn in _LEDGER_READERS:
        blk = _function_block(text, fn)
        assert "DynamoDBCrudPolicy" in blk and "TableName: !Ref TranscriptTableName" in blk, \
            f"{fn} is given TRANSCRIPT_TABLE but no DynamoDB policy for it"


# ----------------------------------------------------------
# Sealing a session's tail is real S3 work: read the member units, read the raw uploads to
# measure the overlap, write the batch and its map. Phase 4 wired the code and the switch
# and not the grants, and the failure was invisible in the intended way -- the guard caught
# the AccessDenied, the sweep carried on, the session finalized WITHOUT its last chunks.
# Found on TEST 2026-08-12, twice in a row: first the ledger Query, then s3:GetObject.
# ----------------------------------------------------------

_SWEEP_SEAL_GRANTS = (
    ("s3:GetObject", "audio_segments/*"),   # the member units being joined
    ("s3:GetObject", "users/*"),            # the raw uploads the trim is measured on
    ("s3:PutObject", "audio_segments/*"),   # the batch and its map
)


def test_the_sweep_may_do_the_s3_work_that_sealing_a_tail_requires():
    text = open(TEMPLATE, encoding="utf-8").read()
    blk = _function_block(text, "FinalizeSweepFunction")
    for action, prefix in _SWEEP_SEAL_GRANTS:
        stmts = [s for s in blk.split("- Effect: Allow") if action in s]
        assert any(prefix in s for s in stmts), (
            f"FinalizeSweepFunction has no {action} on {prefix}; the tail seal fails with "
            f"AccessDenied behind the guard and every session's last chunks go "
            f"untranscribed with nothing failing")


# ----------------------------------------------------------
# The reader half of the same map. `_rebase_batch_turns` fetches
# `audio_segments/…_batch_map.json`, a prefix the extractor has never had. Fixing only the
# key (PR #392) moved the failure from "wrong key" to "AccessDenied on the right key" and
# the symptom did not change at all: one WARNING, filename arithmetic, plausible times.
# Found by re-running the deployed function on TEST after that fix and reading the log
# instead of trusting the unit suite.
# ----------------------------------------------------------


def test_the_extractor_may_read_the_batch_maps_it_looks_for():
    text = open(TEMPLATE, encoding="utf-8").read()
    blk = _function_block(text, "ExtractSessionFunction")
    reads = [s for s in blk.split("- Effect: Allow") if "s3:GetObject" in s]
    assert any("audio_segments/*" in s for s in reads), (
        "ExtractSessionFunction has no s3:GetObject on audio_segments/*, where the seal "
        "writes every batch's time map — batched sessions silently keep filename "
        "arithmetic and only a WARNING says so")


# ----------------------------------------------------------
# The transcript ledger holds the batch ledger: which chunks are in a batch, and the
# SEAL# claim that stops two workers concatenating the same one. Until 2026-08-12 both
# stacks resolved the template default, so TEST wrote its rows into PROD's table --
# found by scanning it and seeing BATCH# rows prod could not have written, because prod
# has never had batching on.
#
# The failure mode of the fix is not a broken deploy, it is a fix that half-lands:
# `fieldsight-users-test` exists in this account and NOTHING wires it, a table created
# for a split that never happened. This test is the difference.
# ----------------------------------------------------------


def _effective_parameter(path, name):
    """What the stack really gets: the workflow's override, else the template default.

    Comparing the two override LINES is not enough. A stage that passes nothing takes
    the template default, so `prod=None, test='fieldsight-transcripts'` reads as two
    different values and is in fact the same table."""
    for ln in open(path, encoding="utf-8").read().splitlines():
        m = re.match(rf'"{name}=(.*)"\s*\\?$', ln.strip())
        if m:
            return m.group(1)
    text = open(TEMPLATE, encoding="utf-8").read()
    block = re.search(rf"\n  {name}:\n(.*?)(?=\n  \w+:)", text, re.S)
    default = re.search(r"Default:\s*(\S+)", block.group(1)) if block else None
    return default.group(1).strip("'\"") if default else None


def test_the_two_stacks_do_not_share_the_transcript_ledger():
    prod = _effective_parameter(WORKFLOWS["prod"], "TranscriptTableName")
    test = _effective_parameter(WORKFLOWS["test"], "TranscriptTableName")
    assert test and prod, f"could not resolve the table for both stages: {test=} {prod=}"
    assert test != prod, (
        f"both stacks resolve TranscriptTableName={test!r}. The batch ledger lives in "
        f"this table, including the SEAL# claim that stops two workers concatenating "
        f"the same batch -- sharing it puts TEST's rows, and TEST's failures, in PROD's "
        f"table. Found that way on 2026-08-12.")


# ----------------------------------------------------------
# BATCH_MAX_CHUNKS and BATCH_SEAL_DEADLINE_SEC were read from the environment by two
# functions and appeared NOWHERE in the template, so they could only ever hold their code
# defaults: the latency/cost dial of this feature was reachable only by editing code, and
# not rollable back by changing a stack parameter. Same shape as the unwired-toggle trap,
# one layer down.
#
# They are deliberately NOT wired symmetrically. The sweep passes deadline zero at close
# on purpose -- see the comment at its seal_ready_runs call -- so giving it
# BATCH_SEAL_DEADLINE_SEC would advertise a knob it ignores.
# ----------------------------------------------------------


def test_both_batch_sides_agree_on_how_many_chunks_make_a_batch():
    """The transcriber accumulates to this size and the sweep treats a run of it as
    complete. Different values there would seal runs the transcriber is still filling."""
    text = open(TEMPLATE, encoding="utf-8").read()
    for fn in ("TranscribeFunction", "FinalizeSweepFunction"):
        assert "BATCH_MAX_CHUNKS: !Ref BatchMaxChunks" in _function_block(text, fn), (
            f"{fn} reads BATCH_MAX_CHUNKS but is not given it -- it takes the code "
            f"default and can disagree with the other side about what a full batch is")


def test_the_seal_deadline_reaches_the_transcriber_and_only_it():
    text = open(TEMPLATE, encoding="utf-8").read()
    assert "BATCH_SEAL_DEADLINE_SEC: !Ref BatchSealDeadlineSec" in \
        _function_block(text, "TranscribeFunction"), \
        "the transcriber reads BATCH_SEAL_DEADLINE_SEC but is not given it"
    assert "BATCH_SEAL_DEADLINE_SEC" not in _function_block(text, "FinalizeSweepFunction"), \
        ("the sweep passes deadline zero at close on purpose; wiring the parameter to it "
         "would advertise a knob that path ignores")


def test_the_batch_dials_are_parameters_not_literals():
    """A knob that exists only in code takes its default forever and raises no error."""
    text = open(TEMPLATE, encoding="utf-8").read()
    for p in ("BatchMaxChunks", "BatchSealDeadlineSec"):
        assert re.search(rf"\n  {p}:\n", text), f"{p} is not a template Parameter"


def test_the_batch_dials_are_settable_from_a_repo_variable():
    """A Parameter is only the LAST of three links.

    `test_every_boolean_toggle_is_reachable_from_a_repo_variable` sweeps booleans;
    these two are strings, which is exactly how they slipped through. Adding the
    Parameter (#402) moved them from "edit the code" to "edit the template" -- better,
    and still not the documented rollback, which is "set a repo variable and redeploy".
    Found by an adversarial review of the PR that claimed to have fixed them.
    """
    for env, path in WORKFLOWS.items():
        passed = _overrides(path)
        for name in ("BatchMaxChunks", "BatchSealDeadlineSec"):
            assert name in passed, (
                f"{env} does not pass {name}, so it can only ever hold its template "
                f"default -- changing how many chunks make a batch would still need a "
                f"template edit and a deploy, not a repo variable")


# ----------------------------------------------------------
# The three failure alarms MONITORING.md promises ("you'll receive emails when...") were
# never created in either account. They are gated on `ShouldCreateAlerts`, which is gated on
# `AlertEmail`, which NO WORKFLOW PASSED -- so the Parameter held its empty default forever
# and the condition was permanently false. Verified 2026-08-13: `describe-alarms` returned
# nothing at all.
#
# This is the worst shape a monitoring gap can take, because the documentation says the
# monitoring exists. Nobody goes looking for an alarm they have been told they have.
# ----------------------------------------------------------


def test_the_failure_alarms_can_actually_be_switched_on():
    for env, path in WORKFLOWS.items():
        assert "AlertEmail" in _overrides(path), (
            f"{env} does not pass AlertEmail, so ShouldCreateAlerts can only ever be false "
            f"and the three alarms MONITORING.md promises are never created")


def test_no_alerts_is_spelled_with_a_sentinel_the_cli_will_carry():
    """`|| ''` here would repeat the failure that killed the 2026-08-12 prod deploy."""
    for env, path in WORKFLOWS.items():
        line = [ln for ln in open(path, encoding="utf-8").read().splitlines()
                if "AlertEmail=" in ln]
        assert len(line) == 1
        assert "|| 'none'" in line[0], (
            f"{env} must default AlertEmail to the sentinel `none`, not to the empty "
            f"string -- SAM rejects an empty --parameter-overrides value and fails the "
            f"entire deploy")


def test_both_spellings_of_no_alerts_are_inert():
    """The sentinel AND the empty string must both mean "no alarms".

    Changing the default without changing the condition would flip it from permanently
    false to permanently TRUE, and subscribe SNS to the literal address `none`."""
    text = open(TEMPLATE, encoding="utf-8").read()
    cond = re.search(r"ShouldCreateAlerts:(.*?)(?=\n  \w+:)", text, re.S).group(1)
    assert "''" in cond, "the empty string must still mean no alarms"
    assert "'none'" in cond, "the sentinel the workflows send must also mean no alarms"


# ----------------------------------------------------------
# Alarms on the function that spends money and can lose audio
# ----------------------------------------------------------

def test_the_transcriber_has_an_error_alarm():
    """It had none, and that is why 27 lost batches were silent.

    2026-08-13, replaying a real 71-minute session into TEST: 27 transcription requests
    were rejected with HTTP 429 (the provider caps concurrent requests at 20; peak Lambda
    concurrency was 141). Each one left a batch WAV with no transcript and nothing to
    re-drive it — about 54 minutes of audio gone. The only trace was ERROR lines nobody
    was watching, because every other significant function has an alarm and this one
    does not.

    Threshold 1 is deliberate and is not noisy: seven days of prod transcribe logs contain
    zero `Error processing` lines. An error here means audio is at risk.
    """
    text = open(TEMPLATE, encoding="utf-8").read()
    block = re.search(r"\n  TranscribeErrorAlarm:\n(.*?)(?=\n  \w+:\n)", text, re.S)
    assert block, "no TranscribeErrorAlarm in the template"
    body = block.group(1)
    # NOT AWS/Lambda Errors: a caught provider rejection returns 200, so that metric never
    # moves. The alarm has to watch a count the function emits itself.
    assert "MetricName: TranscriptionFailures" in body
    assert "Namespace: FieldSight/Transcribe" in body
    assert "!Ref AlertTopic" in body, "an alarm nobody is told about is not an alarm"


def test_the_transcriber_has_a_throttle_alarm():
    """A throttled async invocation writes NO log line at all.

    So the moment a concurrency ceiling is put on this function — which is the fix for the
    429s — the failure mode becomes completely invisible unless the Throttles metric is
    watched. This alarm has to exist BEFORE the ceiling does, not after.
    """
    text = open(TEMPLATE, encoding="utf-8").read()
    block = re.search(r"\n  TranscribeThrottleAlarm:\n(.*?)(?=\n  \w+:\n)", text, re.S)
    assert block, "no TranscribeThrottleAlarm in the template"
    assert "MetricName: Throttles" in block.group(1)


# ----------------------------------------------------------
# The speaker embedder. Three things about it have already gone wrong once each elsewhere
# in this stack, so each is pinned rather than trusted:
#   * a function that reads S3 without the grant (three times tonight, each a single
#     WARNING behind a guard);
#   * a missing ListBucket, which turns "not there" into 403 and makes it look like "not
#     allowed" — or the reverse, depending on which one you were expecting;
#   * a cp312-only layer attached to a function on another runtime, which fails at import.
# ----------------------------------------------------------


def test_the_embedder_may_read_the_audio_and_the_model():
    text = open(TEMPLATE, encoding="utf-8").read()
    blk = _function_block(text, "SpeakerEmbedFunction")
    reads = [s for s in blk.split("- Effect: Allow") if "s3:GetObject" in s]
    for prefix in ("users/*", "models/*"):
        assert any(prefix in s for s in reads), (
            f"SpeakerEmbedFunction has no s3:GetObject on {prefix} — it reads the raw "
            f"upload and downloads its own model, and a denial there is invisible")


def test_the_embedder_can_tell_a_missing_key_from_a_denied_one():
    text = open(TEMPLATE, encoding="utf-8").read()
    blk = _function_block(text, "SpeakerEmbedFunction")
    assert "s3:ListBucket" in blk, (
        "without ListBucket S3 answers 403 for a key that does not exist, so the function "
        "cannot distinguish a missing recording from a permissions fault")


def test_the_embedder_runs_on_the_runtime_its_layer_was_built_for():
    """`fieldsight-vad-layer` is cp312-only. Attached to a function on another runtime it
    fails at import, not at deploy."""
    text = open(TEMPLATE, encoding="utf-8").read()
    blk = _function_block(text, "SpeakerEmbedFunction")
    assert "VadLayerArn" in blk, "the embedder needs onnxruntime, which that layer carries"
    assert "python3.12" in blk, (
        "the embedder attaches the cp312-only VAD layer but does not pin Runtime to "
        "python3.12 — a runtime bump would break it at import with a green deploy")


def test_the_embedder_has_no_event_trigger():
    """It is invoked directly. An S3 event here would embed every upload in the lake."""
    text = open(TEMPLATE, encoding="utf-8").read()
    blk = _function_block(text, "SpeakerEmbedFunction")
    assert "Events:" not in blk, (
        "SpeakerEmbedFunction must have no Events — it is invoked on demand, and a "
        "trigger would run a model over every file that lands")


def test_the_transcriber_can_be_given_a_concurrency_ceiling():
    """The provider caps CONCURRENT requests; Lambda does not, and 141 met a limit of 20.

    Reserved concurrency is the only mechanism that bounds this before the request is made
    -- everything else reacts to the rejection, and the rejection loses the audio. It has to
    be a parameter rather than a constant because the two stages hold different keys on
    different plans, and the number must be derived from the live consumer list, not guessed.
    """
    text = open(TEMPLATE, encoding="utf-8").read()
    assert re.search(r"\n  TranscribeReservedConcurrency:\n", text), "no parameter"
    assert "ReservedConcurrentExecutions: !Ref TranscribeReservedConcurrency" in text \
        or "ReservedConcurrentExecutions: !If" in text, "the parameter reaches no function"
    for env in ("prod", "test"):
        assert "TranscribeReservedConcurrency" in _overrides(WORKFLOWS[env]), \
            f"{env} cannot set it, so it holds its default forever"


def test_an_unset_ceiling_means_no_reservation_at_all():
    """`0` must omit the property, not reserve zero -- reserving zero would stop the
    function dead. Prod's plan is unknown from this repo and its observed peak is 54, so a
    guessed number there would throttle a working pipeline."""
    text = open(TEMPLATE, encoding="utf-8").read()
    assert re.search(r"HasTranscribeReserve:\s*!Not \[!Equals \[!Ref TranscribeReservedConcurrency, '0'\]\]", text)


def test_the_embedder_can_read_the_batch_map_but_not_the_audio_beside_it():
    """A batched turn's audio is reached THROUGH the map, and the map lives under
    audio_segments/ next to the normalised copy this function must never read.

    So the grant is narrowed to `*_batch_map.json`. Granting `audio_segments/*` would be one
    character shorter and would silently permit reading the normalised audio, on which none
    of the Phase 0 thresholds hold — a measurement error that produces plausible numbers
    rather than an error.
    """
    t = open(TEMPLATE, encoding="utf-8").read()
    assert "/audio_segments/*_batch_map.json" in t, (
        "the embedder cannot read the batch map, so every batched turn 403s or reads the "
        "wrong seconds")
    assert "/audio_segments/*'" not in t and '/audio_segments/*"' not in t, (
        "audio_segments/* is granted wholesale somewhere — the narrowing is the point")


def test_the_voiceprint_writer_has_the_layer_the_embedder_cannot_have():
    """The split, pinned from both sides. The embedder carries VadLayerArn (cp312, for
    onnxruntime) and the writer carries PsycopgLayer (cp311) — one function cannot have
    both, and the night this was learned the embedder had a connection and no psycopg,
    raising ModuleNotFoundError on every invocation behind a green deploy.
    """
    t = open(TEMPLATE, encoding="utf-8").read()
    block = t[t.index("  VoiceprintWriterFunction:"):t.index("  SuggestionWriterFunction:")]
    assert "!Ref PsycopgLayer" in block, "the writer cannot reach Aurora without psycopg"
    assert "VadLayerArn" not in block, (
        "the writer took the cp312 layer as well; the two are mutually exclusive")
    assert "VpcConfig" in block, "Aurora is only reachable from inside the VPC"
    assert "Events:" not in block, (
        "the writer acquired a trigger; it is invoked by the embedder and nothing else")


def test_the_embedder_may_invoke_the_writer_and_nothing_else():
    """Its own work reaches the database only through this invoke — it has no psycopg. A
    missing grant here fails at runtime with an AccessDenied nobody sees until a real
    correction is made."""
    t = open(TEMPLATE, encoding="utf-8").read()
    block = t[t.index("  SpeakerEmbedFunction:"):t.index("  VoiceprintWriterFunction:")]
    assert "lambda:InvokeFunction" in block
    assert "voiceprint-writer" in block
    assert "Resource: '*'" not in block and 'Resource: "*"' not in block
