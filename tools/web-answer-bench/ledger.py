"""Anthropic spend for the whole bench, persisted, with a hard stop.

Spec §6: one ledger file in --out, cumulative across Stage 0 and the full bench,
Anthropic dollars only (tokens + web_search_requests x the owner's search price).
No Anthropic call is made once it reaches the cap.

Pools (spec §7): the total is split by phase so the prompt matrix cannot eat the
scored run. `matrix` stops at MATRIX_POOL_CAP; the scored run starts only with
SCORED_RESERVE of room. A file written before pools existed is all Stage 0.
"""
from __future__ import annotations

import json
import os

CAP_USD = 45.0
MATRIX_POOL_CAP = 18.0
# Upper bound of the surviving Claude arms' scored + repeat runs (B + C + C').
SCORED_RESERVE = 23.0
# $ per million tokens (input, output). Spec §1 / §4.0.
PRICES = {"claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}


def call_cost(model, input_tokens, output_tokens, searches, price_per_1000):
    pin, pout = PRICES[model]
    return (input_tokens * pin + output_tokens * pout) / 1_000_000 + \
        searches * price_per_1000 / 1000.0


class Ledger:
    def __init__(self, path, cap=CAP_USD):
        self.path = path
        self.cap = cap
        self.spent = 0.0
        self.pools = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.spent = float(data.get("spent_usd", 0.0))
            pools = data.get("pools")
            self.pools = ({k: float(v) for k, v in pools.items()} if isinstance(pools, dict)
                          else {"stage0": self.spent})

    def exhausted(self):
        return self.spent >= self.cap - 1e-9

    def room(self):
        return self.cap - self.spent

    def pool_spent(self, pool):
        return self.pools.get(pool, 0.0)

    def pool_exhausted(self, pool, pool_cap):
        return self.pool_spent(pool) >= pool_cap - 1e-9

    def add(self, dollars, pool="stage0"):
        # Re-read before writing: the report bench (or another run) may have
        # written report/* pools or spend since this ledger was loaded, and a
        # rewrite from memory would drop them.
        state = read_state(self.path)
        self.spent = state["spent_usd"] + float(dollars)
        self.pools = state["pools"]
        self.pools[pool] = self.pools.get(pool, 0.0) + float(dollars)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"spent_usd": round(self.spent, 6), "cap_usd": self.cap,
                       "pools": {k: round(v, 6) for k, v in self.pools.items()}}, f)
        os.replace(tmp, self.path)


# ---------------------------------------------------------------- report bench
# The report / extraction bench (tools/report-bench, spec
# 2026-10-28-report-extraction-prompt-model-bench-design §7) shares this file.
# Its dollars live ONLY in `pools` under `report/*`; `spent_usd` / `cap_usd`
# stay the web bench's own total and cap, copied through untouched, so the web
# `Ledger` above behaves exactly as before (its `add` rewrites `pools` whole and
# so carries the report pools along). Web $45 + report $40 = global $85.
REPORT_POOL_PREFIX = "report/"
# Held by tools/report-bench/rb_run.py in the shared --out while it runs.
LOCK_NAME = "bench.lock"


def refuse_if_locked(out, refuse):
    """Call `refuse(msg)` (which exits 2) when the report bench holds --out."""
    if os.path.exists(os.path.join(str(out), LOCK_NAME)):
        refuse(f"{LOCK_NAME} exists in --out: the report bench is running (shared ledger); "
               "wait for it, or delete the file only if no bench is running")
REPORT_CAP_USD = 40.0
GLOBAL_CAP_USD = 85.0


def read_state(path):
    """The file as the web `Ledger` reads it: {spent_usd, cap_usd, pools}."""
    if not os.path.exists(path):
        return {"spent_usd": 0.0, "cap_usd": CAP_USD, "pools": {}}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    spent = float(data.get("spent_usd", 0.0))
    pools = data.get("pools")
    pools = ({k: float(v) for k, v in pools.items()} if isinstance(pools, dict)
             else {"stage0": spent})
    return {"spent_usd": spent, "cap_usd": float(data.get("cap_usd", CAP_USD)),
            "pools": pools}


def report_spent(state):
    return sum(v for k, v in state["pools"].items() if k.startswith(REPORT_POOL_PREFIX))


def global_spent(state):
    """Web total (its `spent_usd`, never its pools) + every report pool."""
    return state["spent_usd"] + report_spent(state)


def add_report_spend(path, pool, dollars):
    """Add to one `report/*` pool. Re-reads the file immediately before the
    write (atomic replace); web keys are written back as read."""
    if not pool.startswith(REPORT_POOL_PREFIX):
        raise ValueError(f"report spend goes to a {REPORT_POOL_PREFIX}* pool, not {pool!r}")
    state = read_state(path)
    state["pools"][pool] = state["pools"].get(pool, 0.0) + float(dollars)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"spent_usd": state["spent_usd"], "cap_usd": state["cap_usd"],
                   "pools": {k: round(v, 6) for k, v in state["pools"].items()}}, f)
    os.replace(tmp, path)
    return state
