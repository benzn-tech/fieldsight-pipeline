"""Unit: the backlog alarm's missing-data policy follows its own sweep.

`TreatMissingData: breaching` is correct only where something publishes the
metric. The sweep's `State` is gated on a condition (schedules are off on the
test stack by design), and the moment the alarm stopped sharing prod's series --
which is exactly what adding the Stage dimension did -- the test alarm was
watching a series nobody writes. It flapped into ALARM twice the same day and
e-mailed a real inbox. An alarm that is always red is an alarm nobody reads,
which is the failure the probe exists to prevent, reintroduced by its own alarm.

So the invariant is not "TreatMissingData is breaching" and not "there is an
!If". It is that BOTH properties turn on the SAME condition: wherever the sweep
does not run, absence must not be treated as breaking.
"""
import os
import re

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "..", "src", "template.yaml")


def _resource(name):
    lines = open(TEMPLATE, encoding="utf-8").read().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"  {name}:"))
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^  \S", lines[i])),
               len(lines))
    return "\n".join(lines[start:end])


def _condition_in(line):
    """The condition name out of a `!If [Cond, a, b]`."""
    m = re.search(r"!If\s*\[\s*([A-Za-z0-9]+)\s*,", line)
    return m.group(1) if m else None


def test_the_sweep_and_the_alarm_agree_on_when_absence_is_a_fault():
    sweep = _resource("ExtractionBacklogFunction")
    state = next(l for l in sweep.splitlines() if l.strip().startswith("State:"))
    gate = _condition_in(state)
    assert gate, f"the sweep's State is no longer conditional: {state.strip()!r}"

    alarm = _resource("ExtractionBacklogAlarm")
    missing = next(l for l in alarm.splitlines()
                   if l.strip().startswith("TreatMissingData:"))
    assert _condition_in(missing) == gate, (
        f"the sweep runs under {gate} but the alarm's missing-data policy is "
        f"{missing.strip()!r} -- on a stack where the sweep is off, that alarm "
        f"watches a series nobody writes"
    )
    # And the two arms are the right way round: absence is a fault only where
    # something is supposed to be publishing.
    assert re.search(r"!If\s*\[\s*%s\s*,\s*breaching\s*,\s*notBreaching\s*\]" % gate,
                     missing), missing.strip()
