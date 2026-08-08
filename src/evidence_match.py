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
        return {"status": "unverified", "reason": "no turns in window"}

    # Concatenated, not tested per turn: turns are per-segment and never merged
    # across chunks, so a sentence split at a chunk seam is two turns and
    # per-turn containment would fail an honest citation.
    norm_turns = [normalise(t.get("text", "")) for t in window]
    haystack = " ".join(norm_turns)
    needle = normalise(quote)
    if not needle:
        return {"status": "unverified", "reason": "empty quote"}

    weak = token_count(quote) < floor_tokens
    ratio = None
    pos = haystack.find(needle)
    if pos < 0:
        pos, ratio = _best_fuzzy(needle, haystack)
        if ratio is None or ratio < fuzzy_threshold:
            return {"status": "unverified", "fuzzy_ratio": ratio}

    turn = _turn_at(window, norm_turns, pos)
    if weak:
        status = "weak"
    else:
        status = "verified" if ratio is None else "verified_fuzzy"
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
    if turn.get("abs_start") and at is not None:
        out["found_offset_sec"] = round(
            abs(turn["abs_start"].timestamp() - at.timestamp()), 1)
    return out


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
