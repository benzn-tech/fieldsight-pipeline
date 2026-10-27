"""Anthropic spend for the whole bench, persisted, with a hard stop.

Spec §6: one ledger file in --out, cumulative across Stage 0 and the full bench,
Anthropic dollars only (tokens + web_search_requests x the owner's search price).
No Anthropic call is made once it reaches the cap.
"""
from __future__ import annotations

import json
import os

CAP_USD = 45.0
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
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.spent = float(json.load(f).get("spent_usd", 0.0))

    def exhausted(self):
        return self.spent >= self.cap - 1e-9

    def add(self, dollars):
        self.spent += float(dollars)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"spent_usd": round(self.spent, 6), "cap_usd": self.cap}, f)
        os.replace(tmp, self.path)
