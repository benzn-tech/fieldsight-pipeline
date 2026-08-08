# Claim Provenance — Phase A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every extracted topic cite the words it came from, verify that citation against the transcript mechanically, resolve it to an audio position — and produce the number nobody has: what fraction of production topics cite something that is not in their transcript.

**Architecture:** The extraction prompt gains a per-topic `evidence` array (`at` + `quote`). Immediately after the LLM returns, while `turns` is still in hand, a pure matcher checks each quote against the windowed transcript text and stamps a per-quote status plus `segment_key` + `offset_sec`. The result rides the existing artifact into Aurora through a new `evidence jsonb` column.

**Tech Stack:** Python 3.11 Lambda, psycopg3 + Aurora Postgres, DashScope (qwen3.7-max) on prod / Anthropic on the alternate branch, SAM/CloudFormation, pytest.

**Spec:** `docs/superpowers/specs/2026-08-08-claim-provenance-design.md` (v3, three adversarial review rounds). Read it before Task 1 — particularly *What each layer actually catches*, because the whole point is spoiled if `verified` is ever presented as "true".

## Global Constraints

- **Flag:** `EmitEvidence` → env `EMIT_EVIDENCE`. Workflow fallbacks: `deploy.yml` `true`, `deploy-prod.yml` **`false`**. Template Parameter default `false`. Four changes, not one (BUG-38 — a CLI override replaces samconfig wholesale). Copy the `DeviceAnnouncementPatterns` block (`template.yaml:1533-1543`).
- **`verified` means "the extraction did not make this up" — never "true".** An ASR fabrication that the extraction quotes faithfully verifies. Any log line, field name, or comment that implies otherwise is a defect.
- **Verification must never fail an extraction.** It is a measurement; a measurement that can destroy what it measures is worse than none. Every failure path ends in `unchecked` + a loud log (BUG-40: never a silent except).
- **Migration number:** take the next free one at merge time. `0036` is likely taken — several workstreams allocate from the same sequence and the user runs parallel sessions.
- **Group (multi-device) extractions are out of scope.** The matcher windows on absolute time; group turn lists deliberately have no shared clock (a 12-hour device skew is shipped history), so an honest quote from a second device would be manufactured into an `unverified` and poison the number. `verify_evidence` skips any artifact whose `tier` is `group`.
- **Prod runs qwen3.7-max, not Anthropic.** The `deploy-prod.yml` fallback says `anthropic`; the deployed function says `LLM_PROVIDER=qwen`. Check the function, not the workflow.
- Run `python -m pytest tests/unit -q` before every commit (~2025 tests, ~15s).
- Branch from a freshly fetched `origin/develop` in a clean worktree. `export MSYS_NO_PATHCONV=1` for AWS CLI calls with `/`-prefixed args (BUG-42).

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `src/transcript_utils.py` | turns carry `source_filename` | 1 |
| `src/lambda_extract_session.py` | assembly stamping, schema, prompt rule, verification wiring | 1, 3, 4 |
| `src/evidence_match.py` | **new** — pure matcher: normalise, window, contain, fuzzy, floor, rollup | 2 |
| `src/migrations/00NN_topic_evidence.sql` | `evidence jsonb` on `topics` | 5 |
| `src/repositories/topics.py` | `upsert_topic` kwarg + `_TOPIC_COLS` | 5 |
| `src/lambda_item_writer.py` | pass `evidence` through | 5 |
| `src/template.yaml`, both workflows | flag, env, anthropic ceiling | 3, 6 |

---

## Task 1: turns carry the file they came from

The one enabling change. Without it the anchor cannot be resolved and the whole design is a reverse-lookup module the spec exists to avoid.

**Files:**
- Modify: `src/lambda_extract_session.py` (`assemble_deduped_turns`, ~`:682-741`)
- Test: `tests/unit/test_turn_source_filename.py`

**Interfaces:**
- Consumes: nothing
- Produces: every turn dict returned by `assemble_deduped_turns` carries `source_filename: str` alongside `start_sec` (already present, and already the in-file offset — `transcript_utils.py:283` documents it as "offset from segment start (relative)").

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_turn_source_filename.py
"""A turn must know which transcript it came from.

Without this the audio anchor has to be reverse-engineered from an absolute
timestamp — re-deriving each segment's interval from its filename (BUG-09's
arithmetic, already got wrong once in this repo) and disambiguating the ~2s
ring-buffer overlap where two chunks cover the same instant. Carrying the
filename forward costs one zip()."""
import pytest

ex = pytest.importorskip("lambda_extract_session", reason="requires the lambda deps")


def test_every_turn_carries_its_source_filename(monkeypatch):
    def fake_normalize(bucket, key):
        name = key.split("/")[-1]
        return {"speaker_turns": [
            {"speaker": "spk_0", "text": "hello there", "start_sec": 1.5,
             "end_sec": 3.0, "abs_start": _dt(name), "abs_end": _dt(name),
             "abs_start_str": "14:00:00", "abs_end_str": "14:00:03"}]}, name

    monkeypatch.setattr(ex, "_load_one_transcript", fake_normalize)
    turns, files = ex.assemble_deduped_turns("bkt", ["transcripts/a/d/one.json",
                                                     "transcripts/a/d/two.json"])
    assert len(turns) == 2
    assert {t["source_filename"] for t in turns} == {"one.json", "two.json"}
    # start_sec is untouched: it is already the in-file offset the player seeks to.
    assert all(t["start_sec"] == 1.5 for t in turns)


def _dt(name):
    from datetime import datetime
    return datetime(2026, 8, 7, 14, 0, 0 if "one" in name else 30)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/unit/test_turn_source_filename.py -v`
Expected: FAIL — `KeyError: 'source_filename'`.

- [ ] **Step 3: Implement**

`source_filenames` is appended in the *download* loop (`:730-731`) and paired with `normalized_list` by index, so the assembly loop must zip them:

```python
    turns = []
    # Carry the segment filename onto every turn. The audio anchor for a cited
    # quote is (this file, turn.start_sec); without it the anchor would have to
    # be reverse-derived from an absolute timestamp, which means redoing BUG-09's
    # arithmetic per segment and resolving the ~2s chunk overlap by hand.
    for normalized, filename in zip(normalized_list, source_filenames):
        for turn in normalized.get('speaker_turns', []):
            if turn.get('abs_start') is None:
                continue
            turns.append(dict(turn, source_filename=filename))
    turns.sort(key=lambda t: t['abs_start'])
    turns = _dedup_turn_boundaries(turns)
    return turns, source_filenames
```

`_dedup_turn_boundaries` rebuilds turns with `dict(t, ...)` (`:382`), so the new key survives it.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/unit/test_turn_source_filename.py tests/unit/test_lambda_extract_session.py -v`
Expected: PASS. The existing extract-session tests must still pass — they pin the turn contract.

- [ ] **Step 5: Commit**

```bash
python -m pytest tests/unit -q
git add src/lambda_extract_session.py tests/unit/test_turn_source_filename.py
git commit -m "feat(evidence): a turn knows which transcript it came from"
```

---

## Task 2: the matcher

A standalone module of pure functions, because the Phase A number is the deliverable and every unspecified detail is noise added directly to it.

**Files:**
- Create: `src/evidence_match.py`
- Test: `tests/unit/test_evidence_match.py`

**Interfaces:**
- Consumes: turn dicts from Task 1
- Produces:
  - `normalise(text: str) -> str`
  - `token_count(text: str) -> int` — script-aware
  - `windowed_turns(turns: list, at: datetime, w_seconds: float) -> list`
  - `check_quote(quote: str, turns: list, at: datetime, *, w_seconds, floor_tokens, fuzzy_threshold) -> dict` → `{"status", "segment_key_source", "offset_sec", "found_offset_sec", "fuzzy_ratio"}`
  - `roll_up(statuses: list[str]) -> str`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_evidence_match.py
"""The matcher. Each test here is a documented way the number could have been
noise instead of a measurement."""
from datetime import datetime

import pytest

em = pytest.importorskip("evidence_match")

AT = datetime(2026, 8, 7, 14, 23, 7)


def _turn(text, start_sec=0.0, at=AT, fn="c0000.json"):
    return {"text": text, "start_sec": start_sec, "abs_start": at,
            "abs_end": at, "source_filename": fn}


def test_an_honest_quote_is_verified():
    turns = [_turn("the slab pour is pushed to Thursday because of the pump")]
    r = em.check_quote("the slab pour is pushed to Thursday", turns, AT,
                       w_seconds=90, floor_tokens=5, fuzzy_threshold=0.9)
    assert r["status"] == "verified"
    assert r["segment_key_source"] == "c0000.json" and r["offset_sec"] == 0.0


def test_casing_and_punctuation_do_not_break_it():
    turns = [_turn("the slab pour is pushed to thursday")]
    r = em.check_quote("The slab pour is pushed to Thursday.", turns, AT,
                       w_seconds=90, floor_tokens=5, fuzzy_threshold=0.9)
    assert r["status"] == "verified"


def test_a_quote_spanning_a_chunk_seam_verifies():
    # Turns are per-segment and never merged across chunks, so a sentence split
    # at a seam is two turns. Testing each turn alone would fail an honest
    # citation; the candidate text is the concatenation.
    turns = [_turn("the slab pour is", 0.0), _turn("pushed to Thursday", 0.0)]
    r = em.check_quote("the slab pour is pushed to Thursday", turns, AT,
                       w_seconds=90, floor_tokens=5, fuzzy_threshold=0.9)
    assert r["status"] == "verified"
    # anchors to the turn containing the match START
    assert r["segment_key_source"] == "c0000.json"


def test_cjk_verifies_despite_space_joined_turn_text():
    # Turn text is space-joined while the model writes CJK unspaced. Without
    # stripping whitespace inside CJK runs this fails, and on a bilingual
    # product that would be most of the unverified count.
    turns = [_turn("楼板 浇筑 推迟 到 周四")]
    r = em.check_quote("楼板浇筑推迟到周四", turns, AT,
                       w_seconds=90, floor_tokens=5, fuzzy_threshold=0.9)
    assert r["status"] == "verified"


def test_a_specific_cjk_quote_is_not_weak():
    # 9 characters is specific. Counting whitespace-delimited tokens would make
    # it 1 and cap every CJK topic at `weak` — a language-correlated bias in the
    # headline number.
    assert em.token_count("楼板浇筑推迟到周四") >= 4


def test_a_quote_absent_from_the_transcript_is_unverified():
    turns = [_turn("we talked about the crane and the weather")]
    r = em.check_quote("the market is now coming down", turns, AT,
                       w_seconds=90, floor_tokens=5, fuzzy_threshold=0.9)
    assert r["status"] == "unverified"


def test_a_quote_found_outside_the_window_is_unverified():
    far = datetime(2026, 8, 7, 15, 30, 0)
    turns = [_turn("the slab pour is pushed to Thursday", at=far)]
    r = em.check_quote("the slab pour is pushed to Thursday", turns, AT,
                       w_seconds=90, floor_tokens=5, fuzzy_threshold=0.9)
    assert r["status"] == "unverified", "a match somewhere else is a mis-citation"


def test_a_short_quote_is_weak_not_verified():
    turns = [_turn("yes we should stop")]
    r = em.check_quote("yes", turns, AT,
                       w_seconds=90, floor_tokens=5, fuzzy_threshold=0.9)
    assert r["status"] == "weak", "a one-word quote verifies against anything"


def test_regularisation_lands_in_the_fuzzy_tier_not_verified():
    turns = [_turn("we can t get the pump before then")]      # ASR has no apostrophes
    r = em.check_quote("we cannot get the pump before then", turns, AT,
                       w_seconds=90, floor_tokens=5, fuzzy_threshold=0.9)
    assert r["status"] == "verified_fuzzy"
    assert r["fuzzy_ratio"] >= 0.9


def test_rollup_unverified_dominates():
    assert em.roll_up(["verified", "unverified"]) == "unverified"


def test_rollup_unchecked_is_never_masked_by_a_good_sibling():
    # The status exists to stop our own code bugs deflating the signal; a
    # sibling quote must not hide it.
    assert em.roll_up(["verified", "unchecked"]) == "unchecked"


def test_rollup_takes_the_worst_not_the_best():
    assert em.roll_up(["verified", "weak"]) == "weak"
    assert em.roll_up(["verified", "verified_fuzzy"]) == "verified_fuzzy"


def test_rollup_of_no_evidence_is_absent():
    assert em.roll_up([]) == "absent"
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/unit/test_evidence_match.py -v`
Expected: FAIL — module `evidence_match` not found.

- [ ] **Step 3: Implement**

```python
# src/evidence_match.py
"""Does a cited quote actually appear in the transcript?

This is the cheap half of claim provenance: it catches the EXTRACTION inventing
a claim, mechanically, with no LLM call and no human. It does NOT catch the ASR
inventing words that the extraction then quotes faithfully -- only a person
listening to the audio catches that. `verified` therefore means "the extraction
did not make this up", never "true", and nothing here should be presented
otherwise.

Every rule below exists because without it the resulting number would be noise
rather than a measurement.
"""
import difflib
import re
import unicodedata

STATUS_ORDER = ["weak", "verified_fuzzy", "verified"]      # worst -> best
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")
_CJK = re.compile(r"[　-鿿豈-﫿＀-￯]")


def _is_cjk(ch):
    return bool(_CJK.match(ch))


def normalise(text):
    """Casefold, drop punctuation, collapse whitespace -- and delete whitespace
    INSIDE CJK runs.

    That last part is not cosmetic. Turn text is space-joined
    (transcript_utils.py:383) while CJK arrives with variable spacing, so
    normalised "我 现在" does not contain "我现在" and every Chinese citation
    would read as fabricated."""
    t = unicodedata.normalize("NFKC", text or "").casefold()
    t = _PUNCT.sub(" ", t)
    t = _WS.sub(" ", t).strip()
    out = []
    for i, ch in enumerate(t):
        if ch == " " and 0 < i < len(t) - 1 and _is_cjk(t[i - 1]) and _is_cjk(t[i + 1]):
            continue                      # a space between two CJK chars is formatting
        out.append(ch)
    return "".join(out)


def token_count(text):
    """Specificity, counted per script.

    Whitespace tokens alone would score a fully specific Chinese quote
    ("楼板浇筑推迟到周四") as 1 and cap every CJK topic at `weak` -- a bias in the
    headline number that correlates with language. CJK characters count at
    roughly 2 chars per token."""
    n = normalise(text)
    cjk = sum(1 for ch in n if _is_cjk(ch))
    latin = len([w for w in _WS.split(_CJK.sub(" ", n)) if w])
    return latin + (cjk + 1) // 2


def windowed_turns(turns, at, w_seconds):
    """Turns whose span intersects [at-W, at+W].

    A window rather than a whole-session search because a quote matching
    somewhere else entirely is a MIS-citation, not a verification -- counting it
    as verified would make the number meaningless."""
    lo = at.timestamp() - w_seconds
    hi = at.timestamp() + w_seconds
    return [t for t in turns
            if t.get("abs_start") and t.get("abs_end")
            and t["abs_end"].timestamp() >= lo and t["abs_start"].timestamp() <= hi]


def check_quote(quote, turns, at, *, w_seconds, floor_tokens, fuzzy_threshold):
    """Verify one quote. Returns status plus the audio anchor.

    Never raises for a data reason -- the caller turns an exception into
    `unchecked`, because this is a measurement and a measurement that can fail
    an extraction is worse than no measurement."""
    window = windowed_turns(turns, at, w_seconds)
    if not window:
        return {"status": "unverified", "reason": "no turns in window"}

    # Concatenated, not per-turn: turns are per-segment and never merged across
    # chunks, so a sentence split at a chunk seam is two turns.
    norm_turns = [normalise(t.get("text", "")) for t in window]
    haystack = " ".join(norm_turns)
    needle = normalise(quote)
    weak = token_count(quote) < floor_tokens

    pos = haystack.find(needle)
    ratio = None
    if pos < 0:
        pos, ratio = _best_fuzzy(needle, haystack)
        if ratio is None or ratio < fuzzy_threshold:
            return {"status": "unverified", "fuzzy_ratio": ratio}

    turn = _turn_at(window, norm_turns, pos)
    return {
        "status": "weak" if weak else ("verified" if ratio is None else "verified_fuzzy"),
        "fuzzy_ratio": ratio,
        "segment_key_source": turn.get("source_filename"),
        # Turn granularity, not sub-turn: _build_turn joins words into one string
        # and discards per-word timings, so a character position cannot be mapped
        # back to seconds. The player may open up to a turn early, which for a
        # listener is fine and for a false precision would not be.
        "offset_sec": turn.get("start_sec"),
        "found_offset_sec": abs(turn["abs_start"].timestamp() - at.timestamp()),
    }


def _best_fuzzy(needle, haystack):
    """Best difflib ratio over a sliding window of the needle's own length.

    Similarity against the WHOLE window would be near zero for a 10-token quote
    in a 2,000-token haystack regardless of honesty."""
    n = len(needle)
    if n == 0 or len(haystack) < n:
        return -1, None
    best, best_pos = 0.0, -1
    step = max(1, n // 4)
    for i in range(0, len(haystack) - n + 1, step):
        r = difflib.SequenceMatcher(None, needle, haystack[i:i + n]).ratio()
        if r > best:
            best, best_pos = r, i
    return best_pos, best


def _turn_at(window, norm_turns, pos):
    """The turn containing the match START (the rule for a seam-spanning quote)."""
    running = 0
    for turn, nt in zip(window, norm_turns):
        if running <= pos <= running + len(nt):
            return turn
        running += len(nt) + 1            # +1 for the joining space
    return window[0]


def roll_up(statuses):
    """One status for the topic. A total order, not a description.

    `unverified` dominates because a topic with one good and one invented
    citation is not verified. `unchecked` outranks everything below it because
    it exists to stop OUR bugs deflating the signal -- a sibling quote must not
    mask a verifier crash."""
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
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/unit/test_evidence_match.py -v`
Expected: PASS, 13 tests.

- [ ] **Step 5: Commit**

```bash
python -m pytest tests/unit -q
git add src/evidence_match.py tests/unit/test_evidence_match.py
git commit -m "feat(evidence): the matcher, specified so its number is a measurement"
```

---

## Task 3: schema, prompt rule, and the flag

**Files:**
- Modify: `src/lambda_extract_session.py` (`EXTRACTION_SCHEMA` ~`:391-433`, the rules block ~`:591-603`), `src/template.yaml`, both workflows
- Test: `tests/unit/test_evidence_prompt_and_flag.py`

**Interfaces:**
- Consumes: nothing
- Produces: `EMIT_EVIDENCE` env → module constant; schema carries a per-topic `evidence` array of `{at, quote}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_evidence_prompt_and_flag.py
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ex = pytest.importorskip("lambda_extract_session", reason="requires the lambda deps")


def test_the_schema_asks_for_evidence_on_topics():
    props = ex.EXTRACTION_SCHEMA["properties"]["topics"]["items"]["properties"]
    assert "evidence" in props
    item = props["evidence"]["items"]["properties"]
    assert set(item) >= {"at", "quote"}


def test_the_prompt_demands_verbatim_quotes():
    # Without the word verbatim the model regularises more, and every
    # regularisation lands in the fuzzy tier or below -- inflating the
    # false-unverified rate the whole measurement is trying to keep low.
    prompt, _ = ex.build_extraction_prompt("Ben_UCPK", "2026-08-07", "sid" + "a" * 32,
                                           [], 0)
    assert "verbatim" in prompt.lower()


def test_prod_workflow_fallback_is_off_and_test_is_on():
    prod = (ROOT / ".github/workflows/deploy-prod.yml").read_text(encoding="utf-8")
    test = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    assert "EmitEvidence=${{ vars.PROD_EMIT_EVIDENCE || 'false' }}" in prod
    assert "EmitEvidence=${{ vars.TEST_EMIT_EVIDENCE || 'true' }}" in test


def test_parameter_defaults_off():
    text = (ROOT / "src/template.yaml").read_text(encoding="utf-8")
    m = re.search(r"\n  EmitEvidence:\n(?:.*\n)*?\s*Default: '?(\w+)'?\n", text)
    assert m and m.group(1) == "false"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/unit/test_evidence_prompt_and_flag.py -v`
Expected: FAIL — `evidence` not in the schema.

- [ ] **Step 3: Add the schema field**

Inside `EXTRACTION_SCHEMA`'s topic item `properties`:

```python
                    "evidence": {
                        "type": "array",
                        "description": (
                            "The transcript lines this topic came from. Quote "
                            "VERBATIM from the transcript above -- do not tidy, "
                            "correct or paraphrase. `at` is the [HH:MM:SS] of "
                            "the line the quote starts in."),
                        "items": {
                            "type": "object",
                            "properties": {
                                "at": {"type": "string"},
                                "quote": {"type": "string"},
                            },
                            "required": ["at", "quote"],
                        },
                    },
```

- [ ] **Step 4: Add the instruction**

In the numbered rules block:

```
N. EVIDENCE. For each topic, give 1-2 `evidence` entries quoting the transcript
   lines the topic came from. Quote VERBATIM -- copy the words exactly as they
   appear above, including any that look like transcription errors. Do not add
   evidence inside action_items or findings. Prefer a full clause over a few
   words; a quote of two or three words proves nothing.
```

The verbatim instruction is load-bearing: without it the model regularises,
every regularisation drops to the fuzzy tier or below, and the false-unverified
rate swamps the fabrication rate the measurement exists to find.

- [ ] **Step 5: Wire the flag**

`src/template.yaml` Parameter:

```yaml
  EmitEvidence:
    Type: String
    AllowedValues: ['true', 'false']
    # Off by default. Both workflows pass an explicit value, so this is only
    # reached by a manual `sam deploy` -- the path NormaliseAudio nearly shipped
    # on by accident.
    Default: 'false'
    Description: Ask the extraction to cite its evidence, and verify the citations.
```

`ExtractSessionFunction` env: `EMIT_EVIDENCE: !Ref EmitEvidence`.
`deploy.yml`: `"EmitEvidence=${{ vars.TEST_EMIT_EVIDENCE || 'true' }}" \`
`deploy-prod.yml`: `"EmitEvidence=${{ vars.PROD_EMIT_EVIDENCE || 'false' }}" \`

In the module: `EMIT_EVIDENCE = os.environ.get('EMIT_EVIDENCE', 'false').lower() == 'true'`, and the schema/rule are added to the prompt only when it is on.

- [ ] **Step 6: Run the tests and commit**

```bash
python -m pytest tests/unit -q
git add src/lambda_extract_session.py src/template.yaml .github/workflows/deploy.yml \
        .github/workflows/deploy-prod.yml tests/unit/test_evidence_prompt_and_flag.py
git commit -m "feat(evidence): ask the extraction to cite, behind a flag that is off on prod"
```

---

## Task 4: verify, anchor, and count

**Files:**
- Modify: `src/lambda_extract_session.py`
- Test: `tests/unit/test_evidence_verification.py`

**Interfaces:**
- Consumes: `evidence_match` (Task 2); turns with `source_filename` (Task 1)
- Produces: `verify_evidence(result, turns, session_date) -> dict` — mutates each topic's `evidence` in place adding `status`, `segment_key`, `offset_sec`; sets `evidence_status` per topic; returns `{"verified", "verified_fuzzy", "weak", "unverified", "absent", "unchecked"}` counts, logged once per extraction.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_evidence_verification.py
from datetime import datetime

import pytest

ex = pytest.importorskip("lambda_extract_session", reason="requires the lambda deps")

AT = datetime(2026, 8, 7, 14, 23, 7)


def _turns():
    return [{"text": "the slab pour is pushed to Thursday", "start_sec": 4.0,
             "abs_start": AT, "abs_end": AT, "source_filename":
             "ben_2026-08-07_14-23-00_sidX_c0007_off0.0_to30.0_srcwav.json"}]


def test_a_verified_quote_gets_an_audio_anchor():
    result = {"topics": [{"title": "Slab", "evidence": [
        {"at": "14:23:07", "quote": "the slab pour is pushed to Thursday"}]}]}
    counts = ex.verify_evidence(result, _turns(), "2026-08-07")
    ev = result["topics"][0]["evidence"][0]
    assert ev["status"] == "verified"
    # transcripts/...json -> audio_segments/...wav: same basename, and the
    # extension is ALWAYS .wav even when the name says srcmp4 (that token records
    # the SOURCE format, not the segment's).
    assert ev["segment_key"].startswith("audio_segments/")
    assert ev["segment_key"].endswith(".wav")
    assert ev["offset_sec"] == 4.0
    assert counts["verified"] == 1


def test_an_invented_quote_is_unverified_and_still_stored_verbatim():
    invented = "the market is now coming down"
    result = {"topics": [{"title": "Market", "evidence": [
        {"at": "14:23:07", "quote": invented}]}]}
    ex.verify_evidence(result, _turns(), "2026-08-07")
    ev = result["topics"][0]["evidence"][0]
    assert ev["status"] == "unverified"
    assert ev["quote"] == invented, "the model's own words ARE the evidence"


def test_a_topic_with_no_evidence_is_absent_and_still_written():
    result = {"topics": [{"title": "Something"}]}
    counts = ex.verify_evidence(result, _turns(), "2026-08-07")
    assert result["topics"][0]["evidence_status"] == "absent"
    assert counts["absent"] == 1


def test_a_raising_matcher_yields_unchecked_and_never_propagates(monkeypatch):
    def boom(*a, **k):
        raise ValueError("bug in the matcher")
    monkeypatch.setattr(ex.evidence_match, "check_quote", boom)
    result = {"topics": [{"title": "T", "evidence": [{"at": "14:23:07", "quote": "x y z a b"}]}]}
    counts = ex.verify_evidence(result, _turns(), "2026-08-07")   # must not raise
    assert result["topics"][0]["evidence_status"] == "unchecked"
    assert counts["unchecked"] == 1


def test_a_group_extraction_is_skipped_entirely():
    # The matcher windows on absolute time; group turn lists have no shared
    # clock by design, so an honest quote from a second device would land
    # outside W and be counted as fabrication.
    result = {"tier": "group", "topics": [{"title": "T", "evidence": [
        {"at": "14:23:07", "quote": "the slab pour is pushed to Thursday"}]}]}
    counts = ex.verify_evidence(result, _turns(), "2026-08-07")
    assert counts == {}, "group extractions must not be scored"
    assert "status" not in result["topics"][0]["evidence"][0]


def test_evidence_below_the_topic_level_is_stripped():
    # The model may volunteer evidence inside action items. Aurora drops it
    # (explicit columns), but it costs output tokens and would leave an
    # unverified citation in the artifact for a reader to trust.
    result = {"topics": [{"title": "T", "action_items": [
        {"text": "do it", "evidence": [{"at": "14:23:07", "quote": "anything"}]}]}]}
    ex.verify_evidence(result, _turns(), "2026-08-07")
    assert "evidence" not in result["topics"][0]["action_items"][0]
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/unit/test_evidence_verification.py -v`
Expected: FAIL — `verify_evidence` not defined.

- [ ] **Step 3: Implement**

```python
import evidence_match

EVIDENCE_WINDOW_SEC = float(os.environ.get('EVIDENCE_WINDOW_SEC', '300'))
EVIDENCE_FLOOR_TOKENS = int(os.environ.get('EVIDENCE_FLOOR_TOKENS', '5'))
EVIDENCE_FUZZY = float(os.environ.get('EVIDENCE_FUZZY_THRESHOLD', '0.9'))


def _segment_key_for(transcript_filename, user_folder, date):
    """transcripts/{u}/{d}/{base}.json -> audio_segments/{u}/{d}/{base}.wav.

    Always .wav: an `srcmp4` token in the name records the SOURCE format, not
    the segment's -- VAD writes 16k wav for every emitted unit."""
    base = transcript_filename.rsplit('.', 1)[0]
    return f"audio_segments/{user_folder}/{date}/{base}.wav"


def _parse_at(at_str, session_date, turns):
    """The model returns a bare HH:MM:SS; turns carry full datetimes. Attach the
    session date, and for a session crossing midnight take the occurrence
    nearest the session's own span (BUG-37's family, inside the matcher)."""
    from datetime import datetime, timedelta
    t = datetime.strptime(f"{session_date} {at_str}", "%Y-%m-%d %H:%M:%S")
    if not turns:
        return t
    span_mid = turns[len(turns) // 2]["abs_start"]
    return min((t, t + timedelta(days=1), t - timedelta(days=1)),
               key=lambda c: abs((c - span_mid).total_seconds()))


def verify_evidence(result, turns, session_date, user_folder=None, date=None):
    """Check every cited quote against the transcript the model actually saw.

    Catches the EXTRACTION inventing. Does NOT catch the ASR inventing words the
    extraction then quotes faithfully -- `verified` means "not made up here",
    never "true".

    Never raises: this is a measurement, and a measurement that can fail an
    extraction is worse than no measurement."""
    if result.get('tier') == 'group':
        # No shared clock across devices (assemble_group_turns' own reasoning),
        # so an absolute-time window would manufacture unverified from honest
        # citations on the other device's clock. Scored per source, or not at all.
        logger.info("evidence: skipping group extraction -- no shared clock")
        return {}
    counts = {k: 0 for k in ("verified", "verified_fuzzy", "weak",
                             "unverified", "absent", "unchecked")}
    for topic in result.get('topics') or []:
        for child_key in ('action_items', 'findings'):
            for child in topic.get(child_key) or []:
                child.pop('evidence', None)          # topic level only
        evidence = topic.get('evidence') or []
        statuses = []
        for ev in evidence:
            try:
                at = _parse_at(ev.get('at', ''), session_date, turns)
                r = evidence_match.check_quote(
                    ev.get('quote', ''), turns, at,
                    w_seconds=EVIDENCE_WINDOW_SEC,
                    floor_tokens=EVIDENCE_FLOOR_TOKENS,
                    fuzzy_threshold=EVIDENCE_FUZZY)
            except Exception:
                logger.exception("evidence: verifier failed on %r", ev.get('quote'))
                r = {"status": "unchecked"}
            ev['status'] = r['status']
            if r.get('segment_key_source'):
                ev['segment_key'] = _segment_key_for(
                    r['segment_key_source'], user_folder, date)
                ev['offset_sec'] = r.get('offset_sec')
            if r.get('found_offset_sec') is not None:
                ev['found_offset_sec'] = round(r['found_offset_sec'], 1)
            if r.get('fuzzy_ratio') is not None:
                ev['fuzzy_ratio'] = round(r['fuzzy_ratio'], 3)
            statuses.append(r['status'])
        topic['evidence_status'] = evidence_match.roll_up(statuses)
        counts[topic['evidence_status']] = counts.get(topic['evidence_status'], 0) + 1
    # One line per extraction. found_offset feeds the W calibration; the counts
    # ARE the Phase A deliverable.
    logger.info("evidence: %s", counts)
    return counts
```

Call it in `extract_session` after `parsed` is validated and before the artifact write, guarded by `if EMIT_EVIDENCE:`.

- [ ] **Step 4: Run the tests and commit**

```bash
python -m pytest tests/unit/test_evidence_verification.py -v
python -m pytest tests/unit -q
git add src/lambda_extract_session.py tests/unit/test_evidence_verification.py
git commit -m "feat(evidence): verify every citation and anchor it to audio"
```

---

## Task 5: get it into Aurora

**Files:**
- Create: `src/migrations/00NN_topic_evidence.sql` (next free number)
- Modify: `src/repositories/topics.py`, `src/lambda_item_writer.py`
- Test: `tests/unit/test_topic_evidence_column.py`, `tests/integration/test_topic_evidence_sql.py`

**Interfaces:**
- Consumes: the artifact's per-topic `evidence` + `evidence_status`
- Produces: `topics.evidence jsonb`; `upsert_topic(..., evidence=None)`; `evidence` in `_TOPIC_COLS`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_topic_evidence_column.py
"""The artifact is additive-tolerant; Aurora is not.

upsert_topic has an explicit INSERT column list and _TOPIC_COLS an explicit
SELECT list, so an `evidence` key in the extraction JSON is silently dropped at
the database boundary unless both are changed. The /live-items precedent that
suggests otherwise was about `findings` being attached as a child dict by the
repository -- the generic serializer, not the SQL."""
import pytest

from tests.unit.test_meeting_session_repo import FakeConn

t = pytest.importorskip("repositories.topics", reason="requires psycopg")


def test_upsert_topic_writes_evidence():
    conn = FakeConn(results=[[{"id": "t1"}]])
    t.upsert_topic(conn, "site", "2026-08-07", "Slab",
                   evidence=[{"quote": "x", "status": "verified"}])
    sql = conn.calls[0]["sql"]
    assert "evidence" in sql.split("VALUES")[0], "evidence missing from the INSERT columns"


def test_topic_cols_selects_evidence_back():
    assert "evidence" in t._TOPIC_COLS
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/unit/test_topic_evidence_column.py -v`
Expected: FAIL — `evidence` not in the INSERT.

- [ ] **Step 3: Write the migration**

```sql
-- src/migrations/00NN_topic_evidence.sql
-- The transcript lines a topic was derived from, each with the status of a
-- mechanical check against the transcript and an audio anchor.
--
-- jsonb rather than a child table, matching the `findings` precedent: evidence
-- is read with its topic and never queried independently.
--
-- NULL means the extraction ran before this shipped, or with EMIT_EVIDENCE off.
-- It is NOT the same as "cited nothing" -- that is evidence_status = 'absent'
-- inside the payload.
ALTER TABLE topics ADD COLUMN IF NOT EXISTS evidence jsonb;
```

- [ ] **Step 4: Thread it through**

`repositories/topics.py`: add `evidence` to the INSERT column list, its `%s`, `Jsonb(evidence) if evidence is not None else None` to the params, `evidence=None` to the signature, and `evidence` to `_TOPIC_COLS`.

`lambda_item_writer.py`: pass `evidence=topic.get("evidence")` in the `upsert_topic` call, and carry `evidence_status` inside the payload.

- [ ] **Step 5: Write the integration test**

```python
# tests/integration/test_topic_evidence_sql.py
"""Against a real database. The unit suite drives FakeConn and proves nothing
about the SQL -- jsonb round-tripping included."""
import os
import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")


def test_evidence_round_trips():
    from repositories import topics
    payload = [{"quote": "the slab pour is pushed", "status": "verified",
                "segment_key": "audio_segments/a/b/c.wav", "offset_sec": 4.0}]
    with psycopg.connect(DSN) as conn:
        with conn.transaction() as tx:
            row = topics.upsert_topic(conn, _a_site(conn), "2026-08-07", "Slab",
                                      evidence=payload)
            assert row["evidence"] == payload
            tx.rollback()


def _a_site(conn):
    return conn.execute("SELECT id FROM sites LIMIT 1").fetchone()[0]
```

- [ ] **Step 6: Run and commit**

```bash
python -m pytest tests/unit -q
git add src/migrations/ src/repositories/topics.py src/lambda_item_writer.py \
        tests/unit/test_topic_evidence_column.py tests/integration/test_topic_evidence_sql.py
git commit -m "feat(evidence): evidence reaches Aurora, which needs a column not a hope"
```

---

## Task 6: find the output ceiling before it finds us

**Files:**
- Modify: `src/lambda_extract_session.py`
- Test: `tests/unit/test_output_ceiling.py`

**Interfaces:**
- Consumes: nothing
- Produces: a distinct, logged "output hit the ceiling" condition; a raised Anthropic ceiling.

- [ ] **Step 1: Measure qwen's default, because nobody has**

Prod runs `qwen3.7-max` and `llm_utils` sends **no** `max_tokens` on either qwen branch (`:144-150`, `:158-160`). "Sends no cap" is not "uncapped" — DashScope applies a model default. If that default is in the low thousands, the production path has the same invisible-truncation → retry-storm exposure this task exists to close.

Make one call with a deliberately long expected output and record where it stops. **Do this before the prompt change goes anywhere near prod.** Write the number into the spec.

- [ ] **Step 2: Write the failing test**

```python
# tests/unit/test_output_ceiling.py
"""Output truncation is invisible today: it surfaces as unparseable JSON ->
RuntimeError -> S3-event retry -> the full (paid) LLM call re-runs into the same
wall. That is BUG-43's shape. transcript_stats does NOT cover it -- that records
INPUT truncation only."""
import pytest

ex = pytest.importorskip("lambda_extract_session", reason="requires the lambda deps")


def test_a_truncated_response_is_reported_as_a_ceiling_hit(caplog):
    truncated = '{"topics": [{"title": "Slab", "summary": "the pour wa'
    assert ex.looks_truncated(truncated) is True
    assert ex.looks_truncated('{"topics": []}') is False


def test_the_anthropic_ceiling_scales_past_the_old_8000():
    # claude-sonnet-4-6 supports far more output; :935 was the only consumer of
    # that number, and Timeout 600 / LLM_HTTP_TIMEOUT 540 leave room.
    assert ex.max_tokens_for(n_segments=40) > 8000
```

- [ ] **Step 3: Implement**

```python
def looks_truncated(raw):
    """Did the model stop mid-JSON? Distinguishes a ceiling hit from a model
    returning malformed JSON -- today they are the same log line, and only one
    of them is fixed by raising a limit."""
    if not raw:
        return False
    s = raw.strip()
    return s.startswith('{') and not s.endswith('}')


def max_tokens_for(n_segments):
    return min(4096 + n_segments * 350, 16000)     # was 8000
```

At the parse site:

```python
    parsed = llm_utils.extract_json(raw_response)
    if parsed is None:
        if looks_truncated(raw_response):
            logger.error("%s: output hit the token ceiling (%d chars, max_tokens=%d) "
                         "-- retrying will hit it again",
                         session_base, len(raw_response), max_tokens)
        raise RuntimeError(f"Failed to parse Claude JSON for session {session_base}")
```

- [ ] **Step 4: Run and commit**

```bash
python -m pytest tests/unit -q
git add src/lambda_extract_session.py tests/unit/test_output_ceiling.py
git commit -m "fix(extract): a ceiling hit says so instead of looking like bad JSON"
```

---

## Task 7: run it, calibrate it, and read 30 quotes yourself

No new code. The number is the deliverable, and it is not a number until this is done.

**Files:** none.

- [ ] **Step 1: Deploy to test and confirm the flag**

```bash
export MSYS_NO_PATHCONV=1
aws lambda get-function-configuration --function-name fieldsight-test-extract-session \
  --query "Environment.Variables.EMIT_EVIDENCE" --output text
```

Expected `true` on test, `false` on prod.

- [ ] **Step 2: Record one real session on test and pull the logs**

```bash
aws logs filter-log-events --log-group-name /aws/lambda/fieldsight-test-extract-session \
  --start-time <ms> --filter-pattern '"evidence:"' --region ap-southeast-2
```

- [ ] **Step 3: Calibrate `W` — from the data, and without contaminating it**

Collect `found_offset_sec` for **exact-containment matches of quotes at or above the specificity floor only**. Fuzzy and short-quote matches inside the wide provisional window are exactly the spurious long-range matches a smaller `W` exists to reject; including them drags the tail outward and calibrates against the noise.

Plot it. Honest offsets are bounded by turn length (~60s under whole-chunk) plus the model's timestamp sloppiness; spurious ones spread roughly uniformly. **If two modes separate, put `W` in the valley.** Only if it is genuinely unimodal fall back to a percentile — and then record the expected honest-loss, because a p99 cut permanently reclassifies ~1% of honest matches as `unverified` and that 1% lands in the headline number.

Set `EVIDENCE_WINDOW_SEC`.

- [ ] **Step 4: Calibrate the fuzzy threshold the same way**

Collect `fuzzy_ratio` for quotes that failed exact containment. Put the cut where honest regularisation separates from noise. Shipping the provisional 0.9 while insisting `W` be measured would be the same mistake in a different place.

- [ ] **Step 5: Read 30 unverified quotes**

Take 30 `unverified` quotes and their windowed transcript text and split them by hand into **matcher miss** versus **real invention**.

This is the step that turns "8% unverified" from an uninterpretable figure into "x% matcher, y% fabrication". It is human, not another LLM: the parent investigation established that a text-only judge scores a fluent invention well — which is exactly how 313 fabricated words survived for weeks.

- [ ] **Step 6: Listen to one**

Take one `verified` quote, resolve `segment_key` + `offset_sec` through `/api/org/media/presigned-url`, and play it.

Not decoration — the only test that the chain actually reaches the sound, and the only thing that can catch an ASR fabrication the extraction quoted faithfully.

- [ ] **Step 7: Write the number into the spec**

Record: the per-status counts, `W` and the fuzzy threshold with how they were chosen, the adjudicated matcher/fabrication split, and qwen's measured output ceiling from Task 6.

**Then decide about prod.** A near-zero extraction-layer number is the *expected* result and says nothing about the ASR layer — so it is not a gate on Phase B. What it does settle is whether the extraction layer, never measured until now, is sound.

---

## Self-Review

**Spec coverage.** Citations → Task 3. Write-time verification → Task 4. Anchor resolution incl. the `source_filename` stamp → Tasks 1, 4. Matcher (window, concatenation, CJK, fuzzy, floor, per-quote status, rollup, `unchecked`) → Task 2. `at` date attachment → Task 4. Storage migration → Task 5. Output budget, both branches → Task 6. Metric definition, calibration, adjudication, listening → Task 7. Flag, four changes → Task 3. Evidence stripped below topic level → Task 4.

**Placeholders.** None. The one deliberate blank is the migration number (`00NN`), which the Global Constraints explain: pinning it would collide with a parallel session.

**Type consistency.** `check_quote` returns `segment_key_source` (a transcript filename); Task 4 converts it to `segment_key` (an S3 key) — deliberately different names so the conversion is visible. `status` is per quote; `evidence_status` is per topic. `roll_up` consumes the former and produces the latter.

**Known gap, stated rather than hidden:** `evidence` is excluded from embedding text (spec) but no task changes `chunking.py`. That is because it selects fields explicitly and will not pick up a new key — verify it during Task 5 rather than assuming, since a quote copied into an embedding would double-weight the cited sentence in retrieval.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-08-claim-provenance-phase-a.md`. Two execution options:

**1. Subagent-Driven (recommended)** — a fresh subagent per task, review between tasks.

**2. Inline Execution** — batch execution with checkpoints.

Which approach?
