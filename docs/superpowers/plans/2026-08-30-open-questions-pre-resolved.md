# Open questions, pre-resolved — Implementation Plan (v1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A meeting that leaves a factual question open — *"I think it's 150 but I'll have to check"* — produces a visible, resolved-where-possible open point, instead of nothing.

**Architecture:** Detection lives in `session_brief` because the sentence survives no structured field. Admission is three mechanical gates in a new pure module. Resolution for v1 is a **second model call inside the finalize pass that already builds the brief**, stored in the same object — so there is no new endpoint, no new IAM, no migration, and the resolution inherits the brief's deletion posture exactly.

**Tech Stack:** Python 3.12 Lambda, `pytest`, `llm_utils` (Qwen path), existing `evidence_match`.

**Spec:** `docs/superpowers/specs/2026-08-30-open-questions-pre-resolved-design.md`

## Global Constraints

- **Customer-facing text is English.** `output_language.OUTPUT_LANGUAGE_RULE` is appended to every prompt a customer reads. Quotes stay verbatim in the language spoken.
- **`session_brief.py` must stay pure at import.** `llm_utils` is imported lazily inside the call. A new module it imports must not pull `boto3` or `psycopg`.
- **No new table, no migration, no new IAM, no new endpoint.** v1 is scoped so this holds. If a task needs one, the task has drifted — stop and re-scope.
- **A guard that passes must still log.** "It ran and admitted nothing" and "it never ran" are otherwise the same observation.
- **After each fix, put the defect back and watch the test go red.** A test written against already-correct code has never been shown to fail.
- **TEST only.** `SESSION_BRIEF` is `false` on prod and must stay false until `session_brief/` is registered as a deletion outlet (spec §6). No task here flips it.

### v1 scope, and the spec decisions it resolves

The spec left three decisions open. This plan resolves them and says why:

| spec decision | resolved as | why |
|---|---|---|
| §10.1 which document backs `standard` | **Structure and location only. Never quote a standard, never print a value as fact.** | NZS is paywalled. Structure-plus-location is legally clean, needs no licence, and is most of the value (spec §4). |
| §10.2 does `supply` ship in v1 | **No.** | It is the half that needs an external fetcher, a hostile-input boundary and an egress review. |
| §10.3 TEST first | **Yes.** | prod's flag is off and the deletion gap blocks flipping it. |

**`in_corpus` is also deferred**, and this is a change from the spec's "cheapest v1". Resolving it needs an embedding call plus an invoke of `rag-search`, and `SessionFinalizeFunction` carries **no `lambda:InvokeFunction` at all** — which is new IAM, which v1 forbids. So v1 ships `standard` + `needs_a_person`, and `in_corpus` open points are emitted and displayed **unresolved**, which is honest and is exactly what `needs_a_person` already does.

---

## File Structure

| file | responsibility |
|---|---|
| `src/open_points.py` **(new)** | Pure. The uncertainty-marker gate, the three admission checks, and the resolution prompt builder. No AWS, no network. |
| `src/session_brief.py` **(modify)** | Ask the model for `open_points`; run admission; run resolution; record stats. |
| `src/lambda_org_api.py` **(modify, ~2100)** | Stop whitelisting away fields the writer stores. |
| `tests/unit/test_open_points.py` **(new)** | The gate and the three admission checks. |
| `tests/unit/test_session_brief_open_points.py` **(new)** | The wiring: prompt asks for them, admission runs, stats recorded, failure is non-fatal. |
| `tests/unit/test_org_api_brief_projection.py` **(new)** | The endpoint serves what is stored. |

---

### Task 1: The brief endpoint serves what is stored

Independent of everything else and a real defect today: the stored brief carries `summary` and `open_todos`, and the endpoint serves neither. Fixing it first means Task 4's field is servable the moment it exists, instead of being stored forever and served never.

**Files:**
- Modify: `src/lambda_org_api.py:2100-2107`
- Test: `tests/unit/test_org_api_brief_projection.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `GET /api/org/sessions/{id}/brief` response gains `summary`, `open_todos`, `open_points`.

- [ ] **Step 1: Write the failing test**

```python
"""The brief endpoint returns what the writer stored.

Its docstring says "Returned whole rather than filtered"; the code returned a
five-key projection and dropped `summary` and `open_todos`, both of which are in
the real stored object. A whitelist that silently swallows a new field is how a
feature ships storing something it never serves, with every writer test green.
"""
import json

import pytest

oa = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

STORED = {
    "headline": "Door replacement agreed",
    "sections": [{"title": "Doors", "bullets": []}],
    "entities": [{"name": "Two Specialists"}],
    "tasks": [{"text": "Re-inspect before Tuesday"}],
    "stats": {"unmatched": 2},
    "summary": "Doors on floors 1-3 by Tuesday.",
    "open_todos": ["Re-inspect before Tuesday"],
    "open_points": [{"quote": "I think it's 150", "kind": "standard"}],
}


class _Body:
    def read(self):
        return json.dumps(STORED).encode("utf-8")


def test_every_stored_field_reaches_the_caller(monkeypatch):
    monkeypatch.setattr(oa, "s3", lambda: type("S", (), {
        "get_object": staticmethod(lambda **kw: {"Body": _Body()})})())
    monkeypatch.setattr(oa, "_resolve_org_media_folder",
                        lambda conn, caller, user, what=None: ("Ben_UCPK2", None))

    resp = oa.session_brief_read(None, {"id": "u-1", "company_id": "c-1"},
                                 "sid" + "a" * 32,
                                 {"queryStringParameters": {"date": "2026-08-27"}})
    body = json.loads(resp["body"])

    assert body["status"] == "ready"
    for key in ("headline", "sections", "entities", "tasks", "stats",
                "summary", "open_todos", "open_points"):
        assert key in body, f"the endpoint dropped {key!r}"
    assert body["open_todos"] == ["Re-inspect before Tuesday"]
    assert body["open_points"][0]["kind"] == "standard"


def test_a_brief_without_the_new_fields_still_answers(monkeypatch):
    """Every brief written before today has neither. They must read as empty,
    not as a KeyError inside a background lambda."""
    old = {k: STORED[k] for k in ("headline", "sections", "entities", "tasks", "stats")}
    monkeypatch.setattr(oa, "s3", lambda: type("S", (), {
        "get_object": staticmethod(lambda **kw: {"Body": type(
            "B", (), {"read": staticmethod(lambda: json.dumps(old).encode())})()})})())
    monkeypatch.setattr(oa, "_resolve_org_media_folder",
                        lambda conn, caller, user, what=None: ("Ben_UCPK2", None))

    body = json.loads(oa.session_brief_read(
        None, {"id": "u-1", "company_id": "c-1"}, "sid" + "a" * 32,
        {"queryStringParameters": {"date": "2026-08-27"}})["body"])

    assert body["summary"] == ""
    assert body["open_todos"] == []
    assert body["open_points"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_org_api_brief_projection.py -v`
Expected: FAIL — `the endpoint dropped 'summary'`

- [ ] **Step 3: Write minimal implementation**

In `src/lambda_org_api.py`, replace the return of `session_brief_read`:

```python
    return ok({
        "status": "ready",
        "headline": data.get("headline", ""),
        "sections": data.get("sections", []),
        "entities": data.get("entities", []),
        "tasks": data.get("tasks", []),
        "stats": data.get("stats"),
        # The docstring above says "returned whole rather than filtered" and the
        # code was a five-key whitelist -- `summary` and `open_todos` were in
        # every stored brief and reached no caller. A whitelist here does not
        # protect anything: the object is this company's own brief, the ACL was
        # applied to reach it, and the next field added to the writer would have
        # been swallowed exactly like these two were.
        "summary": data.get("summary", ""),
        "open_todos": data.get("open_todos", []),
        "open_points": data.get("open_points", []),
    })
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_org_api_brief_projection.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Put the defect back**

Delete the `"open_todos"` line, re-run, confirm `the endpoint dropped 'open_todos'`. Restore it.

- [ ] **Step 6: Run the whole suite**

Run: `python -m pytest tests/unit -q`
Expected: no new failures.

- [ ] **Step 7: Commit**

```bash
git add src/lambda_org_api.py tests/unit/test_org_api_brief_projection.py
git commit -m "The brief endpoint returned five of the seven fields it stores"
```

---

### Task 2: The uncertainty gate

**Files:**
- Create: `src/open_points.py`
- Test: `tests/unit/test_open_points.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `open_points.has_uncertainty_marker(text: str) -> bool`

- [ ] **Step 1: Write the failing test**

```python
"""The gate that decides an open point may exist at all.

Rules, not a classifier, and the property that buys is asymmetric: a marker list
that MISSES yields nothing, where a classifier that misfires yields a confident
invention. Everything downstream of this gate may be model-produced; the gate
itself may not be.

An open point needs a marker AND an asserted fact. This function is only the
first half -- hedging with no claim ("I'm not sure, anyway") is filtered later.
"""
import pytest

import open_points as op


@pytest.mark.parametrize("text", [
    "I think this pile is 150 in 3604",
    "I can't remember the exact size",
    "I'll have to check that",
    "not sure whether they still have stock",
    "off the top of my head it was 40 grand",
    "I'll confirm with the engineer",
    "我记得这个柱子是 150",
    "记不清了，回头查一下",
    "不确定还有没有库存",
    "大概是四十万吧",
])
def test_markers_fire(text):
    assert op.has_uncertainty_marker(text) is True


@pytest.mark.parametrize("text", [
    "The pile is 150 in 3604.",
    "Two Specialists will replace the doors by Tuesday.",
    "他们周二来换门。",
    "",
])
def test_a_plain_assertion_is_not_an_open_point(text):
    assert op.has_uncertainty_marker(text) is False


def test_a_non_string_is_not_a_crash():
    """The model produced this field. It may be null, a number, or a list."""
    for junk in (None, 12, [], {}):
        assert op.has_uncertainty_marker(junk) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_open_points.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'open_points'`

(Use a plain `import`, never `pytest.importorskip`, for a module we own: importorskip turns "does not exist yet" into a green skip.)

- [ ] **Step 3: Write minimal implementation**

```python
"""open_points.py — a question the meeting left open, and what may be said about it.

A speaker asserts a fact and marks it uncertain: "I think the pile is 150 in
3604, but I'll have to check." That sentence is not an action item, a finding or
a decision, so the extraction pass -- which keeps about 5% of the transcript's
characters -- has no field for it and drops it. It survives only in the
narrative, which is why this runs inside session_brief.

THE GATE IS RULES AND THE FIELDS ARE NOT, deliberately. A marker list that
misses yields nothing; a classifier that misfires yields a confident invention.
So the marker decides whether an open point may exist, the model fills in what
it is about, and `subject` -- the one string permitted to leave the building
later -- is constrained back to the transcript in code.

PURE: no boto3, no psycopg, no network. session_brief imports this at module
scope and must stay pure at import.
"""
import re

__all__ = ["has_uncertainty_marker", "admit"]

# Deliberately narrow. A marker is a speaker flagging their OWN recall as
# unreliable -- not politeness, not a question to someone else. Widening this
# list is a product decision measured against real briefs, not a tidy-up.
_MARKERS = re.compile(
    r"\bi think\b|\bi believe\b"
    r"|\b(can'?t|cannot|don'?t) remember\b|\bcan'?t recall\b"
    r"|\bnot (quite )?sure\b|\bunsure\b"
    r"|\bi'?ll (have to |need to )?(check|confirm|look (it |that )?up)\b"
    r"|\bfrom memory\b|\boff the top of my head\b"
    r"|\bfrom recollection\b"
    r"|我记得|我觉得|记不清|记不得|不确定|不太确定"
    r"|回头(查|确认)|回去(查|确认)|再确认|查一下"
    r"|大概是|应该是|好像是",
    re.IGNORECASE,
)


def has_uncertainty_marker(text):
    """Does this line carry a speaker flagging their own recall as unreliable?

    Non-strings are False rather than an error: this reads a model-produced
    field, which may be null, a number, or a list on any given run.
    """
    if not isinstance(text, str) or not text.strip():
        return False
    return bool(_MARKERS.search(text))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_open_points.py -v`
Expected: PASS (15 tests)

- [ ] **Step 5: Commit**

```bash
git add src/open_points.py tests/unit/test_open_points.py
git commit -m "The gate that decides an open point may exist"
```

---

### Task 3: Admission — three mechanical checks, and a rejection is counted

**Files:**
- Modify: `src/open_points.py`
- Test: `tests/unit/test_open_points.py`

**Interfaces:**
- Consumes: `has_uncertainty_marker` from Task 2; `evidence_match.check_quote(quote, turns, at, *, w_seconds, floor_tokens, fuzzy_threshold)`.
- Produces: `open_points.admit(candidates: list[dict], turns: list[dict]) -> tuple[list[dict], dict]` — returns `(admitted, stats)` where `stats` is `{"admitted": int, "rejected": {reason: count}}`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_open_points.py

TURNS = [
    {"abs_start_str": "14:09:00", "speaker": "spk_0",
     "text": "I think this pile is 150 in 3604 but I will have to check."},
    {"abs_start_str": "14:09:20", "speaker": "spk_1",
     "text": "The doors go in on Tuesday."},
]


def _candidate(**over):
    c = {"quote": "I think this pile is 150 in 3604 but I will have to check.",
         "at": "14:09:00",
         "claim": "The pile size in NZS 3604 is 150mm",
         "kind": "standard",
         "subject": "3604"}
    c.update(over)
    return c


def test_a_good_candidate_is_admitted():
    admitted, stats = op.admit([_candidate()], TURNS)
    assert len(admitted) == 1
    assert stats["admitted"] == 1
    assert admitted[0]["subject"] == "3604"


def test_no_marker_in_the_quote_is_rejected():
    """The gate is on the QUOTE, not on the model's paraphrase. A model that
    decides a plain statement was uncertain does not get to say so."""
    admitted, stats = op.admit(
        [_candidate(quote="The doors go in on Tuesday.", at="14:09:20")], TURNS)
    assert admitted == []
    assert stats["rejected"]["no_marker"] == 1


def test_a_subject_the_speaker_never_said_is_rejected():
    """`subject` is the ONLY string permitted to leave the building later. If a
    model may compose it, a free composition is a free exfiltration -- so it
    must appear verbatim inside the quote it came from."""
    admitted, stats = op.admit([_candidate(subject="Ellesmere College budget")], TURNS)
    assert admitted == []
    assert stats["rejected"]["subject_not_in_quote"] == 1


def test_a_quote_that_is_not_in_the_transcript_is_rejected():
    """Nothing in session_brief verifies a quote today -- `_snap_to_quote` only
    re-anchors a timestamp and leaves unmatched quotes in place. An open point
    whose quote was never said is an invented uncertainty."""
    admitted, stats = op.admit(
        [_candidate(quote="I think the roof is 200 but I will have to check.")], TURNS)
    assert admitted == []
    assert stats["rejected"]["quote_unverified"] == 1


def test_an_unparseable_timestamp_is_a_rejection_not_a_crash():
    admitted, stats = op.admit([_candidate(at="later on")], TURNS)
    assert admitted == []
    assert stats["rejected"]["bad_anchor"] == 1


def test_junk_from_the_model_is_survivable():
    admitted, stats = op.admit([None, 7, {}, _candidate()], TURNS)
    assert len(admitted) == 1
    assert stats["rejected"]["malformed"] == 3


def test_stats_are_returned_even_when_everything_passed():
    """'It ran and rejected nothing' and 'it never ran' are otherwise the same
    observation. The caller logs this; it must always have something to log."""
    _, stats = op.admit([_candidate()], TURNS)
    assert stats == {"admitted": 1, "rejected": {}}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_open_points.py -v`
Expected: FAIL — `AttributeError: module 'open_points' has no attribute 'admit'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/open_points.py`:

```python
# Same window/floor/threshold the extraction path verifies its citations with
# (lambda_extract_session.py:380-382). Matching them is deliberate: two
# verifiers with different tolerances would call the same quote verified on one
# path and unverified on the other.
_W_SECONDS = 60.0
_FLOOR_TOKENS = 5
_FUZZY = 0.80

_ACCEPTED_STATUSES = ("verified", "verified_fuzzy", "weak")
_KINDS = ("standard", "supply", "in_corpus", "needs_a_person")


def _parse_at(raw):
    """HH:MM:SS -> seconds, or None. The model writes this field."""
    if not isinstance(raw, str):
        return None
    m = re.match(r"^\s*(\d{1,2}):(\d{2}):(\d{2})\s*$", raw)
    if not m:
        return None
    h, mi, s = (int(g) for g in m.groups())
    return h * 3600 + mi * 60 + s


def admit(candidates, turns, *, check=None):
    """Filter model-produced open points down to the ones that survive.

    Returns (admitted, stats). Rejections are COUNTED by reason and never
    raised: this runs inside the brief, and a brief that fails because one
    candidate was malformed is worse than a brief with one fewer open point.

    `check` is injectable so the tests do not need the verifier's tuning; the
    default is evidence_match.check_quote, imported lazily to keep this module
    pure at import.
    """
    if check is None:
        import evidence_match

        def check(quote, at):
            return evidence_match.check_quote(
                quote, turns, at, w_seconds=_W_SECONDS,
                floor_tokens=_FLOOR_TOKENS, fuzzy_threshold=_FUZZY)

    admitted, rejected = [], {}

    def reject(reason):
        rejected[reason] = rejected.get(reason, 0) + 1

    for c in candidates or []:
        if not isinstance(c, dict):
            reject("malformed")
            continue
        quote = c.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            reject("malformed")
            continue

        # 1. The gate, on the QUOTE. A model deciding that a plain statement was
        #    uncertain does not get to say so.
        if not has_uncertainty_marker(quote):
            reject("no_marker")
            continue

        # 2. `subject` is the only string permitted to leave the building later,
        #    so it must be something the speaker actually said. Case-folded
        #    because the model corrects capitalisation and that is not
        #    composition; whitespace-collapsed for the same reason.
        subject = c.get("subject")
        if not isinstance(subject, str) or not subject.strip():
            reject("malformed")
            continue
        if _norm(subject) not in _norm(quote):
            reject("subject_not_in_quote")
            continue

        at = _parse_at(c.get("at"))
        if at is None:
            reject("bad_anchor")
            continue

        # 3. The quote was actually said. Nothing on the brief path checks this
        #    today -- _snap_to_quote re-anchors a timestamp and leaves unmatched
        #    quotes in the brief -- so an open point built on an invented
        #    sentence would be indistinguishable from a real one.
        try:
            status = (check(quote, at) or {}).get("status")
        except Exception:
            reject("verifier_error")
            continue
        if status not in _ACCEPTED_STATUSES:
            reject("quote_unverified")
            continue

        kind = c.get("kind")
        admitted.append({
            "quote": quote,
            "at": c.get("at"),
            "claim": c.get("claim") if isinstance(c.get("claim"), str) else "",
            "kind": kind if kind in _KINDS else "needs_a_person",
            "subject": subject,
        })

    return admitted, {"admitted": len(admitted), "rejected": rejected}


def _norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip().lower()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_open_points.py -v`
Expected: PASS (22 tests)

- [ ] **Step 5: Put each guard back, one at a time**

For each of the three checks, comment it out, run, confirm exactly the matching test goes red, restore it. A guard nothing exercises is the shape this repo keeps shipping.

- [ ] **Step 6: Commit**

```bash
git add src/open_points.py tests/unit/test_open_points.py
git commit -m "Three checks an open point has to survive, and a count of what did not"
```

---

### Task 4: The brief asks for them, admits them, and says how many it dropped

**Files:**
- Modify: `src/session_brief.py` (prompt schema block ~line 60-90; `brief_from_turns` ~line 334)
- Test: `tests/unit/test_session_brief_open_points.py`

**Interfaces:**
- Consumes: `open_points.admit` from Task 3.
- Produces: the stored brief gains `open_points: list[dict]`, and `stats` gains `open_points_admitted` / `open_points_rejected`.

- [ ] **Step 1: Write the failing test**

```python
"""session_brief emits open points, and drops the ones that fail admission.

The wiring, not the rules -- the rules are pinned in test_open_points.py. What
is asserted here is that the prompt asks for the field, that admission actually
runs on the answer, that the counts are recorded, and that a failure anywhere in
this block cannot cost the caller their brief.
"""
import json

import pytest

import session_brief as sb

TURNS = [
    {"abs_start_str": "14:09:00", "speaker": "spk_0",
     "text": "I think this pile is 150 in 3604 but I will have to check."},
]

GOOD = {"quote": "I think this pile is 150 in 3604 but I will have to check.",
        "at": "14:09:00", "claim": "Pile size in NZS 3604", "kind": "standard",
        "subject": "3604"}
INVENTED = {"quote": "I think the roof is 200 but I will have to check.",
            "at": "14:09:00", "claim": "Roof", "kind": "standard", "subject": "roof"}


def _reply(open_points):
    return json.dumps({
        "headline": "Piles", "sections": [], "entities": [], "tasks": [],
        "open_points": open_points,
    })


def test_the_prompt_asks_for_open_points():
    prompt = sb.build_brief_prompt(TURNS)
    assert "open_points" in prompt
    assert "uncertain" in prompt.lower()


def test_an_admitted_point_reaches_the_brief():
    brief = sb.brief_from_turns(
        TURNS, call_llm=lambda *a, **k: (_reply([GOOD]), None))
    assert len(brief["open_points"]) == 1
    assert brief["open_points"][0]["subject"] == "3604"
    assert brief["stats"]["open_points_admitted"] == 1


def test_an_invented_quote_does_not_reach_the_brief():
    brief = sb.brief_from_turns(
        TURNS, call_llm=lambda *a, **k: (_reply([INVENTED]), None))
    assert brief["open_points"] == []
    assert brief["stats"]["open_points_rejected"]["quote_unverified"] == 1


def test_a_brief_with_no_open_points_still_carries_the_field_and_the_counts():
    """`.get("open_points")` returning None must never be how a reader learns a
    meeting had none -- that reads as "this build has no open points"."""
    brief = sb.brief_from_turns(TURNS, call_llm=lambda *a, **k: (_reply([]), None))
    assert brief["open_points"] == []
    assert brief["stats"]["open_points_admitted"] == 0
    assert brief["stats"]["open_points_rejected"] == {}


def test_a_model_that_omits_the_key_entirely_is_survivable():
    reply = json.dumps({"headline": "x", "sections": [], "entities": [], "tasks": []})
    brief = sb.brief_from_turns(TURNS, call_llm=lambda *a, **k: (reply, None))
    assert brief["open_points"] == []


def test_admission_blowing_up_costs_the_points_not_the_brief(monkeypatch):
    """The brief is the artifact the email and the website stand on. Nothing in
    this block may be able to take it down -- same posture as _store_brief."""
    import open_points

    def boom(*a, **k):
        raise RuntimeError("verifier exploded")

    monkeypatch.setattr(open_points, "admit", boom)
    brief = sb.brief_from_turns(TURNS, call_llm=lambda *a, **k: (_reply([GOOD]), None))
    assert brief is not None
    assert brief["headline"] == "Piles"
    assert brief["open_points"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_session_brief_open_points.py -v`
Expected: FAIL — `'open_points' in prompt` is False.

- [ ] **Step 3: Add the schema block to the prompt**

In `src/session_brief.py`, inside the JSON schema in `build_brief_prompt`, after the `"tasks"` array and before the closing brace:

```python
  "open_points": [
    {{
      "quote": "The line, copied VERBATIM from the transcript, in which the speaker states something AND flags that they are not sure of it. Do not rewrite it and do not merge two lines.",
      "at": "HH:MM:SS",
      "claim": "The fact they stated, in one short sentence.",
      "kind": "standard if it is settled by a code, standard or specification | supply if it is about a supplier's stock, price or lead time | in_corpus if an earlier meeting would settle it | needs_a_person if only a named person can answer",
      "subject": "The single term a lookup would need, copied EXACTLY as it appears in the quote above -- a standard number, a product, a company. Not a sentence. If no such term appears in the quote, omit this whole entry."
    }}
  ]
```

And in the numbered guidance below the schema, add:

```
6. **open_points: both halves are required.** A speaker must STATE something and
   FLAG that they are unsure of it -- "I think it's 150, I'll have to check".
   Hedging with no claim ("not sure, anyway") is not one, and neither is a
   question asked of someone else. If nobody left anything hanging, return an
   empty array; a meeting with no open points is the normal case.
```

- [ ] **Step 4: Wire admission into `brief_from_turns`**

First, at the TOP of `src/session_brief.py`, beside the existing
`from output_language import OUTPUT_LANGUAGE_RULE`:

```python
import open_points
```

Module scope, not inside the function: `open_points` is pure (no boto3, no
psycopg, no network), so it cannot break this module's "pure at import"
property, and a lazy import inside a `try` would leave the name unbound for the
resolution block in Task 5 — a `NameError` on the one path that only runs when
something has already gone wrong.

Then, immediately after the `brief["stats"] = ...` line:

```python
    # Open points, admitted or counted. Wrapped because this whole block is an
    # enrichment: the brief is what the confirmation email and the website stand
    # on, and losing it to a verifier bug would be a far worse trade than losing
    # every open point in the session. Same posture as _store_brief.
    try:
        admitted, op_stats = open_points.admit(brief.get("open_points"), turns)
    except Exception:
        logger.exception("session_brief: open-point admission failed -- "
                         "the brief is unaffected")
        admitted, op_stats = [], {"admitted": 0, "rejected": {"admission_error": 1}}
    brief["open_points"] = admitted
    brief["stats"]["open_points_admitted"] = op_stats["admitted"]
    brief["stats"]["open_points_rejected"] = op_stats["rejected"]
    # Logged even at zero: "it ran and admitted nothing" and "it never ran" are
    # the same line otherwise, and that is how a whole feature stays broken.
    logger.info("session_brief: %d open point(s) admitted, %d rejected (%s)",
                op_stats["admitted"], sum(op_stats["rejected"].values()),
                op_stats["rejected"] or "none")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_session_brief_open_points.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Put the defect back**

Remove the `try/except` around admission and make `open_points.admit` raise; confirm `test_admission_blowing_up_costs_the_points_not_the_brief` goes red. Restore.

- [ ] **Step 7: Run the whole suite**

Run: `python -m pytest tests/unit -q`
Expected: no new failures. The existing `tests/unit/test_session_brief*.py` files must stay green — if one asserts an exact `stats` dict, widen it to the keys it is testing rather than deleting the assertion.

- [ ] **Step 8: Commit**

```bash
git add src/session_brief.py tests/unit/test_session_brief_open_points.py
git commit -m "The brief keeps the question the meeting left hanging"
```

---

### Task 5: Resolve the `standard` ones — structure and location, never a value

**Files:**
- Modify: `src/open_points.py` (prompt builder), `src/session_brief.py` (one more call)
- Test: `tests/unit/test_open_points.py`, `tests/unit/test_session_brief_open_points.py`

**Interfaces:**
- Consumes: `admit` from Task 3.
- Produces: `open_points.build_resolution_prompt(points: list[dict]) -> str | None`, and `open_points.attach_resolutions(points, call_llm) -> dict` (stats). Admitted points gain `resolution: {"cases": [str], "where": str} | None`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_open_points.py

def test_only_standard_points_go_to_the_model():
    """A `supply` point needs a live source and a `needs_a_person` one needs a
    person. Sending either to a model produces a fabricated answer, because
    neither fact is in its weights."""
    pts = [{"kind": "standard", "claim": "Pile size in 3604", "subject": "3604"},
           {"kind": "supply", "claim": "CZ LiDAR stock", "subject": "CZ LiDAR"},
           {"kind": "needs_a_person", "claim": "Engineer sign-off", "subject": "engineer"}]
    prompt = op.build_resolution_prompt(pts)
    assert "3604" in prompt
    assert "CZ LiDAR" not in prompt
    assert "engineer" not in prompt


def test_no_standard_points_means_no_model_call():
    assert op.build_resolution_prompt(
        [{"kind": "supply", "claim": "x", "subject": "y"}]) is None


def test_the_prompt_forbids_stating_a_value_as_fact():
    prompt = op.build_resolution_prompt(
        [{"kind": "standard", "claim": "Pile size in 3604", "subject": "3604"}])
    low = prompt.lower()
    assert "do not state" in low or "never state" in low
    assert "cases" in low
    assert "chapter" in low or "section" in low


def test_a_resolution_is_attached_to_the_right_point():
    pts = [{"kind": "supply", "claim": "stock", "subject": "CZ"},
           {"kind": "standard", "claim": "Pile size in 3604", "subject": "3604"}]
    reply = '{"resolutions": [{"index": 1, "cases": ["by load", "by ground"], ' \
            '"where": "NZS 3604, section on foundations"}]}'
    stats = op.attach_resolutions(pts, lambda *a, **k: (reply, None))
    assert pts[1]["resolution"]["where"].startswith("NZS 3604")
    assert pts[1]["resolution"]["cases"] == ["by load", "by ground"]
    assert pts[0]["resolution"] is None          # supply is left unresolved, visibly
    assert stats["resolved"] == 1


def test_an_index_the_model_invented_is_dropped():
    pts = [{"kind": "standard", "claim": "c", "subject": "3604"}]
    reply = '{"resolutions": [{"index": 7, "cases": ["x"], "where": "y"}]}'
    stats = op.attach_resolutions(pts, lambda *a, **k: (reply, None))
    assert pts[0]["resolution"] is None
    assert stats["dropped_bad_index"] == 1


def test_an_llm_failure_leaves_every_point_unresolved_and_says_so():
    pts = [{"kind": "standard", "claim": "c", "subject": "3604"}]
    stats = op.attach_resolutions(pts, lambda *a, **k: (None, "timeout"))
    assert pts[0]["resolution"] is None
    assert stats["error"] == "timeout"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_open_points.py -v`
Expected: FAIL — `module 'open_points' has no attribute 'build_resolution_prompt'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/open_points.py`:

```python
import json

from output_language import OUTPUT_LANGUAGE_RULE

_RESOLUTION_RULES = """You are answering questions a construction meeting left open about a
STANDARD or specification. For each numbered item below, say what the answer DEPENDS ON and
where to look it up.

Rules:
- Give the CASES the value varies by -- load, ground condition, height, span, whatever the
  standard divides on. That is the answer a reader can apply themselves.
- Say WHERE in the standard it is settled: the part, chapter or section. A range is fine.
- NEVER state a specific value, dimension or clause number as fact. You do not have the
  document in front of you, and a number that turns out to be wrong goes into a variation
  and then into a dispute. A reader who knows which cases exist and where to look has what
  they need; a reader given a wrong number has worse than nothing.
- If you do not know which standard or which part, say so by returning no entry for that
  item. An omission is a correct answer here.
- Do not quote the standard. Do not reproduce a table."""


def build_resolution_prompt(points):
    """The prompt for the `standard` points, or None when there are none.

    ONLY `standard` points are sent. A `supply` point is about stock right now
    and a `needs_a_person` one is about somebody's judgement -- neither fact
    exists in a model's weights, so asking produces a fabrication with the same
    confidence as a real answer. They are displayed unresolved instead, which is
    the honest state and is visible to the reader.
    """
    items = [(i, p) for i, p in enumerate(points or [])
             if isinstance(p, dict) and p.get("kind") == "standard"]
    if not items:
        return None
    listed = "\n".join(
        f'{i}. subject: {p.get("subject", "")} | what was said: {p.get("claim", "")}'
        for i, p in items)
    return (
        _RESOLUTION_RULES + OUTPUT_LANGUAGE_RULE
        + "\n\n## Items\n" + listed
        + '\n\n## Reply\nJSON only: {"resolutions": [{"index": <the number above>, '
          '"cases": ["short phrase per case"], "where": "part/chapter/section"}]}. '
          'Omit any item you cannot answer.'
    )


def attach_resolutions(points, call_llm):
    """Fill `resolution` on the `standard` points. Returns a stats dict.

    Every point gets the key, resolved or not: a reader distinguishing "we could
    not answer this" from "this build has no resolutions" needs the key present
    and null, not absent.
    """
    for p in points or []:
        if isinstance(p, dict):
            p.setdefault("resolution", None)

    prompt = build_resolution_prompt(points)
    if prompt is None:
        return {"resolved": 0, "dropped_bad_index": 0, "error": None}

    raw, err = call_llm(prompt, max_tokens=2000, force_json=True)
    if err or not raw:
        return {"resolved": 0, "dropped_bad_index": 0, "error": err or "empty reply"}

    try:
        parsed = json.loads(raw)
        entries = parsed.get("resolutions") or []
    except Exception:
        return {"resolved": 0, "dropped_bad_index": 0, "error": "unparseable reply"}

    resolved = bad = 0
    for e in entries:
        if not isinstance(e, dict):
            bad += 1
            continue
        i = e.get("index")
        # An index the model invented would attach a pile answer to a door
        # question, which is worse than no answer and looks exactly like one.
        if not isinstance(i, int) or not (0 <= i < len(points)):
            bad += 1
            continue
        target = points[i]
        if not isinstance(target, dict) or target.get("kind") != "standard":
            bad += 1
            continue
        cases = [c for c in (e.get("cases") or []) if isinstance(c, str) and c.strip()]
        where = e.get("where") if isinstance(e.get("where"), str) else ""
        if not cases and not where:
            bad += 1
            continue
        target["resolution"] = {"cases": cases, "where": where}
        resolved += 1

    return {"resolved": resolved, "dropped_bad_index": bad, "error": None}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_open_points.py -v`
Expected: PASS (28 tests)

- [ ] **Step 5: Wire it into the brief**

In `src/session_brief.py`, immediately after the admission block from Task 4:

```python
    # Resolve the standard ones with a SECOND model call, here rather than at
    # read time. finalize is non-VPC and already holds the LLM env, so this
    # needs no new endpoint and no new IAM -- and the result lands inside the
    # brief object, which means it inherits the brief's deletion posture instead
    # of becoming a second frozen copy somewhere else.
    #
    # It is a CACHE, not a record: regenerable, never authoritative, and no row
    # anywhere references it (spec section 8).
    try:
        res_stats = open_points.attach_resolutions(admitted, call_llm)
    except Exception:
        logger.exception("session_brief: open-point resolution failed -- "
                         "the points are kept, unresolved")
        res_stats = {"resolved": 0, "dropped_bad_index": 0, "error": "exception"}
    brief["stats"]["open_points_resolved"] = res_stats["resolved"]
    logger.info("session_brief: %d open point(s) resolved, %d dropped on a bad index%s",
                res_stats["resolved"], res_stats["dropped_bad_index"],
                f", error={res_stats['error']}" if res_stats["error"] else "")
```

Add to `tests/unit/test_session_brief_open_points.py`:

```python
def test_the_resolution_call_is_a_second_call_and_its_failure_is_not_fatal():
    """Two calls, and only the first one may take the brief down."""
    calls = []

    def call_llm(prompt, **kw):
        calls.append(prompt)
        if len(calls) == 1:
            return (_reply([GOOD]), None)
        return (None, "resolver timed out")

    brief = sb.brief_from_turns(TURNS, call_llm=call_llm)

    assert len(calls) == 2
    assert brief["open_points"][0]["resolution"] is None
    assert brief["stats"]["open_points_resolved"] == 0
```

- [ ] **Step 6: Run both test files**

Run: `python -m pytest tests/unit/test_open_points.py tests/unit/test_session_brief_open_points.py -v`
Expected: PASS

- [ ] **Step 7: Run the whole suite**

Run: `python -m pytest tests/unit -q`
Expected: no new failures.

- [ ] **Step 8: Commit**

```bash
git add src/open_points.py src/session_brief.py tests/unit/test_open_points.py tests/unit/test_session_brief_open_points.py
git commit -m "Answer the standard questions with the cases and the chapter, never the number"
```

---

### Task 6: End to end on TEST, then break it on purpose

**Files:**
- Create: `scripts/measure_open_points.py`
- No source changes.

**Interfaces:**
- Consumes: everything above.
- Produces: a repeatable check, not a claim.

- [ ] **Step 1: Deploy**

Merge to `develop`; `deploy.yml` puts it on TEST. Confirm the run succeeded AND that the deployed package actually carries the new module — a green deploy is not evidence:

```bash
aws lambda get-function --function-name fieldsight-test-session-finalize \
  --region ap-southeast-2 --query 'Code.Location' --output text
# download, then: python -c "import zipfile;print('open_points.py' in zipfile.ZipFile('f.zip').namelist())"
```

- [ ] **Step 2: Run one real session through it**

There are two briefs on TEST (`Ben_UCPK2/2026-08-27`, `Ben_UCPK2/2026-08-29`) and prod has none. Re-run finalize for one of those sessions and read the stored object:

```bash
aws s3 cp s3://fieldsight-data-test-509194952652/session_brief/Ben_UCPK2/2026-08-29/sidbea0e96072704bc8aaa2b2ee1c1e9166/latest.json - \
  | python -c "import json,sys; b=json.load(sys.stdin); print(json.dumps({'stats':b.get('stats'),'open_points':b.get('open_points')}, indent=2, ensure_ascii=False))"
```

Expected: `stats` carries `open_points_admitted`, `open_points_rejected`, `open_points_resolved`, **whatever their values**. Zero is a valid result on a session that left nothing hanging; a MISSING key means the block did not run.

- [ ] **Step 3: Confirm the endpoint serves them**

```bash
curl -s -X GET "https://wdsgobb7b0.execute-api.ap-southeast-2.amazonaws.com/prod/api/org/sessions/sidbea0e96072704bc8aaa2b2ee1c1e9166/brief?date=2026-08-29&user=Ben_UCPK2" \
  -H "Authorization: <id token>" | python -m json.tool | head -40
```

Expected: `open_points` present (Task 1). Without a token this returns 401, which confirms the route but not the field.

- [ ] **Step 4: Break it on purpose**

Locally revert the `subject_not_in_quote` check and re-run `tests/unit/test_open_points.py` — confirm red. Revert the endpoint's `open_points` line and re-run `tests/unit/test_org_api_brief_projection.py` — confirm red. Restore both.

- [ ] **Step 5: Write the measurement script**

```python
#!/usr/bin/env python3
"""How many open points does a real session produce, and how many survive?

Not a pass/fail check. There are two briefs in existence, so any number this
prints is a description of two sessions -- the n=1 trap barely widened. It
exists so the number is re-derivable rather than remembered, and so the
admission rate can be watched as briefs accumulate.

    AWS_PROFILE=fieldsight-deployer python scripts/measure_open_points.py
"""
import json
import sys

BUCKET = "fieldsight-data-test-509194952652"
PREFIX = "session_brief/"


def main():
    import boto3
    s3 = boto3.client("s3", region_name="ap-southeast-2")
    keys = [o["Key"] for page in s3.get_paginator("list_objects_v2")
            .paginate(Bucket=BUCKET, Prefix=PREFIX)
            for o in page.get("Contents", [])]
    if not keys:
        print("no briefs -- nothing to measure")
        return 2

    total_admitted = total_rejected = total_resolved = 0
    for k in keys:
        b = json.loads(s3.get_object(Bucket=BUCKET, Key=k)["Body"].read())
        st = b.get("stats") or {}
        if "open_points_admitted" not in st:
            print(f"  {k}: written before open points existed -- skipped")
            continue
        rej = st.get("open_points_rejected") or {}
        total_admitted += st["open_points_admitted"]
        total_rejected += sum(rej.values())
        total_resolved += st.get("open_points_resolved", 0)
        print(f"  {k.split('/')[2]}: admitted={st['open_points_admitted']} "
              f"resolved={st.get('open_points_resolved', 0)} rejected={rej or '{}'}")
        for p in b.get("open_points") or []:
            mark = "resolved" if p.get("resolution") else "open"
            print(f"      [{p.get('kind')}/{mark}] {p.get('claim', '')[:80]}")

    print(f"\n{len(keys)} brief(s): {total_admitted} admitted, "
          f"{total_resolved} resolved, {total_rejected} rejected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Commit**

```bash
git add scripts/measure_open_points.py
git commit -m "Count the open points two real sessions actually produce"
```

---

## Out of scope, and why — read before adding a task

- **`supply` resolution.** Needs an external fetcher, a hostile-input boundary and an egress review (spec §6). Its own plan.
- **`in_corpus` resolution.** Needs an embedding call plus a `rag-search` invoke, and `SessionFinalizeFunction` has no `lambda:InvokeFunction` — new IAM, which v1 forbids. These points are emitted and displayed unresolved.
- **Registering `session_brief/` as a deletion outlet.** A pre-existing gap (spec §6), not this feature's, and a hard prerequisite for `SESSION_BRIEF` on prod. Its own change.
- **Merged multi-device meetings.** `process_finalize_request` skips re-derivation for `kind == "updated"`, so they produce no brief and no open points (spec §8b).
- **The UI.** The API gains a field; presenting it is a separate spec.
- **Flipping `SESSION_BRIEF` on prod.** Blocked by the deletion outlet above.
