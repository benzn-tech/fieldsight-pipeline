"""
Unit: every Schedule event gates itself with `State`, never `Enabled`.

Caught on a real deploy, not by review. The non-work expiry sweep shipped with

    Enabled: !If [ShouldEnableNonWorkExpiry, true, false]

and the resulting EventBridge rule came up **ENABLED** on test anyway — SAM does
not translate that form. The feature was still inert (its handler checks an env
var first), but the schedule fired hourly and "ships disabled" was only half
true. Every other schedule in this template already used

    State: !If [Cond, ENABLED, DISABLED]

which does work. This pins the working form so the next scheduled function
cannot pick the one that silently does nothing.

Both checks parse the Schedule event's own Properties block rather than
grepping the whole file — `Enabled: true` also appears under a DynamoDB TTL
spec, and `State:` under resources that are not SAM events.
"""
import os
import re

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "..", "src", "template.yaml")


def _text():
    with open(TEMPLATE, encoding="utf-8") as f:
        return f.read()


def _schedule_blocks():
    """The Properties body of each `Type: Schedule` event, as a list of strings.

    A block runs from the `Type: Schedule` line to the first later line
    indented LESS than `Type:` — that is the next event name, one level up.
    Strictly-less, not less-or-equal: `Properties:` is `Type:`'s sibling at the
    same indent, and cutting there would leave every block empty."""
    lines = _text().splitlines()
    blocks = []
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)Type:\s*Schedule\s*$", line)
        if not m:
            continue
        indent = len(m.group(1))
        body = []
        for nxt in lines[i + 1:]:
            if nxt.strip() and (len(nxt) - len(nxt.lstrip())) < indent:
                break
            body.append(nxt)
        blocks.append("\n".join(body))
    return blocks


def test_there_are_schedule_events_to_check():
    """Guards the parser itself: a regex that silently matches nothing would
    make every other assertion here vacuously true."""
    assert len(_schedule_blocks()) >= 8


def test_no_schedule_event_uses_the_enabled_property():
    offenders = [b for b in _schedule_blocks() if re.search(r"^\s*Enabled:", b, re.M)]
    assert offenders == [], (
        "Schedule events must gate with `State: !If [Cond, ENABLED, DISABLED]`; "
        "`Enabled:` does not reach the EventBridge rule. Offending block(s):\n"
        + "\n---\n".join(offenders))


def test_the_nonwork_sweep_is_gated_on_its_own_condition():
    """It must not ride ShouldEnableSchedules or ShouldEnableFinalize — retiring
    personal topics is its own policy decision, per environment."""
    blocks = [b for b in _schedule_blocks() if "ExpirySweep" in b or "non-work" in b.lower()]
    assert len(blocks) == 1, f"expected exactly one non-work sweep schedule, found {len(blocks)}"
    assert "State: !If [ShouldEnableNonWorkExpiry, ENABLED, DISABLED]" in blocks[0]
