"""Pin: no scheduled DB client may out-pace Aurora's auto-pause timer.

This exists because the first draft of the scale-to-zero design shipped a
15-minute unconditional safety sweep together with `SecondsUntilAutoPause: 900`
— also 15 minutes. Two equal intervals mean the idle timer is reset just before
it can ever expire, so the cluster would have paused **never**, the whole feature
would have saved **nothing**, and every test would still have been green. The
error was caught in review, not by any check.

The rule this pins: a function that connects to Aurora unconditionally on a
schedule must not run more often than half the auto-pause threshold. Otherwise it
re-pins the 0.5-ACU floor 24/7 and no one is told.

`NonWorkExpiryFunction` is the live example of why this is a *pinning* test and
not a one-off review note: it is `rate(1 hour)` with PGHOST today and merely
happens to be DISABLED. The day life-conversation separation ships, it starts
waking the cluster hourly, and nothing else in this repo would notice.

FinalizeSweepFunction is the one legitimate exception — it stays on
`rate(1 minute)` to hold the ≤2-minute confirmation-email promise and instead
skips the *connection* on ticks its DynamoDB flag says are idle. That exception
is only valid while the gate actually exists, so it is asserted, not assumed.
"""
import os
import re

HERE = os.path.dirname(__file__)
APP_TEMPLATE = os.path.join(HERE, "..", "..", "src", "template.yaml")
DB_TEMPLATE = os.path.join(HERE, "..", "..", "infra", "db-template.yaml")
SWEEP_SRC = os.path.join(HERE, "..", "..", "src", "lambda_finalize_claim.py")

# Allowed to run faster than the threshold *because* it gates internally.
GATED_EXCEPTIONS = {"FinalizeSweepFunction"}

# AWS's own default when MinCapacity is 0 and the field is omitted.
AWS_DEFAULT_SECONDS_UNTIL_AUTO_PAUSE = 300


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _resource_blocks(text):
    """{LogicalId: block text} for every top-level resource (indent 2)."""
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.rstrip() == "Resources:")
    blocks, current, name = {}, [], None
    for ln in lines[start + 1:]:
        m = re.match(r"^  (\w+):\s*$", ln)
        if m:
            if name:
                blocks[name] = "\n".join(current)
            name, current = m.group(1), []
        elif name:
            current.append(ln)
    if name:
        blocks[name] = "\n".join(current)
    return blocks


def _schedule_seconds(expr):
    """Shortest interval an EventBridge schedule expression can fire at.

    rate() is exact. For cron(), a minute field carrying a step (`0/15`) fires
    every N minutes; a fixed minute field can fire at most once an hour, which is
    already the conservative bound this test needs.
    """
    m = re.match(r"rate\((\d+)\s+(minute|minutes|hour|hours|day|days)\)", expr.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return n * {"minute": 60, "minutes": 60, "hour": 3600,
                    "hours": 3600, "day": 86400, "days": 86400}[unit]
    m = re.match(r"cron\(([^\s)]+)", expr.strip())
    if m:
        minute_field = m.group(1)
        step = re.search(r"/(\d+)", minute_field)
        return int(step.group(1)) * 60 if step else 3600
    raise AssertionError(f"unrecognised schedule expression: {expr!r}")


def _pause_threshold():
    db = _read(DB_TEMPLATE)
    m = re.search(r"SecondsUntilAutoPause:\s*(\d+)", db)
    if m:
        return int(m.group(1))
    # Field absent: auto-pause is only armed at all when MinCapacity is 0, in
    # which case AWS applies its own 300s default.
    if re.search(r"MinCapacity:\s*0\s*$", db, re.MULTILINE):
        return AWS_DEFAULT_SECONDS_UNTIL_AUTO_PAUSE
    return None       # auto-pause disabled -> nothing to out-pace


def _scheduled_db_clients():
    """[(logical_id, schedule_expr)] for functions that carry PGHOST and run on a
    schedule."""
    out = []
    for name, block in _resource_blocks(_read(APP_TEMPLATE)).items():
        if "PGHOST:" not in block:
            continue
        for expr in re.findall(r"^\s*Schedule:\s*(.+?)\s*$", block, re.MULTILINE):
            out.append((name, expr))
    return out


def test_found_the_functions_we_mean_to_check():
    """Anti-vacuous guard: a parser that silently matches nothing would make
    every assertion below pass while checking absolutely nothing."""
    found = _scheduled_db_clients()
    names = {n for n, _ in found}
    assert found, "parsed no scheduled PGHOST functions — the parser is broken"
    assert "FinalizeSweepFunction" in names
    # The known landmine. If this resource is renamed, update the test — do not
    # let it drop out of coverage silently.
    assert "NonWorkExpiryFunction" in names


def test_pause_threshold_is_readable():
    assert _pause_threshold() is None or _pause_threshold() >= 300


def test_no_ungated_scheduled_client_outpaces_auto_pause():
    threshold = _pause_threshold()
    if threshold is None:
        return                       # auto-pause not armed; nothing to protect
    minimum = threshold * 2
    offenders = []
    for name, expr in _scheduled_db_clients():
        if name in GATED_EXCEPTIONS:
            continue
        seconds = _schedule_seconds(expr)
        if seconds < minimum:
            offenders.append(f"{name} runs every {seconds}s ({expr}), "
                             f"needs >= {minimum}s")
    assert not offenders, (
        "These scheduled functions connect to Aurora more often than half the "
        "auto-pause threshold, so the cluster can never reach its idle window "
        "and scale-to-zero saves nothing:\n  " + "\n  ".join(offenders)
        + "\nEither slow them down, or gate the connection like "
          "FinalizeSweepFunction does and add them to GATED_EXCEPTIONS.")


def test_the_exception_actually_gates_its_connection():
    """FinalizeSweepFunction is exempt only because it skips the connection when
    its flag says idle. If that gate goes, the exemption is a lie."""
    src = _read(SWEEP_SRC)
    assert "SWEEP_REQUIRE_PENDING" in src
    assert "sweep_state.is_pending" in src
    assert "no-pending" in src, "the skip path must be observable in logs"


def test_safety_pass_is_hourly_not_equal_to_the_threshold():
    """The safety pass fires once per wall-clock hour (a single minute-of-hour
    constant). Pin that an hour stays comfortably above the threshold — this is
    the exact relationship the first draft got wrong."""
    threshold = _pause_threshold()
    if threshold is None:
        return
    src = _read(SWEEP_SRC)
    assert "SAFETY_SWEEP_MINUTE" in src
    assert 3600 >= threshold * 2, (
        f"SecondsUntilAutoPause={threshold}s leaves the hourly safety pass "
        f"(3600s) too close to the threshold; the cluster will rarely pause.")
