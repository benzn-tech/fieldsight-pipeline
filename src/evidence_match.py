"""Does a cited quote actually appear in the transcript?

The cheap half of claim provenance: it catches the EXTRACTION inventing a claim,
mechanically, with no LLM call and no human. It does NOT catch the ASR inventing
words that the extraction then quotes faithfully -- only a person listening to
the audio catches that. So `verified` means "the extraction did not make this
up", never "true", and nothing built on this may present it otherwise.

Every rule below exists because without it the resulting number would be noise
rather than a measurement. The number IS the deliverable, so an unspecified
detail here is noise added directly to it.
"""
import difflib
import re
import unicodedata

# Worst -> best. `unverified` and `unchecked` are handled separately in roll_up
# because they DOMINATE rather than rank.
STATUS_ORDER = ["weak", "verified_fuzzy", "verified"]

# Why a citation failed. `unverified` on its own is not actionable: it means
# either "our matcher was too strict" or "the model invented it", and the first
# splits again by WHICH rule was too strict -- each with a different fix (W, the
# normaliser, the fuzzy cut). These codes travel into the artifact so a reader
# starts from what the matcher already knew rather than re-deriving it by hand,
# and so the cases the matcher knew for certain cost no reading at all.
REASON_NO_TURNS = "no_turns_in_window"           # W too narrow, or a bad anchor
REASON_NO_CANDIDATE_TEXT = "no_candidate_text"   # turns present but empty: ASR, not W
REASON_EMPTY_QUOTE = "empty_quote"               # nothing was cited
REASON_BELOW_FUZZY = "below_fuzzy_threshold"     # a near miss: blame the cut
REASON_NOTHING_CLOSE = "nothing_close_in_window"  # nothing resembling it is there

# Where "not close" stops being "not close ENOUGH".
#
# One code for both sends the reader to the wrong place. The first real
# unverified citation this feature produced was labelled below_fuzzy_threshold
# at a ratio of 0.331 -- which reads as "the cut is too strict" when the truth
# was that the quote sat 560 seconds away and nothing resembling it was in the
# window at all. Loosening the threshold would not have helped, and looking
# there is wasted time.
#
# Measured 2026-08-10 over two populations: no honestly-tidied quote scored
# below 0.634, and 99% of quotes scored against windows they do not belong to
# scored below 0.625. This constant is that gap.
#
# Deliberately NOT the fuzzy threshold, and deliberately not a deploy
# parameter: it labels, it never decides a status. Tying it to the threshold
# would mean tightening the cut silently starts relabelling genuine near-misses
# as "nothing there", which is the confusion this exists to end.
NOTHING_CLOSE_RATIO = 0.63

# The model's own elision marker. Matched on the RAW quote: `normalise` strips
# punctuation, so by the time a quote is normalised the ellipsis is gone.
_ELLIPSIS = re.compile(r"\s*(?:\.{2,}|…)\s*")

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")
# CJK ideographs + compatibility + fullwidth forms.
_CJK_CLASS = "　-鿿豈-﫿＀-￯"
_CJK = re.compile(f"[{_CJK_CLASS}]")


def _is_cjk(ch):
    return bool(_CJK.match(ch))


def normalise(text):
    """Casefold, drop punctuation, collapse whitespace -- and delete whitespace
    INSIDE CJK runs.

    That last part is not cosmetic. Turn text is space-joined
    (transcript_utils' `' '.join(word_list)`) while a model writing Chinese
    writes it unspaced, so normalised "我 现在" does not contain "我现在" and
    every Chinese citation would read as fabricated. On a bilingual product that
    alone could be most of the unverified count.
    """
    t = unicodedata.normalize("NFKC", text or "").casefold()
    t = _PUNCT.sub(" ", t)
    t = _WS.sub(" ", t).strip()
    out = []
    for i, ch in enumerate(t):
        if (ch == " " and 0 < i < len(t) - 1
                and _is_cjk(t[i - 1]) and _is_cjk(t[i + 1])):
            continue                      # a space between two CJK chars is formatting
        out.append(ch)
    return "".join(out)


def token_count(text):
    """Specificity, counted per script.

    Whitespace tokens alone would score a fully specific Chinese quote
    ("楼板浇筑推迟到周四") as 1 and cap every CJK topic at `weak` -- a bias in
    the headline number that correlates with language, on a product that runs in
    two. CJK characters count at roughly 2 chars per token; a mixed quote sums
    both.
    """
    n = normalise(text)
    cjk = sum(1 for ch in n if _is_cjk(ch))
    latin = len([w for w in _WS.split(_CJK.sub(" ", n)) if w])
    return latin + (cjk + 1) // 2


def windowed_turns(turns, at, w_seconds):
    """Turns whose span intersects [at - W, at + W].

    A window rather than a whole-session search because a quote matching
    somewhere else entirely is a MIS-citation, not a verification -- counting it
    as verified would make the number meaningless.
    """
    if at is None:
        return []
    lo = at.timestamp() - w_seconds
    hi = at.timestamp() + w_seconds
    out = []
    for t in turns:
        start, end = t.get("abs_start"), t.get("abs_end") or t.get("abs_start")
        if not start:
            continue
        if end.timestamp() >= lo and start.timestamp() <= hi:
            out.append(t)
    return out


def check_quote(quote, turns, at, *, w_seconds, floor_tokens, fuzzy_threshold):
    """Verify one quote against the transcript. Returns a dict with `status`
    plus, when matched, the audio anchor.

    Raises for a programming error only; the caller turns any exception into
    `unchecked`, because this is a MEASUREMENT and a measurement that can fail
    an extraction is worse than no measurement.
    """
    window = windowed_turns(turns, at, w_seconds)
    if not window:
        return {"status": "unverified", "reason": REASON_NO_TURNS}

    # Concatenated, not tested per turn: turns are per-segment and never merged
    # across chunks, so a sentence split at a chunk seam is two turns and
    # per-turn containment would fail an honest citation.
    norm_turns = [normalise(t.get("text", "")) for t in window]
    haystack = " ".join(norm_turns)
    needle = normalise(quote)
    if not needle:
        return {"status": "unverified", "reason": REASON_EMPTY_QUOTE}

    weak = token_count(quote) < floor_tokens
    ratio = None
    fragments = 0
    pos = haystack.find(needle)
    if pos < 0:
        # An ellipsis is the model telling us it left something out. Checking
        # each side separately turns a fuzzy guess into an exact test, and it
        # is the only thing that works on this class: how much was elided is
        # the model's choice, so the similarity of the joined string has no
        # lower bound. Measured over 949 live citations, this is what nearly
        # every fuzzy-branch citation actually was.
        pos, fragments = _match_spliced(quote, haystack)
    if pos < 0:
        pos, ratio = _best_fuzzy(needle, haystack)
        if ratio is None:
            # Turns were in the window but carried no words. Widening W would
            # never fix this one, so it must not be filed beside the misses
            # that widening W does fix.
            return {"status": "unverified", "reason": REASON_NO_CANDIDATE_TEXT}
        if ratio < fuzzy_threshold:
            # Which of the two failures it is decides where the next person
            # looks: at our cut, or at the anchor and the transcript.
            reason = (REASON_BELOW_FUZZY if ratio >= NOTHING_CLOSE_RATIO
                      else REASON_NOTHING_CLOSE)
            return {"status": "unverified", "reason": reason,
                    "fuzzy_ratio": round(ratio, 3)}

    turn = _turn_at(window, norm_turns, pos)
    if weak:
        status = "weak"
    elif ratio is None and not fragments:
        status = "verified"
    else:
        # A splice is exact on every fragment, so it is stronger evidence than
        # a fuzzy match -- but it is not `verified`. "A... B" reads as one
        # continuous sentence and is two moments, and in a feature whose whole
        # job is provenance that difference cannot be quietly dropped. It stays
        # in the tier that is counted apart.
        status = "verified_fuzzy"
    out = {"status": status,
           "segment_key_source": turn.get("source_filename"),
           # Turn granularity, not sub-turn: _build_turn joins words into one
           # string and discards per-word timings, so a character position
           # cannot be mapped back to seconds. Under TRANSCRIBE_WHOLE_CHUNK a
           # turn can be a whole 30-60s chunk, so the player may open up to a
           # turn early. A listener starting a minute early still hears the
           # passage; a false precision would be worse.
           "offset_sec": turn.get("start_sec")}
    if ratio is not None:
        out["fuzzy_ratio"] = round(ratio, 3)
    if fragments:
        out["spliced"] = True
        out["fragments"] = fragments
    if turn.get("abs_start") and at is not None:
        out["found_offset_sec"] = round(
            abs(turn["abs_start"].timestamp() - at.timestamp()), 1)
    return out


def _match_spliced(quote, haystack):
    """Every ellipsis-separated fragment present, exactly. Returns
    (earliest position, fragment count), or (-1, 0).

    Transcripts carry no punctuation at all (parse_transcribe_json keeps
    pronunciation items only), so "..." never occurs in the candidate text and
    is unambiguously the model's own elision marker.

    ORDER IS NOT REQUIRED, and that is not laxity. Turns sort by absolute start
    while the mobile ring buffer overlaps chunks, so two transcriptions of the
    same moment can land in either order -- a real 2026-08-10 citation had its
    first-quoted fragment sitting AFTER its second in the candidate text.
    Requiring increasing positions would have failed that honest citation while
    looking rigorous.

    All-or-nothing: one real half must never carry an invented one.
    """
    parts = [normalise(p) for p in _ELLIPSIS.split(quote or "")]
    parts = [p for p in parts if p]
    if len(parts) < 2:
        return -1, 0                      # nothing was actually elided
    positions = []
    for part in parts:
        i = haystack.find(part)
        if i < 0:
            return -1, 0
        positions.append(i)
    # Earliest in the candidate text, which (turns being time-ordered) is the
    # earliest audio -- where a listener should start.
    return min(positions), len(parts)


def _best_fuzzy(needle, haystack):
    """Best difflib ratio over a sliding window of the needle's own length.

    Similarity against the WHOLE window would be near zero for a 10-token quote
    in a 2,000-token haystack regardless of honesty, so it has to be a
    best-alignment search. Stepped at a quarter of the needle to keep it cheap;
    the tier this feeds is a fallback, not the primary test.
    """
    n = len(needle)
    if n == 0 or not haystack:
        return -1, None
    if len(haystack) <= n:
        # The quote is as long as, or longer than, everything in the window.
        # Returning "no match" here would fail every honest citation whose
        # regularisation made it longer than the source ("can t" -> "cannot"),
        # which is the single most common shape in this tier.
        return 0, difflib.SequenceMatcher(None, needle, haystack).ratio()
    best, best_pos = 0.0, -1
    step = max(1, n // 4)
    # +step so the tail of the haystack is always covered even when its length
    # is not a whole number of steps past n.
    for i in range(0, len(haystack) - n + 1 + step, step):
        window = haystack[i:i + n]
        if not window:
            break
        r = difflib.SequenceMatcher(None, needle, window).ratio()
        if r > best:
            best, best_pos = r, i
    return best_pos, best


def _turn_at(window, norm_turns, pos):
    """The turn containing the match START -- the rule for a quote spanning a
    chunk seam, which is exactly why the haystack is concatenated."""
    running = 0
    for turn, nt in zip(window, norm_turns):
        if running <= pos <= running + len(nt):
            return turn
        running += len(nt) + 1            # +1 for the joining space
    return window[0]


def roll_up(statuses):
    """One status for a topic, as a total order rather than a description.

    Order matters and each rule is load-bearing:

      0. no citations at all -> `absent`. Tested FIRST so the "worst remaining"
         rule below is never a minimum over an empty set.
      1. any `unverified` -> `unverified`. A topic with one good and one
         invented citation is not verified.
      2. any `unchecked` -> `unchecked`. It exists to stop OUR OWN bugs
         deflating the signal, so a sibling quote must not mask a verifier
         crash.
      3. otherwise the WORST remaining.
    """
    if not statuses:
        return "absent"
    if "unverified" in statuses:
        return "unverified"
    if "unchecked" in statuses:
        return "unchecked"
    ranked = [s for s in statuses if s in STATUS_ORDER]
    if not ranked:
        return "absent"
    return min(ranked, key=STATUS_ORDER.index)
