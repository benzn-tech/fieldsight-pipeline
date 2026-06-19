"""Accuracy + efficiency metrics for ASR outputs.

- WER  : word error rate  (English-oriented)
- CER  : character error rate (the right metric for Chinese, which has no word
         boundaries; we strip whitespace before comparing)
- RTF  : real-time factor = processing_time / audio_duration (lower is faster)

Reference normalization is intentionally light (lowercase, strip punctuation,
collapse whitespace) so we compare *content* not formatting. We report both WER
and CER for every pair because FieldSight audio is bilingual (NZ English +
Mandarin), and you can decide which matters per clip.
"""
from __future__ import annotations

import re
import unicodedata

try:
    import jiwer
except Exception:  # pragma: no cover - surfaced in the UI if missing
    jiwer = None

# Punctuation across both scripts. Kept out of WER/CER so "ok." == "ok".
_PUNCT = "，。！？；：、,.!?;:\"'`~@#$%^&*()_+-=[]{}|\\/<>《》「」『』（）…—·"
_PUNCT_RE = re.compile("[" + re.escape(_PUNCT) + r"\s]+")
_CJK_RE = re.compile(r"[一-鿿㐀-䶿]")


def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.lower()
    # Drop punctuation but keep a single space between tokens.
    text = re.sub("[" + re.escape(_PUNCT) + "]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _chars_only(text: str) -> str:
    """For CER: remove all whitespace and punctuation, compare char-by-char."""
    text = unicodedata.normalize("NFKC", text or "").lower()
    return _PUNCT_RE.sub("", text)


def looks_chinese(text: str) -> bool:
    if not text:
        return False
    cjk = len(_CJK_RE.findall(text))
    return cjk >= max(3, len(text) * 0.15)


def compute_wer(reference: str, hypothesis: str) -> float | None:
    if jiwer is None or not reference.strip():
        return None
    ref, hyp = normalize_text(reference), normalize_text(hypothesis)
    if not ref:
        return None
    try:
        return float(jiwer.wer(ref, hyp))
    except Exception:
        return None


def compute_cer(reference: str, hypothesis: str) -> float | None:
    if jiwer is None or not reference.strip():
        return None
    ref, hyp = _chars_only(reference), _chars_only(hypothesis)
    if not ref:
        return None
    try:
        # jiwer.cer on space-joined chars == character error rate
        return float(jiwer.cer(ref, hyp))
    except Exception:
        return None


def primary_metric(reference: str, hypothesis: str) -> tuple[str, float | None]:
    """Pick the headline metric: CER for Chinese-dominant refs, else WER."""
    if looks_chinese(reference):
        return "CER", compute_cer(reference, hypothesis)
    return "WER", compute_wer(reference, hypothesis)


def real_time_factor(latency_s: float, audio_duration_s: float) -> float | None:
    if not audio_duration_s:
        return None
    return latency_s / audio_duration_s
