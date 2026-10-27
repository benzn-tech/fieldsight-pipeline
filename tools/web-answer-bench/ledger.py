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
        self.spent += float(dollars)
        self.pools[pool] = self.pools.get(pool, 0.0) + float(dollars)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"spent_usd": round(self.spent, 6), "cap_usd": self.cap,
                       "pools": {k: round(v, 6) for k, v in self.pools.items()}}, f)
        os.replace(tmp, self.path)
