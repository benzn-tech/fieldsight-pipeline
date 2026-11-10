"""The session's turn chain, shared by extraction and the nightly J2 worker.

Lifted out of lambda_extract_session unchanged (spec 2026-11-10-nightly-rematch-agent-design.md
§3 J2): key discovery for one session (`session_base_from_key`, `gather_session_segments` --
chunk `sid{32hex}` sessions group every chunk, legacy keys group by source file) and the turn
assembly (`assemble_session_turns`: normalise, re-time batches, order on the session clock,
drop chunk-seam and batch-window duplicates, audio-event tags, device announcements and the
Ask agent's own answers).

The extractor imports these names back and passes its own S3 client, so its behaviour is the
same as before the move. The S3 client is a parameter (`s3_client`); None builds a default
client once. No psycopg and no extractor import: the non-VPC worker imports this module.
"""
import difflib
import json
import logging
import os
import re

import boto3

import agent_turn_filter
import batch_stitch
import chunk_stitch
from transcript_utils import (
    extract_base_time_from_filename,
    extract_device_from_filename,
    extract_session_id_from_filename,
    normalize_transcript,
)

logger = logging.getLogger(__name__)

_s3_client = None


def _default_s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client('s3')
    return _s3_client


TRANSCRIPTS_PREFIX = 'transcripts/'


# ------------------------------------------------------------
# Device announcements are not speech (2026-08-08)
# ------------------------------------------------------------
# Recorders play spoken prompts — "recording started", "please stop recording" —
# and any device within earshot records them. The transcriber has no way to know
# a machine said it, so they arrive as ordinary speaker turns: five of them in
# one 70-minute session, and in the densest five minutes spk_2 and spk_3 were
# largely machine audio, which is how that session's artifact came to report
# speaker_count: 4 with at least one device counted as a person.
#
# This gets worse exactly as the product gets better. Multi-device grouping
# means every device announces start and stop and every nearby device hears it,
# so the count of these grows with the square of the crew size.
#
# Matched a whole SENTENCE at a time, never as a substring, and every sentence
# in the turn has to match: "we should stop recording now" is a person talking
# about the recorder and must survive. The length guard is the other half of
# that promise.
DEVICE_ANNOUNCEMENT_MAX_CHARS = int(
    os.environ.get('DEVICE_ANNOUNCEMENT_MAX_CHARS', '60'))


# Overridable as a JSON list (see the DeviceAnnouncementPatterns parameter) so a
# phrase met in the field can be added without a code change. Patterns rather
# than literals because a transcriber renders the same prompt differently across
# engines and runs, and because the app can change its wording without telling
# the backend — which is also why the artifact reports what was removed.
#
# The app's voice lines. The first four shipped in GrandTime PR #13 (merged
# 2026-08-07, wired and verified in the release apk — an earlier version of this
# comment said they were staged but unwired, which was wrong) and are still what
# every device older than 0.7.12 says:
#
#   recording_started : "Recording started."
#   recording_stopped : "Recording stopped."
#   meeting_prompt    : "Recording stopped. Has the meeting ended? Check the screen."
#   meeting_ended     : "Meeting ended. Recording stopped."
#
# GrandTime 0.7.12 (branch feat/notification-sounds, 2026-09-15) replaced the
# first two with a recorded set, one clip per event. Wording as supplied with
# the clips; none of these matched the patterns before this change, because the
# match is a whole sentence and "video"/"audio" in front of "recording" was
# enough to miss:
#
#   video_started / video_paused / video_stopped : "Video recording started." / "... paused." / "... stopped."
#   audio_started / audio_paused / audio_stopped : "Audio recording started." / "... paused." / "... stopped."
#   photo_captured : "Photo captured"
#   low_battery    : "Low battery, recording will stop soon."
#   ask_agent      : "Ready, go ahead."
#
# Several lines are MULTI-SENTENCE, which is why matching is done per sentence
# rather than over the whole turn: a whole-turn match caught only the first two.
# Per-sentence also covers the likelier field case — the prompts have audible
# pauses between sentences, so a transcriber may well emit them as separate
# turns, and each one has to be recognisable on its own.
_DEFAULT_ANNOUNCEMENT_PATTERNS = [
    # `stopp?` because English doubles the consonant: "stop", "stopped",
    # "stopping". Without it "Stopped recording." walks straight through, which
    # is what the tests caught.
    r"(please\s+)?(start|stopp?|end)(ed|ing)?\s+(the\s+)?record(ing)?",
    r"(the\s+)?((video|audio)\s+)?record(ing)?\s+(has\s+|is\s+)?(start|stopp?|end|paus)(ed|ing)?",
    r"(the\s+)?meeting\s+(has\s+)?(start|stopp?|end)(ed|ing)?",
    r"(start|end)\s+of\s+(the\s+)?meeting",
    r"record(ing)?\s+(started|stopped|ended)",
    # From meeting_prompt. "check the screen" is deliberately NOT here on its
    # own: a person can plausibly say exactly that on a site, and it carries no
    # recording vocabulary to distinguish it. It is only ever dropped as part of
    # a turn whose other sentences are announcements.
    r"has\s+the\s+meeting\s+ended",
    # From the 0.7.12 recorded set.
    r"photo\s+captured",
    r"(low\s+battery\s+)?recording\s+will\s+stop\s+soon",
    # The Ask start cue. Only ever as ONE sentence: split at the comma into
    # "Ready." and "Go ahead.", each is something a person says on site all
    # day, so neither is a pattern or a companion, and that rendering is
    # deliberately left unfiltered. The owner accepted that a person saying
    # exactly "Ready, go ahead." as a whole turn loses it (2026-09-15).
    r"ready\s+go\s+ahead",
]


# Sentences that are not announcements by themselves, but are recognisable as
# prompt text when they arrive attached to one.
_ANNOUNCEMENT_COMPANIONS = [
    r"check\s+the\s+screen",
    # From low_battery, when the transcriber splits it at the comma. On its own
    # it is a person talking about a tool.
    r"low\s+battery",
]


def _announcement_patterns():
    raw = os.environ.get('DEVICE_ANNOUNCEMENT_PATTERNS')
    if not raw:
        return _DEFAULT_ANNOUNCEMENT_PATTERNS
    try:
        loaded = json.loads(raw)
        if isinstance(loaded, list) and all(isinstance(p, str) for p in loaded):
            # An EMPTY list means "use the defaults", not "filter nothing".
            # '[]' is what both deploy workflows send when the repo variable is
            # unset, because SAM's --parameter-overrides rejects a bare "Key="
            # with an empty value. Reading it as "disable the filter" would turn
            # the feature off on every stack that has not set the variable —
            # which is all of them — silently.
            return loaded or _DEFAULT_ANNOUNCEMENT_PATTERNS
        logger.warning("DEVICE_ANNOUNCEMENT_PATTERNS is not a list of strings; "
                       "using defaults")
    except Exception as e:
        # Never silent (BUG-40): a typo here would otherwise turn the filter off
        # and read as "the announcements came back", sending the next person to
        # the transcriber.
        logger.warning("DEVICE_ANNOUNCEMENT_PATTERNS is not valid JSON (%s); "
                       "using defaults", e)
    return _DEFAULT_ANNOUNCEMENT_PATTERNS


def _normalise_for_match(text):
    """Lowercase, drop punctuation, collapse whitespace. ASR renders the same
    prompt as "Please stop recording." or "please stop recording" depending on
    engine and run, and neither spelling is the interesting part."""
    return re.sub(r'\s+', ' ', re.sub(r'[^\w\s]', ' ', (text or '').lower())).strip()


def is_device_announcement(text):
    """True when every sentence of the turn is recorder prompt text.

    Per sentence, not per turn: two of the four voice lines are multi-sentence
    ("Meeting ended. Recording stopped."), and a whole-turn match caught neither.

    "Every sentence" is what keeps a person safe. A turn has to be prompt text
    end to end, so "Recording stopped. I'll redo that bit." survives — the second
    sentence is a person reporting a gap in the record, which is the most useful
    thing they could say, and it is exactly what a first-sentence-only rule would
    have deleted.
    """
    if not text or len(_normalise_for_match(text)) > DEVICE_ANNOUNCEMENT_MAX_CHARS:
        return False
    patterns = _announcement_patterns()
    sentences = [_normalise_for_match(s) for s in re.split(r'[.!?]+', text)]
    sentences = [s for s in sentences if s]
    if not sentences:
        return False
    matched_an_announcement = False
    for sentence in sentences:
        if any(re.fullmatch(p, sentence) for p in patterns):
            matched_an_announcement = True
        elif not any(re.fullmatch(p, sentence) for p in _ANNOUNCEMENT_COMPANIONS):
            return False
    # A turn of nothing but companions ("Check the screen.") is not an
    # announcement — it needs at least one sentence that actually names the
    # recorder, or a person saying those three words loses their turn.
    return matched_an_announcement


def filter_device_announcements(turns):
    """Return (kept_turns, stats).

    `stats['texts']` keeps the distinct phrases removed. That is the point of
    it: the wording the recorders will actually use is not settled, so the
    filter is also the instrument that reports what it is meeting in the field.
    A filter whose misses are invisible cannot be tuned.
    """
    kept, removed = [], []
    for turn in turns:
        if is_device_announcement(turn.get('text')):
            removed.append(turn)
        else:
            kept.append(turn)
    stats = {
        'removed': len(removed),
        'texts': sorted({(t.get('text') or '').strip() for t in removed}),
    }
    if removed:
        logger.info("Filtered %d device announcement turn(s): %s",
                    len(removed), stats['texts'][:5])
    return kept, stats


# An ElevenLabs audio-event annotation: "[background noise]", "[laughs]",
# "[话筒碰撞声]". Matched by SHAPE, deliberately — the vocabulary is open and
# unstable (the same event came back as both "[点击鼠标]" and "[鼠标点击]" in one
# evaluation, and it differs per language), so a phrase table would be
# permanently behind whatever the engine emits next.
#
# Bounded at 40 characters and no nested "]" so an unclosed bracket in real
# speech cannot swallow the rest of a turn. A tag that long is not a tag.
AUDIO_EVENT_TAG_RE = re.compile(r'\[[^\[\]]{1,40}\]')


# Rollback without a code change, matching how the rest of tonight's changes
# ship. Default on: these reached the extraction prompt, the rolling summary
# and the confirmation email as ordinary speech from the day prod switched to
# ElevenLabs, and nothing downstream can tell them from a person talking.
FILTER_AUDIO_EVENT_TAGS = os.environ.get(
    'FILTER_AUDIO_EVENT_TAGS', 'true').lower() == 'true'


def filter_audio_event_tags(turns):
    """Strip audio-event annotations from turns. Return (kept_turns, stats).

    A turn that is NOTHING but tags is dropped; a turn that mixes them with
    speech keeps the speech. That distinction is the whole design: the tags
    arrive glued to real sentences ("[background noise] So you've got to
    rearrange these fences"), and dropping those turns would delete site
    conversation to remove an annotation.

    Runs BEFORE filter_device_announcements, which is not cosmetic ordering.
    `is_device_announcement` matches whole sentences, so "[background noise]
    Recording started." does not match while the tag is attached — the
    announcement survives as speech unless the tag goes first.

    `stats['tags']` reports the distinct annotations met, for the same reason
    the announcement filter reports its phrases: a filter whose encounters are
    invisible cannot be tuned, and this vocabulary is set by the provider.
    """
    if not FILTER_AUDIO_EVENT_TAGS:
        return list(turns), {'removed': 0, 'stripped': 0, 'tags': []}

    kept, removed, stripped, seen = [], 0, 0, set()
    for turn in turns:
        text = turn.get('text') or ''
        tags = AUDIO_EVENT_TAG_RE.findall(text)
        if not tags:
            kept.append(turn)
            continue
        seen.update(tags)
        cleaned = re.sub(r'\s+', ' ', AUDIO_EVENT_TAG_RE.sub(' ', text)).strip()
        if not cleaned:
            removed += 1
            continue
        stripped += 1
        # Copy rather than mutate: these turn dicts are also held by the
        # normalized transcripts the caller assembled, and a caller that reads
        # them again should see what the transcript actually said.
        kept.append({**turn, 'text': cleaned})
    stats = {'removed': removed, 'stripped': stripped, 'tags': sorted(seen)}
    if removed or stripped:
        logger.info("Audio-event tags: dropped %d tag-only turn(s), cleaned "
                    "%d mixed turn(s); tags seen: %s",
                    removed, stripped, stats['tags'][:8])
    return kept, stats


def session_base_from_key(key):
    """`transcripts/{user}/{date}/{filename}.json` -> (user_folder, date,
    session_base) where session_base = filename minus '.json', VAD offset
    suffix stripped (`.split('_off')[0]`). Returns None for anything that
    isn't a parseable transcript key (wrong prefix/shape, non-.json, or a
    filename transcript_utils itself can't extract a device/base-time from --
    validated here rather than downstream so a bad key is skipped once, with
    a single log line, instead of failing deeper in the pipeline)."""
    parts = key.split('/')
    if len(parts) != 4 or parts[0] != 'transcripts':
        return None
    user_folder, date, filename = parts[1], parts[2], parts[3]
    if not filename.endswith('.json'):
        return None

    device = extract_device_from_filename(filename)
    base_time = extract_base_time_from_filename(filename)
    if not device or device == 'Unknown' or not base_time:
        logger.warning(f"Skipping unparseable transcript key: {key}")
        return None

    # 2026-07 voice-timeliness paradigm: a device-minted session_id groups EVERY
    # ~1-min chunk of one press-record->stop into a single extraction. Without
    # this, `.split('_off')[0]` still carries the per-chunk `_c{NNNN}` token, so
    # each chunk would become its own session_base -> one extraction per minute
    # (the fragmentation risk). When a session_id is present it IS the stable
    # session base (`sid{id}`, identical across all chunks); legacy whole-file
    # keys fall back to the historical per-source-file base unchanged.
    session_id = extract_session_id_from_filename(filename)
    if session_id:
        session_base = f"sid{session_id}"
    else:
        session_base = filename[:-len('.json')].split('_off')[0]
    return user_folder, date, session_base


def gather_session_segments(bucket, user_folder, date, session_base, s3_client=None):
    """List `transcripts/{user_folder}/{date}/` and return the S3 keys
    (sorted) whose OWN session_base matches this one -- i.e. every VAD
    segment (and/or whole-file recording) belonging to the same session,
    never a neighboring session recorded the same day."""
    prefix = f"{TRANSCRIPTS_PREFIX}{user_folder}/{date}/"
    matched = []
    paginator = (s3_client or _default_s3()).get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get('Contents', []):
            key = obj['Key']
            parsed = session_base_from_key(key)
            if parsed is not None and parsed[2] == session_base:
                matched.append(key)
    return sorted(matched)


def _dedup_turn_boundaries(turns, max_window=12):
    """Remove the chunk-overlap duplication the mobile chunk-session contract
    introduces. The device carries ~2s of PCM from one ~30s segment into the
    next (AudioSegmentation.PcmRingBuffer: "a sentence crossing a boundary
    appears whole in both"), so the tail of one segment's turns and the head of
    the next's transcribe the SAME words. For each adjacent pair whose time
    ranges OVERLAP (the later turn starts before the earlier one ends — the seam
    signature), drop the later turn's leading duplicate word-run via
    chunk_stitch.dedup_overlap. Genuinely sequential turns (later starts at/after
    the earlier ends: legacy whole-file recordings, VAD segments, plain
    back-to-back speech) never overlap in time, so this is a NO-OP outside a
    chunked session — the pre-chunk pipeline is byte-for-byte unchanged. `turns`
    must be abs_start-sorted. Returns a new list with fully-overlapping turns
    dropped (the later turn's abs_start is left as-is; a <=2s label wobble is
    immaterial at the prompt's minute granularity)."""
    out = []
    for t in turns:
        if out:
            prev_end = out[-1].get('abs_end')
            cur_start = t.get('abs_start')
            if prev_end is not None and cur_start is not None and cur_start < prev_end:
                kept = chunk_stitch.dedup_overlap(
                    (out[-1].get('text') or '').split(),
                    (t.get('text') or '').split(),
                    max_window=max_window)
                if not kept:
                    continue                          # the whole turn was overlap
                if len(kept) != len((t.get('text') or '').split()):
                    t = dict(t, text=' '.join(kept))  # copy — never mutate the caller's turn
        out.append(t)
    return out


DUP_MAX_GAP_SEC = 3.0     # a person cannot say the same sentence twice this fast
DUP_MIN_RATIO = 0.85      # SequenceMatcher on the lowercased text
DUP_MIN_CHARS = 25        # below this, a repeat is speech, not a re-decode
DUP_LOOKBACK = 6          # turns to compare against; the time gate does the real work
DUP_OPEN_PROBE = 12       # chars of one opening that must appear in...
DUP_OPEN_WINDOW = 24      # ...this much of the other's opening


def _opens_alike(a, b, probe=DUP_OPEN_PROBE, window=DUP_OPEN_WINDOW):
    """Do these two turns START on the same words?

    Similarity alone cannot be trusted here, and the counter-example is a site
    sentence, not a contrived one:

        "The door on level one is damaged and needs replacing."
        "The window on level three is damaged and needs replacing."

    Those score 0.891 — INSIDE the range measured for real re-decodes of the
    same audio (0.857 to 0.980), because they share a long tail. No single
    ratio can separate them, and collapsing them loses a defect report.

    What does separate them: a re-decode of one piece of audio begins on the
    same words, while two observations begin on their two different subjects.
    The probe is offset-tolerant (looked for anywhere in the other's opening)
    so a copy carrying a leading filler — "like, like we're gonna use this"
    against "You know, like, like we're gonna use this" — still matches.

    Measured cost of this gate on the session it was built from: 61 of 750
    duplicates go unmerged (33.2% of characters saved instead of 35.7%). That
    is the right side to err on — a missed duplicate costs tokens, a false one
    costs a record of something that was said.

    Punctuation is stripped before comparing. Two windows over one utterance
    disagree about it freely — the four decodes measured at 13:44:45 split on
    "Oh." against "Oh," alone — and no subject is ever distinguished by a comma.
    """
    a, b = _open_key(a), _open_key(b)
    return bool(a and b) and (a[:probe] in b[:window] or b[:probe] in a[:window])


def _open_key(s):
    return re.sub(r'[^0-9a-z㐀-䶿一-鿿豈-﫿]+', ' ',
                  (s or '').lower()).strip()


_NUMBERY = re.compile(
    r'[0-9０-９]|[一二三四五六七八九十'
    r'百千万两]|'
    r'\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|thirty|'
    r'forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|first|second|third|'
    r'monday|tuesday|wednesday|thursday|friday|saturday|sunday|'
    r'january|february|march|april|june|july|august|september|october|november|december)\b',
    re.I)


_MORPH_PREFIX = 4     # "want"/"wanted" share this much; "tiles"/"soffit" share none


_WORDISH = re.compile(r'[^0-9a-z一-鿿]')


def _diff_is_only_noise(a, b):
    """Are these two texts different only in the ways a RE-DECODE differs?

    The opening-alignment gate stops two statements that BEGIN on different
    subjects. It does nothing about a difference further in, and that is where
    this pass was quietly losing real content. Every one of these merged
    against the live function before this guard existed:

        "Cut the pipe at 2400 and cap it off..."  / "...at 3400..."
        "Make sure you cut it at two metres..."   / "...at three metres..."
        "Leave them 1200mm of clearance..."       / "...1500mm..."
        "...booked for Thursday."                 / "...booked for Tuesday."
        "Level 2 needs the tiles out by Friday"   / "...the soffit out..."
        "...把三楼的瓷砖跟天花板全部拆出来"        / "...把四楼的..."

    Four of the six lost the number. Those are measurements, delivery times and
    floor numbers -- the exact payload the design says must survive. The test
    suite caught none of it, because every guard case happened to put its
    differing token inside the 12-character opening probe.

    So: compare word by word and refuse when a REPLACED span carries meaning.

      - a number, ordinal, weekday or month on either side -> refuse. A
        re-decode of one piece of audio almost never disagrees about a digit,
        and when it does ("150" against "one hundred and fifty") letting the
        duplicate through costs tokens, while merging costs the number.
      - two words of >=4 letters not sharing a 4-letter prefix -> refuse.
        "want"/"wanted" share one; "tiles"/"soffit", "Thursday"/"Tuesday",
        "door"/"window" do not.

    INSERTED and DELETED spans are left alone: a window clipping an utterance
    short, or one copy carrying a leading "You know,", are the ordinary shapes
    of a re-decode.

    This refuses genuine merges too -- "Why has he got his GM" against "Why is
    he called his GM" is a real duplicate this now keeps. That is the intended
    direction: a missed duplicate costs tokens, a false merge costs a record of
    something that was said.
    """
    # Punctuation is stripped BEFORE the word diff, or "friday." against
    # "friday," reads as a replaced span carrying a weekday and vetoes a
    # perfectly ordinary truncation merge.
    wa = [_WORDISH.sub('', w) for w in (a or '').lower().split()]
    wb = [_WORDISH.sub('', w) for w in (b or '').lower().split()]
    wa, wb = [w for w in wa if w], [w for w in wb if w]
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, wa, wb).get_opcodes():
        if tag != 'replace':
            continue
        left, right = ' '.join(wa[i1:i2]), ' '.join(wb[j1:j2])
        if _NUMBERY.search(left) or _NUMBERY.search(right):
            return False
        for side, partners in ((wa[i1:i2], wb[j1:j2]), (wb[j1:j2], wa[i1:i2])):
            for x in side:
                word = _WORDISH.sub('', x)
                if len(word) < _MORPH_PREFIX:
                    continue                      # function words and fillers
                if not any(_WORDISH.sub('', p)[:_MORPH_PREFIX] == word[:_MORPH_PREFIX]
                           for p in partners):
                    return False
    return True


def _dedup_batch_window_repeats(turns, max_gap=DUP_MAX_GAP_SEC,
                                min_ratio=DUP_MIN_RATIO, min_chars=DUP_MIN_CHARS,
                                lookback=DUP_LOOKBACK):
    """Collapse the SAME audio transcribed more than once by overlapping batch
    windows. Returns (turns, stats).

    `_dedup_turn_boundaries` above already handles the device seam, and it is
    the right tool there: it trims an exact leading word-run. It cannot help
    here, for two measured reasons (session sid15770a…, 3,715 turns, after that
    pass had already run):

      1. The recogniser decodes the same audio DIFFERENTLY in each window —
         "they want it" / "they wanted it", "Why has he got his GM" / "Why is he
         called his GM", one copy carrying a leading "You know," the other not.
         An exact word-run match finds nothing to trim in any of those.
      2. 201 pairs were BYTE-IDENTICAL, adjacent, and still survived — because
         that pass is gated on `cur_start < prev_end`, and a batch-rebased turn
         may carry no usable `abs_end` (`_rebase_batch_turns` only overwrites it
         when the end offset resolves). So the gate never fires and the exact
         path is never even reached.

    Hence: no dependence on `abs_end`, and similarity rather than equality.

    The TIME window is the real guard, not the ratio. Two turns from one speaker
    starting within `max_gap` seconds of each other cannot both be genuine — a
    person cannot repeat a 25-character sentence in three seconds — so a
    speaker who really does say something twice is left alone. Measured on that
    session, the drop count saturates at a 3s gap (751 pairs at 3s, 5s and 8s
    alike), so widening the window buys nothing and only risks eating real
    repetition.

    The LONGER text wins: overlapping windows clip the same utterance at
    different points, and the fuller decode is the one worth keeping. The
    surviving turn keeps its original position and timestamps.

    A no-op wherever windows do not overlap (legacy whole-file, VAD segments,
    unbatched chunk sessions): those never produce two same-speaker turns
    starting within 3s with 85% of their text in common.
    """
    kept, dropped, chars_saved = [], 0, 0
    for t in turns:
        text = (t.get('text') or '').strip()
        start = t.get('abs_start')
        match = None
        if len(text) >= min_chars and start is not None:
            for prev in reversed(kept[-lookback:]):
                p_start = prev.get('abs_start')
                p_text = (prev.get('text') or '').strip()
                if p_start is None or prev.get('speaker') != t.get('speaker'):
                    continue
                if abs((start - p_start).total_seconds()) > max_gap:
                    continue
                if len(p_text) < min_chars:
                    continue
                # Compare against what that turn ORIGINALLY said, not the text a
                # previous merge may have written over it. Without this, A absorbs
                # B, then C is measured against B's words and gets dropped even
                # though C never resembled A -- reproduced, and it deleted a
                # "changes on Monday" correction, leaving the transcript
                # asserting Friday.
                p_orig = prev.get('_dedup_orig_text', p_text)
                if not _opens_alike(p_orig, text):
                    continue      # different subjects, not one utterance twice
                if difflib.SequenceMatcher(None, p_orig.lower(),
                                           text.lower()).ratio() < min_ratio:
                    continue
                if not _diff_is_only_noise(p_orig, text):
                    continue      # the difference carries meaning: keep both
                match = prev
                break
        if match is None:
            kept.append(t)
            continue
        dropped += 1
        chars_saved += min(len(match.get('text') or ''), len(text))
        if len(text) > len(match.get('text') or ''):
            # Replace in place so the surviving turn keeps its position and
            # timestamps; copy so a caller's turn dict is never mutated. Carry
            # the original words alongside so later turns are still compared
            # against what was actually said here (see the loop above).
            idx = next(k for k, v in enumerate(kept) if v is match)
            kept[idx] = dict(match, text=text,
                             _dedup_orig_text=match.get('_dedup_orig_text')
                             or (match.get('text') or '').strip())
    out = [{k: v for k, v in t.items() if k != '_dedup_orig_text'} if
           '_dedup_orig_text' in t else t for t in kept]
    return out, {'dropped': dropped, 'chars_saved': chars_saved}


def _is_empty_transcript(data):
    """True when this IS a transcript and it simply holds no words.

    Deliberately conservative: anything that is not recognisably a
    well-formed-but-empty transcript is left to be reported as unreadable, so a
    genuinely broken file is never quietly downgraded to "nothing was said".
    """
    if not isinstance(data, dict):
        return False
    results = data.get('results')
    if not isinstance(results, dict):
        return False
    transcripts = results.get('transcripts')
    if not isinstance(transcripts, list):
        return False
    text = ''.join((t or {}).get('transcript') or '' for t in transcripts
                   if isinstance(t, dict))
    return not text.strip()


def _rebase_batch_turns(bucket, key, normalized, s3_client=None):
    """Re-time a batched transcript's turns through its map.

    A batch's `word.start` is counted from the first sample of the concatenated file — an
    origin that appears in no filename. Filename arithmetic gets the batch's start right
    and then drifts by however much overlap was trimmed at each earlier seam: a second or
    two, invisible in a rendered report, wrong in every timestamp after the first chunk.

    The map is written beside the batch WAV under `audio_segments/`, never beside the
    transcript — putting a `.json` under `transcripts/` would fire this very lambda's own
    S3 trigger. `batch_stitch.map_key_for_transcript` is the one place that swap lives.

    `start_sec` / `end_sec` are deliberately left batch-relative. They are the in-file
    offsets the evidence and playback paths seek with, and the file they name
    (`source_filename`) is the batch WAV — re-basing them would point every quote at a
    position that does not exist in the object it names.

    A missing map keeps the filename times and says so loudly. The error is bounded — a
    batch never spans a gap, by design — and a bounded time error is recoverable in a way
    that a dropped transcript is not.
    """
    if not batch_stitch.is_batch_key(key):
        return normalized
    map_key = batch_stitch.map_key_for_transcript(key)
    if map_key is None:
        logger.warning("batch transcript %s is not shaped like a session key — keeping "
                       "filename arithmetic", key)
        return normalized
    try:
        doc = json.loads((s3_client or _default_s3()).get_object(
            Bucket=bucket, Key=map_key)['Body'].read())
    except Exception:
        logger.warning("batch map %s is unreadable — keeping filename arithmetic for %s, "
                       "so its times may be off by the trimmed overlap", map_key, key)
        return normalized

    for turn in normalized.get('speaker_turns') or []:
        if turn.get('abs_start') is None:
            continue                       # no time to re-base; do not invent one
        start = batch_stitch.resolve_abs_time(doc, turn.get('start_sec') or 0.0)
        if start is None:
            continue
        turn['abs_start'] = start
        turn['abs_start_str'] = start.strftime('%H:%M:%S')
        end = batch_stitch.resolve_abs_time(doc, turn.get('end_sec') or 0.0)
        if end is not None:
            turn['abs_end'] = end
            turn['abs_end_str'] = end.strftime('%H:%M:%S')
        # Each word through the map too: a turn can span two clips, and the
        # silence cut between them is not in the file's own seconds.
        turn['word_abs'] = [batch_stitch.resolve_abs_time(doc, off)
                            for _, off in turn.get('words') or []]
    return normalized


def assemble_session_turns(bucket, keys, s3_client=None):
    """Download + normalize each transcript segment for a session (skipping
    corrupt / unnormalizable ones), collect its abs-timed speaker turns, order
    them on the single session clock, and drop the mobile chunk-overlap
    duplication at device seams (_dedup_turn_boundaries; a no-op on legacy /
    VAD / sequential turns), and drop the recorder's own spoken announcements
    (filter_device_announcements). Returns (turns, source_filenames,
    announcement_stats) — the one clean word stream shared by the Tier-2
    extraction, the Tier-1 rolling summary and the confirmation email, so all
    three describe exactly the same session.

    The announcement filter lives HERE rather than in extract_session because
    it did not, and the gap was invisible: extraction stopped seeing "Recording
    started" while the rolling summary and the finalize email still did, so the
    same session read differently depending on which one you looked at. A rule
    about what counts as speech belongs to the thing that produces the speech.
    """
    client = s3_client or _default_s3()
    normalized_list = []
    source_filenames = []
    for key in keys:
        try:
            obj = client.get_object(Bucket=bucket, Key=key)
            data = json.loads(obj['Body'].read().decode('utf-8'))
        except Exception as e:
            logger.warning(f"Skipping corrupt transcript segment {key}: {e}")
            continue

        filename = key.rsplit('/', 1)[-1]
        normalized = normalize_transcript(data, filename)
        if normalized is not None:
            # A batched transcript's times come from its map, never from the filename.
            normalized = _rebase_batch_turns(bucket, key, normalized, s3_client=client)
        if normalized is None:
            # normalize_transcript returns None for BOTH "this file is not a
            # transcript I can read" and "this is a perfectly good transcript
            # with no words in it" (transcript_utils: `not parsed['full_text']`).
            # Reporting them with one message cost a real investigation: nine
            # segments of one prod session were logged as "unnormalizable", which
            # reads as corruption, and they were empty transcripts of chunks VAD
            # had already judged silent.
            #
            # The distinction now matters more than it did. With
            # DROP_SILENT_CHUNKS on, a silent chunk is never transcribed at all —
            # so from here on an EMPTY transcript means the transcriber found
            # nothing in audio that VAD DID judge to be speech. That is the
            # too-quiet signal the loudness work targets, and it is the metric
            # for whether that work helped. Filed under the same message as a
            # corrupt file, it is invisible.
            if _is_empty_transcript(data):
                logger.info(
                    "No words in transcript %s -- the transcriber returned an "
                    "empty result. This is NOT a parse failure: either the chunk "
                    "was silent, or the speech in it was too quiet to recognise.",
                    key)
            else:
                logger.warning("Skipping unreadable transcript segment %s "
                               "(present, but not in a shape we can normalise)",
                               key)
            continue

        normalized_list.append(normalized)
        source_filenames.append(filename)

    turns = []
    # Stamp each turn with the segment it came from. The two lists are built in
    # the loop above and a skipped segment appends to NEITHER, so zip() pairs
    # them correctly -- but the pairing is load-bearing enough to be pinned by a
    # test, because getting it wrong attributes every quote to the wrong audio
    # file and nothing fails.
    #
    # This is what makes a cited quote resolvable to audio at all. Without it
    # the anchor has to be reverse-derived from an absolute timestamp: each
    # segment's interval recomputed from its filename (BUG-09's arithmetic,
    # already got wrong once here) and then the ~2s ring-buffer overlap
    # disambiguated by hand. turn['start_sec'] is already the in-file offset,
    # so the filename is the only missing half.
    for normalized, filename in zip(normalized_list, source_filenames):
        for turn in normalized.get('speaker_turns', []):
            if turn.get('abs_start') is None:
                continue
            turns.append(dict(turn, source_filename=filename))
    turns.sort(key=lambda t: t['abs_start'])
    turns = _dedup_turn_boundaries(turns)   # drop mobile chunk-overlap dup at seams (no-op pre-chunk)
    turns, repeat_stats = _dedup_batch_window_repeats(turns)  # drop re-decoded audio (no-op pre-batch)
    if repeat_stats['dropped']:
        # Logged, not silent: this number IS the batching rollout's health check.
        # It should be ~0 while BATCH_TRANSCRIPTION is off and jump the moment it
        # is switched on. A session where it stays 0 under batching means the
        # windows stopped overlapping; one where it climbs past ~half the turns
        # means they overlap too far.
        logger.info("Batch-window repeats collapsed: %d turn(s), %d chars, "
                    "%d turn(s) remain", repeat_stats['dropped'],
                    repeat_stats['chars_saved'], len(turns))
    # Tags first: an announcement wearing one ("[background noise] Recording
    # started.") is invisible to the sentence-matching filter below until the
    # tag is gone. See filter_audio_event_tags.
    turns, _tag_stats = filter_audio_event_tags(turns)
    turns, announcement_stats = filter_device_announcements(turns)
    # The Ask agent's own answer, played aloud into the running recording. Same family as the
    # announcements above -- machine speech arriving as an ordinary speaker turn -- but it cannot
    # be pattern-matched, because the text is whatever the model said. It is matched against what
    # the agent is recorded as having said, published per ask by the in-VPC voice-audit function.
    #
    # Derived from the key rather than taken as a parameter so the five callers (extraction,
    # rolling summary, finalize email, group merge) need no signature change and cannot forget it.
    user_folder, date = _user_and_date_from_key(keys[0] if keys else None)
    if user_folder and date:
        turns, agent_stats = agent_turn_filter.apply_agent_filter(
            turns, client, bucket, user_folder, date)
        # Dropped from the stream, not from the record: the transcript JSON and the audio are
        # untouched, so the viewer still shows the exchange. What must not see them is anything
        # derived -- `speaker_count` (a solo wearer plus an agent turn counts as 2 and silently
        # disables item-writer's speaker_count==1 gate), the extraction prompt (whose
        # participants rule would list the agent as a person), and the embedded windows.
        # Removing them here covers all three at one site instead of three.
        turns = [t for t in turns if not t.get('from_agent')]
        announcement_stats = dict(announcement_stats or {}, agent_turns=agent_stats)
    return turns, source_filenames, announcement_stats


def _user_and_date_from_key(key):
    """transcripts/{user_folder}/{date}/{file}.json -> (user_folder, date)."""
    if not key:
        return None, None
    parts = key.split('/')
    if len(parts) < 4 or parts[0] != 'transcripts':
        return None, None
    return parts[1], parts[2]
