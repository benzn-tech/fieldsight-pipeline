"""The model-call layer's registry: one entry per model-backed task (spec 2026-11-10 §4.2).

Each entry holds the model, its call parameters, the prompt hash it was measured with and the
prices used for accounting -- one place to change. This is the minimal slice the nightly J2
turn pick needs; the other fallback chains stay where they are.

Entry keys:
  model                 the model id (sent to the Claude API with ANTHROPIC_API_KEY)
  max_tokens            the response ceiling; thinking counts against it on this model
  effort                output_config.effort, as measured
  temperature           None = not sent (this model rejects a non-default sampling value)
  prompt_hash           the prompt the entry was measured with
  allow_cross_language  whether a cross-language pick may be written (True only once the
                        offline measurement's cross-language bar has passed)
  usd_per_mtok_in/out   prices; None = cost unknown, so the run's cost_usd stays NULL

No psycopg, no boto3: both the non-VPC worker and the in-VPC writer import it.
"""
from __future__ import annotations

import copy

import nightly_turn_pick

ENTRIES = {
    "nightly_turn_pick": {
        "model": "claude-sonnet-5-5",
        "max_tokens": 16000,
        "effort": "high",
        "temperature": None,
        "prompt_hash": nightly_turn_pick.PROMPT_HASH,
        "allow_cross_language": False,
        "usd_per_mtok_in": None,
        "usd_per_mtok_out": None,
    },
}


def get(task: str) -> dict:
    """A copy of the task's entry; KeyError for an unknown task."""
    return copy.deepcopy(ENTRIES[task])


def cost_usd(entry: dict, tokens_in: int, tokens_out: int):
    """The call's cost from the entry's prices, or None when either price is unknown."""
    p_in, p_out = entry.get("usd_per_mtok_in"), entry.get("usd_per_mtok_out")
    if p_in is None or p_out is None:
        return None
    return (int(tokens_in or 0) * p_in + int(tokens_out or 0) * p_out) / 1_000_000
