"""Cosine similarity, in pure Python, for the functions that must not carry numpy.

`voiceprint_utils` is numpy throughout and rightly so — it does frame-level work on whole
windows. But `repositories.voiceprints._agreement` imports it for **one dot product over a
handful of vectors**, and that repository is loaded by `lambda_voiceprint_writer`, which is
in-VPC, carries the psycopg layer, and has no numpy at all.

The result: enrolment logged `enrolment accepted (frames=2 spread=0.198 limit=0.35)` and then
died with `ModuleNotFoundError: No module named 'numpy'` in the function that stores it. The
guard passed; nothing was written; the voiceprint library stayed empty for weeks while the
accepted line said otherwise.

The fix is not a numpy layer on the writer. A compiled layer pins the runtime and the pin is
contagious — that is a documented trap in this repository, and it would attach a 40 MB
dependency to a function whose entire job is one INSERT. The arithmetic is a dot product and
two norms over ~200 floats, exact in either implementation.

`voiceprint_utils.cosine` delegates here so there is ONE definition. Two implementations of a
similarity score that drift apart would move every threshold in this feature by an amount
nobody could see.
"""
from math import sqrt


def _flat(v) -> list:
    """A flat list of floats, from whatever the caller had.

    `.ravel()` by duck typing rather than by importing numpy: the numpy implementation this
    replaces flattened its inputs, and several callers hand it 2-D arrays. Dropping that
    would not raise — a 2-D array iterates into 1-D rows and `float()` on a row raises far
    from here, or worse, on a length-1 row does not raise at all.

    `is None` rather than a truth test: `if not a` on a numpy array raises "the truth value
    of an array with more than one element is ambiguous", which is exactly how the first
    version of this function turned 72 unrelated tests red.
    """
    if v is None:
        return []
    if hasattr(v, "ravel"):
        v = v.ravel().tolist()
    return [float(x) for x in v]


def cosine(a, b) -> float:
    """Cosine similarity. Loudness-invariant on purpose: across 0–6 m the level moves by
    ~20 dB, and a score that moved with it would be measuring the microphone rather than the
    speaker. A zero vector scores 0.0 rather than raising — an empty window is a thing that
    happens, and it is not an error worth failing an extraction over.

    Accepts anything iterable of numbers, including the list `_parse_vector` produces for a
    runtime without pgvector, which is exactly the runtime this exists for.
    """
    av, bv = _flat(a), _flat(b)
    if len(av) != len(bv):
        # Not silently zero: two different embedding models, or a truncated row, would score
        # 0.0 forever and read as "never the same person" rather than as a mistake.
        raise ValueError(f"cosine needs equal lengths, got {len(av)} and {len(bv)}")
    na = sqrt(sum(x * x for x in av))
    nb = sqrt(sum(x * x for x in bv))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return sum(x * y for x, y in zip(av, bv)) / (na * nb)
