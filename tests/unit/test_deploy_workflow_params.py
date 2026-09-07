"""Unit: the deploy workflows actually pass the parameters the template declares.

The failure this exists for is invisible rather than loud. `template.yaml` grew
`EnableNonWorkExpiry` / `NonWorkExpirySince`, both correctly defaulted to inert,
and neither workflow passed them — so the feature could not be switched on by
setting a repo variable, which is the only way anyone would try. Nothing broke;
it simply had no effect, in an environment where "no effect" and "off by
default" look identical.

Two shapes are checked:

  * a declared gate is threaded through to `sam deploy`, defaulted off
  * no override is emitted as a bare `Key=` — SAM rejects an empty value, and
    the existing layer overrides already work around it with a shell guard
"""
import os
import re

WORKFLOWS = {
    "test": os.path.join(os.path.dirname(__file__), "..", "..",
                         ".github", "workflows", "deploy.yml"),
    "prod": os.path.join(os.path.dirname(__file__), "..", "..",
                         ".github", "workflows", "deploy-prod.yml"),
}
PREFIX = {"test": "TEST", "prod": "PROD"}


def _text(env):
    with open(WORKFLOWS[env], encoding="utf-8") as fh:
        return fh.read()


def test_both_workflows_arm_the_sweep_from_a_repo_variable():
    for env, prefix in PREFIX.items():
        body = _text(env)
        assert f"EnableNonWorkExpiry=${{{{ vars.{prefix}_NONWORK_EXPIRY_ENABLED" in body, (
            f"{env}: the sweep cannot be switched on without this override")


def test_the_sweep_defaults_to_off_in_both_environments():
    """This one deletes — it tombstones topics and drops their vectors. A deploy
    that turns it on because someone forgot a default is not recoverable by
    redeploying."""
    for env, prefix in PREFIX.items():
        assert (f"vars.{prefix}_NONWORK_EXPIRY_ENABLED || 'false'") in _text(env), (
            f"{env}: missing the || 'false' default")


def test_the_floor_is_passed_only_when_set():
    """`NonWorkExpirySince` has no safe default: empty means "refuse to run",
    which is what we want, but SAM rejects a bare `Key=` and would fail the
    whole deploy. Passed through a shell guard, as the layer overrides are."""
    for env, prefix in PREFIX.items():
        body = _text(env)
        assert "$NONWORK_SINCE_PARAM" in body, f"{env}: floor not passed"
        assert f'if [ -n "${{{{ vars.{prefix}_NONWORK_EXPIRY_SINCE }}}}" ]' in body, (
            f"{env}: floor must be guarded, not emitted unconditionally")
        assert 'NonWorkExpirySince=" \\' not in body, (
            f"{env}: bare empty override — SAM rejects this")


def _passed_parameters(env):
    """Every CloudFormation parameter the workflow passes to `sam deploy`."""
    body = _text(env)
    inline = re.findall(r'^\s+"(\w+)=[^"]*"\s*\\?\s*$', body, re.MULTILINE)
    guarded = re.findall(r'^\s+\w+="(\w+)=', body, re.MULTILINE)
    return set(inline) | set(guarded)


def test_test_can_mirror_every_behaviour_prod_runs():
    """A parameter prod passes and test does not is a place where verifying
    something on test says nothing about prod.

    This is not hypothetical: AUTHORITY_FLIP ran true on prod and false on test
    for weeks, and test could not even be switched to match because deploy.yml
    did not pass the parameter at all. Anything test proved about topic
    authority was untransferable, which is the same class of gap that made
    BUG-39 possible.

    Values are allowed to differ — that is what separate environments are for.
    What must not differ is whether test is *capable* of matching.
    """
    # ManageDataBucketPolicy is environment-specific by design, documented in
    # template.yaml: test owns its own bucket's policy, prod points at the
    # shared lake whose policy is hand-managed and cannot be adopted by CFN.
    BY_DESIGN = {"ManageDataBucketPolicy"}

    missing = _passed_parameters("prod") - _passed_parameters("test") - BY_DESIGN
    assert not missing, (
        "prod passes these and test cannot: test would be unable to reproduce "
        f"prod's behaviour for them: {sorted(missing)}")


def test_no_override_line_can_emit_an_empty_value():
    """Generalises the trap above to every parameter, present and future.

    A line like `"Foo=${{ vars.BAR }}" \\` deploys fine while BAR is set and
    fails the entire deploy the day it is cleared. The two existing cases that
    genuinely have no default (the layer ARNs) use shell guards instead, and
    are excluded by that fact — they never appear inside a quoted override.
    """
    # Only real override lines: indented, quoted, and continued with a
    # backslash. The shell-guard form assigns into a variable first
    # (`FOO_PARAM="Key=..."`) and is the pattern being recommended, not flagged.
    pattern = re.compile(r'^\s+"(\w+)=\$\{\{ vars\.\w+ \}\}" \\$', re.MULTILINE)

    # Known and currently harmless, listed rather than fixed:
    #   * layer ARNs — required in practice; a missing one should fail loudly
    #     rather than deploy a stack wired to nothing
    #   * the device-ledger pair — added by separate work and set today, so the
    #     deploy is green. Clearing either repo variable would break the whole
    #     test deploy, not just the ledger. Left alone here to avoid editing
    #     another change's line; worth converting to a shell guard.
    ALLOWED = {"VadLayerArn", "DocxLayerArn", "KeyframeFfmpegLayerArn",
               "NotionDataSource", "DeviceLedgerUrl"}

    for env in WORKFLOWS:
        offenders = [k for k in pattern.findall(_text(env)) if k not in ALLOWED]
        assert not offenders, (
            f"{env}: these overrides have no default and will fail the whole "
            f"deploy the day their repo variable is cleared — use the "
            f"shell-guard pattern instead: {offenders}")


def test_no_override_line_can_emit_an_empty_value_from_a_shell_variable():
    """The sibling above only looks at `${{ vars.X }}` interpolations.

    A secret reaches the command as a SHELL variable, continued with a
    backslash, and an unset secret expands to the empty string exactly like a
    cleared repo variable. SAM then rejects the bare Key= at argument parsing,
    before CloudFormation is reached, and the whole deploy exits 2.

    That gap is not hypothetical: `QwenChatApiKey=$QWEN_CHAT_API_KEY` was added
    in this shape, passed the sibling test because it is not a `vars.`
    interpolation, and failed the first TEST deploy after merge. The shell-guard
    form (`FOO_PARAM=""; if [ -n "$FOO" ]; then FOO_PARAM="Key=$FOO"; fi`) is
    the pattern this repo already uses and is what the fix adopted.
    """
    pattern = re.compile(r'^\s+"(\w+)=\$(\w+)"', re.MULTILINE)

    # Secrets that are required for any deploy to be meaningful. An absent one
    # SHOULD fail loudly rather than deploy a stack wired to nothing -- the same
    # reasoning the sibling test applies to the layer ARNs.
    REQUIRED = {"ClaudeApiKey", "DashScopeApiKey", "DbSecretArn",
                "ElevenLabsApiKey", "FargateSubnetIds", "FargateVpcId",
                "RealPTTAccount", "RealPTTPassword"}

    for env in WORKFLOWS:
        offenders = [k for k, _ in pattern.findall(_text(env)) if k not in REQUIRED]
        assert not offenders, (
            f"{env}: an unset secret makes these emit a bare 'Key=' and SAM "
            f"exits 2 before CloudFormation runs -- use the shell-guard "
            f"pattern: {offenders}")


def test_no_comment_sits_inside_a_line_continuation():
    """A comment between two backslash-continued lines silently ends the command.

    This shipped and broke the test deploy. Everything from the `#` onward is
    comment, so `sam deploy` ran with a TRUNCATED argument list; the remaining
    argument lines were then parsed as separate commands and the step died with
    `"AuthorityFlip=false": command not found` — but only AFTER sam deploy had
    finished, so the failure looked cosmetic. It was not: the step exited 127,
    which skipped the migration step behind it, and two migrations silently did
    not reach the database.

    Explanations belong above the command, not between its arguments.
    """
    import glob
    import os
    root = os.path.join(os.path.dirname(__file__), "..", "..", ".github", "workflows")
    offenders = []
    for path in sorted(glob.glob(os.path.join(root, "*.yml"))):
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        for i in range(1, len(lines)):
            if lines[i - 1].rstrip().endswith("\\") and lines[i].strip().startswith("#"):
                offenders.append(f"{os.path.basename(path)}:{i + 1}")
    assert not offenders, (
        "comment inside a line continuation — ends the command early: "
        f"{offenders}")



def test_the_chat_key_reads_the_secret_the_key_was_created_under():
    """The chat vendor moved to OpenRouter by pointing `QwenBaseUrl` at it --
    the endpoint-dispatch design in `llm_utils._is_dashscope`, not a new
    provider branch. What that design does NOT decide is which GitHub secret
    holds the credential, and the key was created as OPENROUTER_API_KEY while
    the workflows read QWEN_CHAT_API_KEY.

    The failure is the quiet kind this file exists for: an unset
    QWEN_CHAT_API_KEY makes `UsesSeparateChatVendor` false, so chat silently
    falls back to the DashScope key and bills the wrong vendor -- or fails, once
    that account is the one in arrears. Nothing in the deploy says so.

    OPENROUTER_API_KEY takes precedence; QWEN_CHAT_API_KEY stays as the fallback
    so a repo that set the older name keeps working.
    """
    for env, path in WORKFLOWS.items():
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        assert "secrets.OPENROUTER_API_KEY" in src, env
        line = [l for l in src.splitlines() if "QWEN_CHAT_API_KEY:" in l]
        assert len(line) == 1, (env, line)
        assert "secrets.OPENROUTER_API_KEY" in line[0], (env, line[0])
        assert "secrets.QWEN_CHAT_API_KEY" in line[0], (env, line[0])
