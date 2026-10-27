"""Report-bench Anthropic spend, inside the ledger file shared with the web bench.

Spec §7. The file format and the report-pool read / write live in
tools/web-answer-bench/ledger.py (`read_state`, `add_report_spend`); this module
adds the report bench's own rules: the held-out reserve, the pre-held-out stop
and the Haiku long-context price. Every check reads the file fresh, so spend
the web bench recorded since is seen.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import rb_common  # noqa: E402


def web_ledger():
    return rb_common.load_web("ledger")


_W = web_ledger()

LEDGER_NAME = "anthropic_ledger.json"
REPORT_CAP = _W.REPORT_CAP_USD            # 40
GLOBAL_CAP = _W.GLOBAL_CAP_USD            # 85
HELDOUT_RESERVE = 9.0                     # 18 Sonnet runs x $0.50, set aside first
PRE_HELDOUT_STOP = REPORT_CAP - HELDOUT_RESERVE   # 31
HELDOUT_POOL = "report/heldout"
PRE_HELDOUT_POOLS = ("report/pilot", "report/round0", "report/rounds12")

# $ / MTok (input, output); Haiku bills prompts over 100K tokens at the second pair.
PRICES = dict(_W.PRICES)
LONG_CONTEXT_AT = 100_000
LONG_CONTEXT_PRICES = {"claude-haiku-5-5": (0.50, 2.50)}


def call_cost(model, input_tokens, output_tokens):
    pin, pout = PRICES[model]
    if input_tokens > LONG_CONTEXT_AT and model in LONG_CONTEXT_PRICES:
        pin, pout = LONG_CONTEXT_PRICES[model]
    return (input_tokens * pin + output_tokens * pout) / 1_000_000


class ReportLedger:
    def __init__(self, path):
        self.path = path

    def _state(self):
        return _W.read_state(self.path)

    def pool_spent(self, pool):
        return self._state()["pools"].get(pool, 0.0)

    def report_spent(self):
        return _W.report_spent(self._state())

    def global_spent(self):
        return _W.global_spent(self._state())

    def can_spend(self, pool, upper):
        st = self._state()
        eps = 1e-9
        if _W.global_spent(st) + upper > GLOBAL_CAP + eps:
            return False
        if _W.report_spent(st) + upper > REPORT_CAP + eps:
            return False
        if pool != HELDOUT_POOL:
            pre = sum(st["pools"].get(p, 0.0) for p in PRE_HELDOUT_POOLS)
            if pre + upper > PRE_HELDOUT_STOP + eps:
                return False
        return True

    def add(self, pool, dollars):
        _W.add_report_spend(self.path, pool, dollars)
