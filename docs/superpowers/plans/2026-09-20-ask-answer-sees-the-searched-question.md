# The Answering Prompt Sees What It Searched For — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When Ask's rewrite step turns a follow-up ("why do we talk it? who
requested?") into a standalone question ("why do we talk about PS4? who
requested?"), the **answering** prompt — not only the retrieval embed and the
web-answer verdict — must be built from that standalone text. Today
`build_rag_prompt` is called with the caller's literal `question` even when a
rewrite ran, so the model answering the question has no antecedent for the
pronoun the rewrite already resolved, and truthfully reports it cannot
identify what "it" refers to.

**Architecture:** `_rag_answer` in `src/lambda_ask_agent.py` already computes
two names for "the question": `question` (caller's literal text, always) and
`asked` (the rewrite's output; equals `question` when no rewrite ran or the
feature is off). A boolean, `rewritten`, records whether a rewrite actually
happened (set at `:1251` as `asked, rewritten = question, False`, then
reassigned at `:1300-1303` only inside the `ASK_CONVERSATION_MEMORY` branch).
`asked` already reaches three destinations: the retrieval embed (`:1400`),
the web-answer verdict (`verdict_question=asked` at `:1590-1591`), and the
response body / UI "Searched for:" line (`asked if rewritten else None`,
e.g. `:1600`, `:1690`). This plan adds the fourth and last: `build_rag_prompt`
at both its call sites (`:1608` primary, `:1672` language-leak retry) receives
`asked if rewritten else question` instead of `question`. No new parameter is
added to `build_rag_prompt`; no history reaches it, before or after. The
web-answer *lookup* and *admission screen* (`src/web_answer.py:answer()`,
called with `question` at `:1501` and `:1590`) are untouched — the spec
explicitly does not extend this change to the external-search boundary.

**Tech Stack:** Python 3.12 Lambda (`src/lambda_ask_agent.py`), pytest with
monkeypatch-based fakes (`tests/unit/test_lambda_ask_agent_rag.py`), same
harness as the rest of this repo's unit suite.

**Spec:** `docs/superpowers/specs/2026-09-20-ask-answer-sees-the-searched-question.md`
(amends `docs/superpowers/specs/2026-09-17-ask-conversation-memory-design.md`
§2 and §3.3; does not reopen §1 decisions 2-5, §4.4's distance gate, or
`history`'s exclusion from the answering prompt).

## Global Constraints

- **History never enters the answering prompt.** Zero, before and after this
  change — this is the parent spec's §2 protection and this amendment leaves
  it standing. Every new/changed test must keep asserting this explicitly
  (`"history" not in kwargs`, and the prior turn's answer text not present in
  the prompt args), not just stop asserting the old behaviour.
- **No new request field, no new response field.** `asked` already exists in
  the response body (e.g. `:1600`); nothing is added to the wire contract.
- **Retrieval, ACL and tombstone behaviour are unchanged.** `dashscope_utils.embed([asked])`
  at `:1400` is untouched; the change is confined to what `build_rag_prompt`
  is called with.
- **`web_answer.answer()`'s arguments are unchanged.** `question` still goes
  to the lookup and to `question_admission.screen`; `verdict_question=asked`
  still goes only to the verdict, exactly as PR #876 shipped it. This spec
  does not touch `src/web_answer.py` or its tests
  (`tests/unit/test_answering_from_the_open_web.py`) at all.
- **The conditional is exact:** `asked if rewritten else question` — use the
  variable named `rewritten` (set at `src/lambda_ask_agent.py:1251` and
  `:1300`), not a re-derived `asked != question` check, since `asked` and
  `question` can coincide even when a rewrite ran (a no-op rewrite).
- **Both `build_rag_prompt` call sites** (`:1608` main, `:1672` language-leak
  retry) must change together, or the 1-in-13 retry path silently regresses
  to pronoun-blind answering.
- Development artefacts (code comments, commit messages, docs) in English.
- Commit messages end with:
  ```
  Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
  ```
- Windows repo: never `git add -A`; add files by path.
- Test harness (run from the worktree root
  `C:/Users/camil/fswork/specs-2026-09-20`, Git Bash):
  ```bash
  export UV_LINK_MODE=copy AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=ap-southeast-2
  uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest <paths> -q
  ```

## File Structure

- Modify `src/lambda_ask_agent.py` — the two `build_rag_prompt(question, ...)`
  call sites at `:1608` and `:1672` become `build_rag_prompt(asked if rewritten else question, ...)`.
- Modify `tests/unit/test_lambda_ask_agent_rag.py` — replace
  `test_the_answering_prompt_gets_the_original_and_no_history` (`:534-556`)
  with two tests (one per branch of the spec's §3), and add a retry-path
  assertion beside the existing `insist_language=True` test.
- No other file changes. `src/web_answer.py` and
  `tests/unit/test_answering_from_the_open_web.py` are read-only references,
  not modified (spec §3.3).

---

### Task 1: Replace the test that pins the old (reversed) decision

**Files:**
- Modify: `tests/unit/test_lambda_ask_agent_rag.py`

**Interfaces:**
- Consumes: `laa._rag_answer(body: dict) -> dict` (existing), `laa.build_rag_prompt` (existing, spied via monkeypatch as in the test being replaced), `ask_rewrite.standalone_question` (existing, stubbed).
- Produces: no new names; two new test functions in place of one.

- [ ] **Step 1: Read the exact test being replaced, to anchor the diff**

Confirmed at `tests/unit/test_lambda_ask_agent_rag.py:534-556`:

```python
def test_the_answering_prompt_gets_the_original_and_no_history(monkeypatch):
    """SS2's whole claim. Assert the ABSENCE explicitly: no history text reaches
    build_rag_prompt, and it is called with the caller's original question."""
    monkeypatch.setenv("ASK_CONVERSATION_MEMORY", "true")  # Task 10: gated
    wire(monkeypatch, chunks=[{"chunk_text": "level 3 grid note", "id": "c-1",
                               "topic_id": "t-1", "source_s3_key": "x",
                               "report_date": "2026-09-17"}])
    monkeypatch.setattr(ask_rewrite, "standalone_question",
                        lambda q, h, **kw: ("rewritten", True))

    prompts = []
    real_build = laa.build_rag_prompt
    def spy_build(question, chunks, **kw):
        prompts.append((question, kw))
        return real_build(question, chunks, **kw)
    monkeypatch.setattr(laa, "build_rag_prompt", spy_build)

    laa._rag_answer({"question": "when is he finishing it?", "caller_sub": SUB,
                     "history": [{"question": "ceiling grid?",
                                  "answer": "level 3 is behind"}]})

    asked_with, kwargs = prompts[0]
    assert asked_with == "when is he finishing it?"
    assert "history" not in kwargs
    assert "level 3 is behind" not in str(kwargs)
```

This test's docstring calls itself "SS2's whole claim" and its assertion
`asked_with == "when is he finishing it?"` (the pronoun form, unchanged by
the rewrite) is exactly the decision this spec reverses. It must not simply
be deleted — the spec (§4) requires it be replaced by two tests, one per
branch of §3, both keeping the negative history assertions.

- [ ] **Step 2: Write the failing (new) tests**

In `tests/unit/test_lambda_ask_agent_rag.py`, replace the entire function
body quoted in Step 1 (same file, same location, between
`test_the_rewritten_text_is_what_gets_embedded` above it and
`test_a_body_without_history_sends_the_payload_it_sends_today` below it)
with:

```python
def test_the_answering_prompt_gets_asked_when_rewritten_and_no_history(monkeypatch):
    """Spec 2026-09-20 SS3.2: when a rewrite ran, the answering prompt must be
    built from `asked` -- the standalone text retrieval already searched with
    -- not the caller's literal pronoun-bearing text. Still assert the
    ABSENCE explicitly: no history text reaches build_rag_prompt."""
    monkeypatch.setenv("ASK_CONVERSATION_MEMORY", "true")  # Task 10: gated
    wire(monkeypatch, chunks=[{"chunk_text": "level 3 grid note", "id": "c-1",
                               "topic_id": "t-1", "source_s3_key": "x",
                               "report_date": "2026-09-17"}])
    monkeypatch.setattr(ask_rewrite, "standalone_question",
                        lambda q, h, **kw: ("rewritten", True))

    prompts = []
    real_build = laa.build_rag_prompt
    def spy_build(question, chunks, **kw):
        prompts.append((question, kw))
        return real_build(question, chunks, **kw)
    monkeypatch.setattr(laa, "build_rag_prompt", spy_build)

    laa._rag_answer({"question": "when is he finishing it?", "caller_sub": SUB,
                     "history": [{"question": "ceiling grid?",
                                  "answer": "level 3 is behind"}]})

    asked_with, kwargs = prompts[0]
    assert asked_with == "rewritten", "the answering prompt must see what retrieval searched for"
    assert "history" not in kwargs
    assert "level 3 is behind" not in str(kwargs)


def test_the_answering_prompt_gets_the_original_when_not_rewritten(monkeypatch):
    """Spec 2026-09-20 SS3.1: when no rewrite ran, the answering prompt is
    byte-identical to today -- built from the caller's own `question`."""
    wire(monkeypatch, chunks=[{"chunk_text": "level 3 grid note", "id": "c-1",
                               "topic_id": "t-1", "source_s3_key": "x",
                               "report_date": "2026-09-17"}])

    prompts = []
    real_build = laa.build_rag_prompt
    def spy_build(question, chunks, **kw):
        prompts.append((question, kw))
        return real_build(question, chunks, **kw)
    monkeypatch.setattr(laa, "build_rag_prompt", spy_build)

    laa._rag_answer({"question": "what happened at Ellesmere?", "caller_sub": SUB})

    asked_with, kwargs = prompts[0]
    assert asked_with == "what happened at Ellesmere?"
    assert "history" not in kwargs
```

Note: `test_the_answering_prompt_gets_the_original_when_not_rewritten` does
not set `ASK_CONVERSATION_MEMORY` and sends no `history` — this is the
default/off path (`asked, rewritten = question, False` at `:1251`), which is
the majority of real first-turn questions and must stay untouched by this
change.

- [ ] **Step 3: Run the tests to verify the first one fails**

Run:
```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_lambda_ask_agent_rag.py -q -k "gets_asked_when_rewritten or gets_the_original_when_not_rewritten"
```
Expected: `test_the_answering_prompt_gets_asked_when_rewritten_and_no_history`
FAILS with `AssertionError: the answering prompt must see what retrieval
searched for` (`asked_with` is `"when is he finishing it?"`, not
`"rewritten"`), because `src/lambda_ask_agent.py:1608` still calls
`build_rag_prompt(question, ...)`.
`test_the_answering_prompt_gets_the_original_when_not_rewritten` PASSES
already (it pins today's unchanged default path).

- [ ] **Step 4: Implement — main call site**

In `src/lambda_ask_agent.py`, at `:1608`, change:

```python
        prompt = build_rag_prompt(question, chunks, mode=body.get("mode"),
                                  today=today, basis=basis, pinned_topic=pinned_topic)
```

to:

```python
        # Spec 2026-09-20 (amends the 2026-09-17 SS2 comment above): the
        # answering prompt must see what retrieval actually searched for. A
        # rewrite that resolved "it" to "PS4" for the embed at :1400 and the
        # web verdict at :1590 must resolve it here too, or the model has no
        # antecedent for the pronoun the rewrite already solved -- measured
        # UCPK2 2026-09-20, turn 2 of 3: "why do we talk it? who requested?"
        # retrieved the right chunks and then answered "the excerpts do not
        # contain enough context to identify what 'it' refers to." `question`
        # only when `rewritten` is False (SS3.1): byte-identical to today for
        # every first-turn question. Still no `history` parameter here --
        # that boundary (spec 2026-09-17 SS2) is unchanged.
        prompt = build_rag_prompt(asked if rewritten else question, chunks,
                                  mode=body.get("mode"),
                                  today=today, basis=basis, pinned_topic=pinned_topic)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run the Step 3 command again. Expected: both PASS.

- [ ] **Step 6: Prove the guard can go red**

Temporarily revert the `:1608` line back to
`build_rag_prompt(question, chunks, ...)` (remove the `asked if rewritten
else` substitution only, keep the comment or delete it — either way the
call must pass `question`). Run the Step 3 command again. Expected:
`test_the_answering_prompt_gets_asked_when_rewritten_and_no_history` FAILS
the same way it did in Step 3;
`test_the_answering_prompt_gets_the_original_when_not_rewritten` still
PASSES (this is exactly why a second, off-path test is required — a change
that only ever ran the on-path test could not by itself prove the
conditional, only that `asked` is unconditionally used). Restore the Step 4
implementation and re-run: both PASS.

- [ ] **Step 7: Commit**

```bash
git add src/lambda_ask_agent.py tests/unit/test_lambda_ask_agent_rag.py
git commit -m "$(cat <<'EOF'
Ask: the answering prompt sees what retrieval searched for

build_rag_prompt's main call site took the caller's literal question even
when a rewrite had already resolved a pronoun for retrieval and the web
verdict, so a follow-up like "why do we talk it? who requested?" fetched
the right chunks and then answered that it could not identify what "it"
meant. Pass `asked` instead of `question` only when `rewritten` is True;
byte-identical for every question that was not rewritten. No history
reaches the prompt, before or after this change.

test_the_answering_prompt_gets_the_original_and_no_history, which pinned
the reversed decision, is replaced by two tests -- one per branch -- both
keeping its "no history reaches the prompt" assertions verbatim.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: The language-leak retry call site

**Files:**
- Modify: `src/lambda_ask_agent.py` — `:1672`
- Modify: `tests/unit/test_lambda_ask_agent_rag.py` — a test beside the
  existing `insist_language=True` retry test(s)

**Interfaces:**
- Consumes: same `asked`/`rewritten`/`chunks`/`basis`/`today` locals already
  in scope at `:1672` (same function, same `try` block as Task 1's call site).
- Produces: no new names.

- [ ] **Step 1: Confirm the ground this test stands on**

Two facts were verified by the controller before this plan was written.
Re-confirm each with one command rather than assuming them:

```bash
grep -n "answer_language\|insist_language" tests/unit/test_lambda_ask_agent_rag.py   # expect: no hits
grep -nE "^import answer_language|^from answer_language" src/lambda_ask_agent.py      # expect: no hits
```

1. **No existing test covers the retry path.** There is no pattern in this file
   to copy — the test below is the first one to reach `:1672`.
2. **`answer_language` is imported INSIDE `_rag_answer`** (`src/lambda_ask_agent.py:1317`;
   the function begins at `:1189`) and nowhere at module level. A function-local
   import never creates a module attribute, so **`laa.answer_language` does not
   exist**: `monkeypatch.setattr(laa, "answer_language", …)` raises
   `AttributeError`, and adding `raising=False` to silence that would patch
   nothing at all while the test still passed — a test that verifies nothing.

The patch target is therefore the `answer_language` **module object**, which the
function-local import resolves to through `sys.modules`. Import it in the test
module and patch it there.

- [ ] **Step 2: Write the failing test**

Add, in the same test file, near the tests found in Step 1:

```python
def test_the_retry_prompt_also_gets_asked_when_rewritten(monkeypatch):
    """Spec 2026-09-20: the 1-in-13 language-leak retry must not regress to
    pronoun-blind answering just because it rebuilds the prompt at a second
    call site (src/lambda_ask_agent.py:1672)."""
    monkeypatch.setenv("ASK_CONVERSATION_MEMORY", "true")  # Task 10: gated
    wire(monkeypatch, chunks=[{"chunk_text": "level 3 grid note", "id": "c-1",
                               "topic_id": "t-1", "source_s3_key": "x",
                               "report_date": "2026-09-17"}])
    monkeypatch.setattr(ask_rewrite, "standalone_question",
                        lambda q, h, **kw: ("rewritten", True))
    # `answer_language` is imported inside _rag_answer, so it is NOT an
    # attribute of `laa` — patch the module object the function-local import
    # resolves to. See Step 1.
    import answer_language
    monkeypatch.setattr(answer_language, "violates",
                        lambda answer: True)  # force the retry every time

    prompts = []
    real_build = laa.build_rag_prompt
    def spy_build(question, chunks, **kw):
        prompts.append((question, kw))
        return real_build(question, chunks, **kw)
    monkeypatch.setattr(laa, "build_rag_prompt", spy_build)

    laa._rag_answer({"question": "when is he finishing it?", "caller_sub": SUB,
                     "history": [{"question": "ceiling grid?",
                                  "answer": "level 3 is behind"}]})

    assert len(prompts) == 2, "expected the primary prompt and the retry prompt"
    for asked_with, kwargs in prompts:
        assert asked_with == "rewritten"
        assert "history" not in kwargs
```

Two things to watch while running it:

- **Prove the patch bites before trusting the assertion.** `violates` returning
  `True` unconditionally must make `len(prompts) == 2`. If it comes back `1`,
  the patch did not take effect and the test is measuring nothing — do not
  "fix" it by relaxing the assertion; find out why the retry did not fire.
- The retry also re-runs `llm_utils.call_llm`, so whatever `wire()` stubs that
  with must tolerate a second call. If it raises or returns a fixture that only
  works once, extend the stub — do not drop the retry from the test.

- [ ] **Step 3: Run the test to verify it fails**

```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_lambda_ask_agent_rag.py -q -k retry_prompt_also_gets_asked
```
Expected: FAIL — `prompts[1]`'s `asked_with` is `"when is he finishing it?"`,
not `"rewritten"`, because `:1672` still calls
`build_rag_prompt(question, ...)`.

- [ ] **Step 4: Implement — retry call site**

In `src/lambda_ask_agent.py`, at `:1672`, change:

```python
            retry_prompt = build_rag_prompt(question, chunks, mode=body.get("mode"),
                                            today=today, basis=basis,
                                            insist_language=True, pinned_topic=pinned_topic)
```

to:

```python
            # Same substitution as the primary prompt above (:1608) and for
            # the same reason: the retry must not silently revert to
            # pronoun-blind answering on the 1-in-13 turns that need it.
            retry_prompt = build_rag_prompt(asked if rewritten else question, chunks,
                                            mode=body.get("mode"),
                                            today=today, basis=basis,
                                            insist_language=True, pinned_topic=pinned_topic)
```

- [ ] **Step 5: Run the test to verify it passes**

Run the Step 3 command again. Expected: PASS.

- [ ] **Step 6: Prove the guard can go red**

Temporarily revert `:1672` to `build_rag_prompt(question, chunks, ...)`
(remove only the substitution). Run the Step 3 command again. Expected:
FAIL, same assertion as Step 3. Restore the Step 4 implementation and
re-run: PASS. Also re-run Task 1's two tests plus this one together to
confirm nothing regressed:
```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_lambda_ask_agent_rag.py -q -k "gets_asked_when_rewritten or gets_the_original_when_not_rewritten or retry_prompt_also_gets_asked"
```
Expected: `3 passed`.

- [ ] **Step 7: Commit**

```bash
git add src/lambda_ask_agent.py tests/unit/test_lambda_ask_agent_rag.py
git commit -m "$(cat <<'EOF'
Ask: the language-leak retry prompt also sees what was searched for

The retry at src/lambda_ask_agent.py:1672 rebuilds the answering prompt on
the ~1-in-13 turn where the first answer leaked the wrong language. It used
the same `question` argument the primary prompt did before the prior
commit, which would have reintroduced the pronoun-blind failure on retried
turns even after the primary path was fixed. Same substitution, same
condition: `asked` only when `rewritten` is True.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Confirm nothing else still asserts or documents the old behaviour

**Files:** none modified unless Step 1 finds something (then fixed here,
report as a deviation from "no other file changes").

**Interfaces:** none — this is a verification task.

- [ ] **Step 1: Grep for other tests or comments pinning the reversed decision**

Run:
```bash
grep -rn "gets_the_original_and_no_history\|the answering prompt below still gets\|SS2: a chat history is a copy" src tests docs/superpowers/specs/2026-09-17-ask-conversation-memory-design.md
```

Expected matches and their disposition:
- `tests/unit/test_lambda_ask_agent_rag.py` — no match after Task 1 (the
  test was renamed/replaced, not left in place under its old name).
- `src/lambda_ask_agent.py:1253-1259` (the comment above `asked, rewritten =
  question, False` at `:1251`, quoted in the spec's §1 as stating the
  omission was deliberate) — this comment predates the rewrite step's own
  history-handling and describes §2's actual, still-true rule ("history ...
  must never be the thing retrieval or the web branch acts on"); it does
  NOT claim `build_rag_prompt` always receives `question` regardless of
  `rewritten` — re-read it in place. If, on rereading, the wording does
  read as asserting the old (reversed) behaviour for the answering prompt
  specifically, tighten it in this step to say what Task 1's new comment at
  `:1608` says (history stays out; `asked` now reaches the answering prompt
  when a rewrite ran) rather than leaving two comments in the same function
  contradicting each other.
- `docs/superpowers/specs/2026-09-17-ask-conversation-memory-design.md` §2 —
  do not edit this file. It is the parent spec and its §2 argument (history
  never reaches the answering prompt) remains true; this plan's spec already
  documents itself as an amendment rather than an edit to §2's text.

- [ ] **Step 2: Record findings**

In the task report (not a new file), state: whether the grep found any
stale assertion or comment beyond the ones listed above, and if the
`:1253-1259` comment was edited, quote the before/after.

- [ ] **Step 3: If a stale test assertion was found and fixed, commit it separately**

```bash
git add <file>
git commit -m "$(cat <<'EOF'
Ask: remove a stale assertion of the reversed answering-prompt decision

<one line naming exactly what was stale and where>

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```
If Step 1 found nothing to fix, skip this step (no empty commit).

---

### Task 4: Full unit suite, push, PR into `develop`

**Files:** none modified (unless the suite finds a regression, fixed in the
task that caused it).

- [ ] **Step 1: Full unit suite**

Run:
```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit -q
```
Expected: all pass except any pre-existing skips already present before this
branch (record the pass/skip counts in the report; do not silently accept a
new skip that was not there before).

- [ ] **Step 2: Push and open the PR (merge is the owner's)**

```bash
git push -u origin docs/specs-2026-09-20
gh pr create --base develop --title "Ask: the answering prompt sees what it searched for" --body "$(cat <<'EOF'
## Summary
- The rewrite step already resolves a follow-up's pronoun for retrieval
  (`dashscope_utils.embed([asked])`) and for the web-answer verdict
  (`verdict_question=asked`, PR #876), but `build_rag_prompt` -- the call
  that writes the sentence the user reads -- still took the caller's literal
  `question`. Measured on UCPK2 dev 2026-09-20: turn 2 of 3 retrieved the
  right chunks and then answered "the excerpts do not contain enough context
  to identify what 'it' refers to," even though turn 3 (same chunks, no
  pronoun) answered correctly.
- Both `build_rag_prompt` call sites (`src/lambda_ask_agent.py:1608` primary,
  `:1672` language-leak retry) now receive `asked if rewritten else question`.
  Byte-identical to today when no rewrite ran. No history reaches the
  answering prompt, before or after -- unchanged from the 2026-09-17 design's
  SS2.
- `web_answer.answer()`'s arguments are unchanged: `question` still goes to
  the web lookup and the admission screen; `verdict_question=asked` still
  goes only to the verdict, exactly as PR #876 shipped it. No schema change,
  no new request or response field.

Spec: `docs/superpowers/specs/2026-09-20-ask-answer-sees-the-searched-question.md`
(amends `docs/superpowers/specs/2026-09-17-ask-conversation-memory-design.md`
SS2 and SS3.3; does not reopen SS1 decisions 2-5, the distance gate, or
history's exclusion from the answering prompt).

## Test plan
- [x] `test_the_answering_prompt_gets_asked_when_rewritten_and_no_history` --
  new; replaces `test_the_answering_prompt_gets_the_original_and_no_history`,
  which pinned the reversed decision.
- [x] `test_the_answering_prompt_gets_the_original_when_not_rewritten` -- new;
  proves the no-rewrite path is byte-identical to today.
- [x] `test_the_retry_prompt_also_gets_asked_when_rewritten` -- new; covers
  the 1-in-13 language-leak retry call site.
- [x] Every new/changed test still asserts no history text reaches the
  answering prompt.
- [x] Full unit suite green (counts in the task report above).

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```
Merging into `develop` (deploys TEST) is the owner's call.
