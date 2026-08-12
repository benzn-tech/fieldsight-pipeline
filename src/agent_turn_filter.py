"""Mark transcript turns that are the Ask agent's own answer played back into the room.

The device plays SP-Ask's answer aloud while the meeting recording is still running, so the
answer is recorded, transcribed, and extracted as if a person had said it. Measured on prod
2026-08-12: an answer the agent read out of the index became a finding on the daily timeline,
worded as a fact confirmed on site.

That closes a loop -- extraction feeds the index, the index feeds the answer, the answer is
recorded back into a transcript, and the transcript feeds extraction. One statement said once
becomes several mutually corroborating findings across days, and nothing in the report shows
they share a source.

This module is deliberately pure: no S3, no database, no clock. Every caller rebuilds turns
from the raw transcript JSON independently (extraction, ingest, the report generator), so the
decision has to be one function they can all share -- if two of them disagree about which turns
are the agent's, `embed_from_sidecar` raises on the hash mismatch and the whole report fails to
ingest rather than quietly diverging.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import re
from zoneinfo import ZoneInfo

logger = logging.getLogger()

VOICE_ASK_PREFIX = os.environ.get("VOICE_ASK_PREFIX", "voice_ask/")
DEVICE_TZ = ZoneInfo(os.environ.get("DEVICE_TZ", "Pacific/Auckland"))

# Reuses the evidence verifier's shape rather than inventing a second fuzzy matcher.
FLOOR_TOKENS = 5
COVERAGE = 0.80

# `at` is stamped when the answer TEXT is produced -- before the synthesised audio has even been
# returned to the device, let alone played. So the window is lopsided: a little tolerance before
# (a device clock can sit behind the server's) and room for a long answer to finish after.
WINDOW_BEFORE = timedelta(seconds=10)
WINDOW_AFTER = timedelta(seconds=90)


@dataclass(frozen=True)
class AgentAnswer:
    """One thing the agent said, in the transcript's own time base.

    `at_local` is device-local naive time, NOT server UTC: turn `abs_start` comes from the chunk
    filename, which the device stamps with its own wall clock. Converting is the caller's job and
    it has to happen exactly once -- this repo has already shipped one bug from mixing the two.
    """
    at_local: datetime
    text: str


def _normalise(text):
    """Lowercase, drop punctuation, collapse whitespace.

    `[^\\w\\s]`, never `[^0-9a-z]`. The ASCII form erases every CJK character, which makes any
    two Chinese strings compare equal and gives any Chinese turn zero tokens -- this repo has
    shipped that bug twice, and the agent answers Chinese questions in Chinese.
    """
    return re.sub(r'\s+', ' ', re.sub(r'[^\w\s]', ' ', (text or '').lower())).strip()


def _tokens(text):
    """Whitespace tokens, plus each CJK character as its own token.

    Chinese is not whitespace-delimited, so a whole sentence would otherwise be one token and
    never clear the floor.
    """
    out = []
    for word in _normalise(text).split():
        if any('一' <= ch <= '鿿' for ch in word):
            out.extend(ch for ch in word if not ch.isspace())
        else:
            out.append(word)
    return out


def _contained(turn_text, answer_text):
    """Is the turn a piece of the answer?

    Containment, not similarity. One played answer is routinely split across turns by
    diarisation or a chunk boundary: the measured case leaves a 5-token fragment of a 16-token
    sentence, which any symmetric ratio scores around 0.4 and would keep -- filtering the first
    playback and leaving half the agent's sentence in the record.
    """
    turn = _tokens(turn_text)
    if len(turn) < FLOOR_TOKENS:
        # Below the floor, containment means nothing: "yes" is inside almost any answer, and
        # eating one-word human replies is worse than missing a short echo.
        return False
    answer = _tokens(answer_text)
    if not answer:
        return False
    remaining = list(answer)
    hits = 0
    for tok in turn:
        if tok in remaining:
            remaining.remove(tok)   # multiset: a word repeated in the turn needs it repeated
            hits += 1               # in the answer, or the extra copies do not count
    return hits / len(turn) >= COVERAGE


def filter_agent_turns(turns, answers):
    """Return (turns, stats) with agent-originated turns marked `from_agent: True`.

    Marks, never deletes, and never mutates the caller's list. A deleted turn is invisible
    afterwards: if the matching is ever wrong there is no way to find out, and the operator's
    question would read as though nobody answered it. Consumers drop marked turns from
    `speaker_count`, from the prompt, and from the embedded windows -- which is where the loop
    is actually cut.

    `stats['answers_with_no_match']` is load-bearing. A device whose wall clock is out matches
    nothing and looks exactly like a session where nobody asked anything; this codebase has a
    shipped instance of a clock 12 hours wrong. That counter is the only place the failure
    surfaces.
    """
    out = [dict(t) for t in turns]
    matched_total = 0
    unmatched_answers = 0
    claimed = set()

    for answer in answers:
        hit_any = False
        for i, turn in enumerate(out):
            if i in claimed or turn.get("from_agent"):
                continue
            start = turn.get("abs_start")
            if not isinstance(start, datetime):
                # No diarisation collapses the session into one untimed turn. Matching that on
                # text alone would mark the entire recording as the agent.
                continue
            if not (answer.at_local - WINDOW_BEFORE <= start <= answer.at_local + WINDOW_AFTER):
                continue
            if not _contained(turn.get("text"), answer.text):
                continue
            turn["from_agent"] = True
            claimed.add(i)
            matched_total += 1
            hit_any = True
        if not hit_any:
            # A duplicate sidecar entry (the audit hop is at-least-once) finds its turn already
            # claimed and reports no match of its own -- which would inflate this counter with
            # a non-problem. Only count an answer as unmatched when NO turn anywhere matched it.
            if not any(_contained(t.get("text"), answer.text) for t in out):
                unmatched_answers += 1

    return out, {"matched": matched_total, "answers_with_no_match": unmatched_answers}


def load_agent_answers(s3, bucket, user_folder, date):
    """Read every answer this user's agent spoke on `date` (device-local).

    Converted to the transcript's own time base here and nowhere else: turn `abs_start` is a
    naive device-local datetime from the chunk filename, while the sidecar stamps server UTC.
    Doing the conversion once, at the boundary, is the only way the two stop drifting -- this
    repo has already shipped a bug from carrying both bases around.

    Raises on an S3 error rather than returning empty. A "no sidecar" that is really a 403 would
    make one caller filter and another not, and `embed_from_sidecar` turns that disagreement into
    a hash miss that fails the whole report. Absent is fine; unreadable is not, and the two must
    not look alike -- this repo has shipped that exact confusion once already (a missing key
    answering 403 because the role lacked ListBucket).
    """
    if not (bucket and user_folder and date):
        return []
    prefix = f"{VOICE_ASK_PREFIX}{user_folder}/{date}/"
    answers = []
    # Paginator, not list_objects_v2: it is what every other lister in this codebase uses
    # (lambda_ingest._load_turns), so the existing S3 doubles already model it.
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
            row = json.loads(body)
            at_utc = row.get("at_utc")
            text = row.get("answer")
            if not (at_utc and text):
                continue
            at = datetime.fromisoformat(at_utc)
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            answers.append(AgentAnswer(
                at_local=at.astimezone(DEVICE_TZ).replace(tzinfo=None), text=text))
    return answers


def apply_agent_filter(turns, s3, bucket, user_folder, date):
    """Load the day's answers and mark the turns they produced. The one entry point callers use.

    One function, three call sites (the extraction assembler, ingest's loader which embed-report
    shares, and the report generator). They must agree exactly: if two of them disagree about
    which turns are the agent's, the transcript-window chunk hashes diverge and
    `embed_from_sidecar` raises rather than degrading.
    """
    answers = load_agent_answers(s3, bucket, user_folder, date)
    if not answers:
        return turns, {"matched": 0, "answers_with_no_match": 0}
    marked, stats = filter_agent_turns(turns, answers)
    if stats["answers_with_no_match"]:
        # The only signal a skewed device clock ever produces. A device whose wall clock is out
        # matches nothing and is indistinguishable from a day when nobody asked anything.
        logger.warning(
            "agent-turn filter: %d of %d answers matched no turn for %s/%s -- "
            "device clock skew or a missed playback",
            stats["answers_with_no_match"], len(answers), user_folder, date)
    return marked, stats


# Sentence terminators: ASCII plus the CJK forms. Splitting on '.' alone never fires on Chinese,
# which ends sentences with U+3002 -- the same blind spot that made two earlier ASCII-only
# normalisations erase CJK entirely.
_SENTENCE_SPLIT = re.compile(r'(?<=[.!?。！？])\s*')


def filter_agent_text(full_text, answers, file_start_local, duration_sec):
    """Drop agent sentences from a flat transcript string. Returns (text, stats).

    For the report generator, which has no speaker turns -- it keeps `full_text` from
    `parse_transcribe_json` and never builds them. Rebuilding turns there just to filter would
    change the text of EVERY report, including the ones with no agent answer in them, to fix a
    narrow case. This touches the text only when something actually matches.

    The time condition survives, at file granularity: the file's own span must overlap the
    answer's window. Coarser than the per-turn check, and deliberately kept anyway -- containment
    alone would strip a person quoting the agent an hour later, which is a person reporting a
    fact and the only human record of it.

    Same `_contained` underneath as the turn-level path. Two fuzzy matchers meant to agree
    eventually do not.
    """
    if not full_text or not answers or not isinstance(file_start_local, datetime):
        return full_text, {"removed": 0}
    file_end = file_start_local + timedelta(seconds=duration_sec or 0)
    live = [a for a in answers
            if a.at_local - WINDOW_BEFORE <= file_end
            and file_start_local <= a.at_local + WINDOW_AFTER]
    if not live:
        return full_text, {"removed": 0}

    kept, removed = [], 0
    for sentence in _SENTENCE_SPLIT.split(full_text):
        if not sentence.strip():
            continue
        if any(_contained(sentence, a.text) for a in live):
            removed += 1
            continue
        kept.append(sentence.strip())
    return (" ".join(kept) if removed else full_text), {"removed": removed}
