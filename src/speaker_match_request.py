"""One shape for "match this session against the company's voiceprints", two producers.

Until 2026-09-23 there was one producer -- `lambda_org_api.speaker_match`, the endpoint no
frontend has ever called -- so the matcher ran only when somebody POSTed to it by hand, and
in practice never. Naming a speaker propagated the name inside that ONE meeting and the next
meeting started from `spk_0` again, which is not what "the system recognises Ben" means to
anybody using it.

Adding a second producer (`lambda_item_writer`, at finalize) is what makes the feature
continuous. It is also exactly the shape this repository has been bitten by: org-api wrote
`label_map`, the writer read it, and the hop between them dropped the key -- label
inheritance was unreachable code that reported success. `#603` is the same failure in the
other direction, one homogeneity rule written three times with two copies fixed and nothing
changing.

So the artifact is built HERE, once, and both producers call this. A field added for one of
them arrives in the other by construction rather than by somebody remembering.

**No numpy, no boto3, no psycopg.** `lambda_item_writer` runs in-VPC on the psycopg layer
and cannot have numpy; `lambda_org_api` runs beside it. This module is arithmetic and dicts,
and the caller does its own S3 write, because the two halves write to different buckets
under different names for reasons that are theirs.
"""
from __future__ import annotations

import uuid

import turn_name_overlay

#: How much audio one embedder invocation may be asked for. The matcher embeds every turn it
#: is given, so a whole day in one artifact is a timeout rather than a slow run.
DEFAULT_SECONDS_PER_RUN = 3600.0
DEFAULT_TURNS_PER_RUN = 400


def label_map(turns) -> list[dict]:
    """(turn_ref, source_filename, speaker_label) for every turn — no audio, no vectors.

    The transcriber's own grouping, in the one form the writer can act on. Sent WHOLE with
    every run rather than sliced per run, because inheritance is the single thing in this
    chain whose answer for one turn depends on ANOTHER turn — which is precisely the
    invariant `split_for_budget` relies on ("no turn's answer depends on another's").
    """
    return [{"turn_ref": turn_name_overlay.turn_ref(t["source_filename"],
                                                    float(t["start_sec"])),
             "source_filename": t["source_filename"],
             "speaker_label": t.get("speaker_label")}
            for t in turns or []]


def split_for_budget(turns, seconds_per_run: float = DEFAULT_SECONDS_PER_RUN,
                     turns_per_run: int = DEFAULT_TURNS_PER_RUN) -> list[list]:
    """Group turns into runs that fit one invocation. Never splits a turn."""
    runs, current, seconds = [], [], 0.0
    for t in turns:
        dur = max(0.0, float(t.get("end_sec", 0)) - float(t.get("start_sec", 0)))
        if current and (seconds + dur > seconds_per_run or len(current) >= turns_per_run):
            runs.append(current)
            current, seconds = [], 0.0
        current.append(t)
        seconds += dur
    if current:
        runs.append(current)
    return runs


def build(company_id, session_base, user_folder, date, turns, mode, source,
          site_id=None, requested_by=None,
          seconds_per_run: float = DEFAULT_SECONDS_PER_RUN,
          turns_per_run: int = DEFAULT_TURNS_PER_RUN) -> list[dict]:
    """Every artifact needed to match this session. Empty list when there is nothing to ask.

    `session_base` is normalised through `turn_name_overlay.session_base` here rather than
    trusted from the caller: two spellings of a session are equal as sessions and not as
    strings, and this repository has already stored one spelling and queried the other,
    twice in one night, one layer apart each time.

    Returns [] for a session with no turns rather than one empty artifact. An artifact that
    asks the embedder to match nothing still costs an invocation, still writes a log line
    that looks like work, and would make "the matcher ran and found nobody" and "there was
    nothing to run it on" the same observation.

    `source` rides on the artifact — `api` when a person asked, `finalize` when the session
    closing asked. Not decoration: when a company's costs jump or a wrong name appears, the
    first question is which producer made it, and nothing else in the artifact answers that.

    It is REQUIRED, with no default, and that is the point of it. A default would be right
    for exactly one caller and silently wrong for the next one added — which is how the
    field would come to say `api` for work nobody asked for, and the one question it exists
    to answer would get a confident wrong answer instead of no answer.
    """
    if source not in ("api", "finalize"):
        raise ValueError(
            f"source must be 'api' or 'finalize', got {source!r} — it is how an operator "
            f"tells an automatic name from one a person requested")
    key = turn_name_overlay.session_base(session_base)
    if not key:
        raise ValueError(
            f"session id must carry its sid (...sid<32 hex>), got {session_base!r} — a "
            f"wrong session key reads another session's turns onto this one")
    if not turns:
        return []
    if not (company_id and user_folder and date):
        raise ValueError(
            "company_id, user_folder and date are all required: the consumer derives its S3 "
            "keys from them, and a missing one surfaces as a NoSuchKey far from its cause")

    whole_map = label_map(turns)
    runs = split_for_budget(turns, seconds_per_run, turns_per_run)
    out = []
    for i, group in enumerate(runs):
        out.append({
            "op": "match",
            "request_id": uuid.uuid4().hex,
            "session_base": key,
            "company_id": str(company_id),
            "user_folder": user_folder,
            "date": date,
            "requested_by": str(requested_by) if requested_by else None,
            "source": source,
            "part": i + 1,
            "of": len(runs),
            "mode": mode,
            "turns": group,
            # Absent when the site could not be resolved, which the matcher reads as "no
            # narrowing" — the behaviour before this feature, and the safe direction.
            "site_id": str(site_id) if site_id else None,
            "label_map": whole_map,
        })
    return out
