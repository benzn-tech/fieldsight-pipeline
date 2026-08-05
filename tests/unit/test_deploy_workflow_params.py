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
