"""Reference-free scoring with Claude as an LLM judge.

When you don't have a ground-truth transcript, we ask Claude to estimate each
system's accuracy by cross-referencing all outputs (consensus) plus linguistic
plausibility. Transcripts are anonymized as "Model A/B/C..." before judging to
reduce brand bias, then scores are mapped back.

This is an ESTIMATE, not a true WER — surfaced clearly in the UI. When a real
reference is provided, WER/CER is used instead and the judge is skipped.
"""
from __future__ import annotations

import json
import re
import string

DEFAULT_JUDGE_MODEL = "claude-sonnet-4-6"

_SYSTEM = (
    "You are a meticulous speech-recognition (ASR) evaluator. You will see "
    "several transcripts of the SAME audio produced by different ASR systems. "
    "There is no ground-truth reference. Estimate how accurate each transcript "
    "likely is by (1) cross-referencing where systems agree (consensus is "
    "probably correct), (2) judging linguistic fluency/plausibility, and "
    "(3) noting likely errors in proper nouns, numbers, and domain terms "
    "(this is NZ construction-site audio, possibly bilingual English/Mandarin). "
    "Be decisive and discriminating — do not give everyone the same score."
)


def judge_available(config: dict) -> bool:
    return bool(config.get("ANTHROPIC_API_KEY"))


def score_transcripts(config: dict, transcripts: dict[str, str]) -> dict[str, dict]:
    """transcripts: {provider_label: text}. Returns {provider_label: {score, reason}}."""
    items = [(label, txt) for label, txt in transcripts.items() if (txt or "").strip()]
    if not config.get("ANTHROPIC_API_KEY") or len(items) < 1:
        return {}
    try:
        import anthropic
    except Exception:
        return {}

    # Anonymize: Model A, Model B, ...
    aliases = {f"Model {string.ascii_uppercase[i]}": label for i, (label, _) in enumerate(items)}
    rev = {v: k for k, v in aliases.items()}
    blocks = "\n\n".join(
        f"### {rev[label]}\n{txt.strip()[:6000]}" for label, txt in items
    )
    user = (
        f"Here are {len(items)} transcripts of the same audio:\n\n{blocks}\n\n"
        "Return ONLY a JSON object mapping each model id to an object with "
        '"score" (integer 0-100, estimated accuracy) and "reason" (<= 25 words). '
        'Example: {"Model A": {"score": 87, "reason": "..."}}'
    )

    model = config.get("JUDGE_MODEL") or DEFAULT_JUDGE_MODEL
    try:
        client = anthropic.Anthropic(api_key=config["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model=model,
            max_tokens=1500,
            system=_SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    except Exception as exc:  # noqa: BLE001
        return {"_error": {"score": None, "reason": f"judge failed: {exc}"}}

    parsed = _extract_json(text)
    out: dict[str, dict] = {}
    for alias, label in aliases.items():
        entry = parsed.get(alias) or {}
        out[label] = {
            "score": _as_int(entry.get("score")),
            "reason": str(entry.get("reason", ""))[:200],
        }
    return out


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def _as_int(v):
    try:
        return int(round(float(v)))
    except Exception:
        return None
