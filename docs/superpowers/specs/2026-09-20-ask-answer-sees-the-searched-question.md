# The answering prompt sees what it searched for

**Date:** 2026-09-20
**Status:** design — awaiting review
**Scope:** backend (`lambda_ask_agent.py`, `web_answer.py`), tests only. No
schema change, no new request field, no client change beyond what already
ships.
**Amends:** `docs/superpowers/specs/2026-09-17-ask-conversation-memory-design.md`
§2 and §3.3. It does not reopen §1's decisions 2–5, §4.4's distance gate, or
anything about `history`, which still never leaves the rewrite step. It
resolves that spec's own §10 question 1 ("is the `asked` residue
acceptable?") for one additional surface: the answering prompt.

---

## 0. The measured gap

UCPK2 dev, three consecutive turns in one Ask session, 2026-09-20:

1. *"did we talk about ps4?"* → correct, cited answer.
2. *"why do we talk it? who requested?"* → the UI rendered `Searched for: why
   do we talk about PS4? who requested?` (the rewrite worked and is proven by
   its own displayed line), then answered: *"The provided excerpts do not
   contain enough context to identify what 'it' refers to or why it is being
   discussed, nor who specifically requested it."*
3. The same question typed out in full, no pronoun → correct, detailed
   answer.

The retrieval half of conversation memory worked exactly as designed: the
rewrite resolved "it" to PS4 and fetched the right chunks (proof: turn 3, with
those same chunks, answers correctly). The answering half was never told.
`_rag_answer` embeds `asked` and then builds the prompt from `question` —
turn 2's model received the retrieved excerpts plus the caller's literal *"why
do we talk it? who requested?"*, with no antecedent for "it" anywhere in the
prompt it can see. A model given a pronoun and no referent correctly reports
that it cannot resolve the pronoun; that is not a hallucination, it is the
prompt working as specified. The specification is the defect.

## 1. Root cause, verified against `origin/main`

* `src/lambda_ask_agent.py:1400` — `query_vec = dashscope_utils.embed([asked])[0]`.
  Retrieval embeds the rewritten string.
* `src/lambda_ask_agent.py:1608` — `prompt = build_rag_prompt(question, chunks, ...)`.
  The primary answering prompt is built from the caller's original text.
* `src/lambda_ask_agent.py:1672` — the language-leak retry rebuilds with
  `build_rag_prompt(question, chunks, ...)` again: same original text, same
  omission, on the one-in-thirteen retry path too.
* `src/lambda_ask_agent.py:1253-1259` — the comment states the omission is
  deliberate: *"the answering prompt below still gets the caller's own
  `question`, not `asked` (SS2: a chat history is a copy taken before a
  deletion and must never be the thing retrieval or the web branch acts
  on)."*
* `tests/unit/test_lambda_ask_agent_rag.py:534`
  `test_the_answering_prompt_gets_the_original_and_no_history` pins exactly
  this: it asserts `asked_with == "when is he finishing it?"` (the pronoun
  form) even though `asked` was rewritten to a standalone question in the same
  test.
* PR #876 (`b416488`, "Ask: the verdict judges the question retrieval actually
  searched for") changed one thing: the web-answer **verdict** now judges
  `asked` (`src/lambda_ask_agent.py:1586-1591`, `verdict_question=asked`). It
  did not touch `build_rag_prompt`'s arguments. So today there are three
  destinations for the rewritten question — the embed call, the verdict — and
  one holdout: the thing that actually writes the sentence the user reads.

The gap is narrow and exact: `build_rag_prompt` at both its call sites takes
`question` where it should, when a rewrite happened, take `asked`.

## 2. Why this does not reopen §2 of the parent spec

§2's title is "why history goes to the rewrite and never to the answer," and
its argument, in its own words, is that *"a chat history is a COPY taken
before the deletion. It passes through no predicate, because it is not
retrieved — it is replayed."* Its worked example is turn 2's **answer**
naming "he" — i.e., prose from a prior model response, verbatim, reaching the
next prompt. That is the channel §2 closes, and this spec does not reopen it:
**no history — no prior question, no prior answer, no turn count — reaches
the answering prompt under this change.** The only thing crossing into the
answering prompt is `asked`, the single string already produced by the
rewrite step for a different purpose.

Read against §2's own three sub-arguments:

* **§2.1, the deletion bypass.** The bypass is a *copy* of retrieved text
  evading the tombstone predicate. `asked` is not a copy of retrieved text; it
  is the query the rewrite step produced and that retrieval *already used* to
  fetch the very chunks the answering prompt is grounded in (`asked` at
  `:1400` and `chunks` from that same call are the two inputs `build_rag_prompt`
  would receive). Nothing named in `asked` is asserted as fact by virtue of
  being in the prompt — the model still must ground its answer in the fenced
  excerpts, which passed the tombstone predicate on this call, not on a
  frozen one.
* **§2.2, prompt injection.** §2.2's concern is that history is *user-typed
  text with no "excerpts are data" rule over it*. `asked` is also user-derived
  text with no such rule — but it is not a new user-typed span: it is the
  same class of string the prompt already contains via `question` itself
  (the caller's own words), just resolved. Adding a second copy of "words the
  caller is responsible for" changes exposure by degree, not by kind, and only
  for turns where a rewrite ran.
* **§2.3, ACL correctness.** §2.3 is about retrieval permissions, which are
  untouched here: `asked` already goes through the ACL-checked retrieval path
  before this spec, and continues to.

So §2's actual target — turn-to-turn replay of prior answers and prior
questions — stays closed. What follows makes the narrower case for the one
string §2 did not itself route anywhere: `asked`.

### 2.1 `asked` already crosses every boundary this would cross

Three facts, each independently verified, establish that including `asked` in
the answering prompt opens no channel that is not already open on the exact
same request:

1. **It is already sent to the vector store.** `dashscope_utils.embed([asked])`
   (`:1400`) — DashScope receives it before this spec is even relevant.
2. **It is already sent to an LLM provider.** The web-answer verdict call
   (`:1586-1591`, `verdict_question=asked`, landed by PR #876) already hands
   `asked` to the same class of model call this spec targets — the verdict
   prompt is a `web.client.call`, i.e. an LLM inference over `asked`. The
   answering prompt is the next LLM call downstream in the identical request.
3. **It is already displayed to the user.** `fieldsight-ui`
   `scripts/composites/ask-chat.js:1140` (verified on both `origin/main` and
   `origin/dev`): `'Searched for: ' + m.asked`, gated only on `m.asked &&
   m.asked !== m.question` (`:1136`). The backend already returns `asked`
   in the response body whenever a rewrite ran (`asked if rewritten else
   None`, e.g. `:1590`, `:1600`), specifically so it can be rendered.

Given 1–3, the answering prompt is the *last* of the request's four
consumers — vector store, verdict model, UI, answer model — to receive
`asked`, not the first. There is no new party seeing this string, and no new
persistence: it is not written to any store this spec adds: it already
travels over the wire in the response and is already computed before the
answering prompt is built.

### 2.2 The residual exposure, stated plainly

`asked` is *derived from history* (§4.2 of the parent spec: `standalone_question(question,
history)`), so a name or phrase that originated in a since-deleted recording
can appear inside the rewritten question text — e.g. "why do we talk about
PS4" could, on a different transcript, have rewritten to "why do we talk about
the incident James reported," where "James" and "the incident" trace to a
turn whose source recording is later deleted. That is real, and it is not
this spec's to wave away.

**It is already accepted, in this exact shape, by the parent spec.** §3.3 of
the 2026-09-17 design states it about the *rendered* `asked` line: *"if the
record behind that turn has since been deleted, the nouns it lifted ... are
re-shown in the UI even though the answer below is built only from live
retrieval. This is stated rather than hidden ... The mitigation is that
`asked` is a single question the reader just took part in composing, not a
passage of recovered content."* §10 question 1 leaves that judgment open —
*"An owner decision, not a technical one"* — but the mitigation it names
(single composed question, not recovered content; the reader is the person
who saw the original content in their own chat before deletion; already shown
to them) applies identically whether `asked` sits in a UI line or inside a
prompt fenced the same way `question` already is.

**Verdict: acceptable, on the same terms §3.3 already accepted, and no
wider.** The reasoning does not generalize past `asked` — it does not license
sending history, prior answers, or anything not already computed and already
displayed. If a future change wanted to send something with a larger blast
radius than a single rewritten question, this spec's argument would not cover
it and a fresh review would be needed. If an owner instead judges the §3.3
residue unacceptable, the correct fix is the one §3.3 itself names — stop
rendering (and, by the same logic, stop feeding to the answer) `asked` when
the rewrite drew on history — not building a second, inconsistent answer for
the same string in two places.

## 3. Behaviour specified

### 3.1 When `rewritten` is `False`

The answering prompt is **byte-identical to today**. `build_rag_prompt(question,
chunks, ...)` is called with `question` exactly as now; nothing about this
path changes when no rewrite ran, including the majority of first-turn
questions where `asked == question` and `rewritten` is `False`.

### 3.2 When `rewritten` is `True`

Both call sites pass `asked` instead of `question`:

* `src/lambda_ask_agent.py:1608` — `prompt = build_rag_prompt(asked if rewritten else question, chunks, ...)`.
* `src/lambda_ask_agent.py:1672` — the language-leak retry prompt, same
  substitution, so the one-in-thirteen retry does not regress back to the old
  pronoun-blind behaviour.

Nothing else in `build_rag_prompt`'s signature changes — no `history`
parameter is added, matching §6 of the parent spec ("No history in the
answering prompt") which this amendment leaves standing.

### 3.3 The web-answer fallback path

`asked` should **not** reach it, and the code already establishes why by
precedent, not by extension of this spec's own reasoning:

* `src/web_answer.py:answer()` docstring (`:144-159`): *"It reaches the
  verdict and NOTHING else. The lookup below still sends the asker's own
  `question`, and `question_admission.screen` still screens that same text, so
  a rewrite ... never widens what leaves the account."*
* `tests/unit/test_answering_from_the_open_web.py:223`
  `test_the_rewrite_never_reaches_the_web_or_the_screen` pins it: the lookup
  prompt (`fake.web_prompt`) contains the follow-up (`question`), not the
  standalone rewrite, and `question_admission.screen` is called with
  `question`.

This is a different boundary from §2's: the web lookup and the admission
screen send text to a **third-party search engine outside the account**,
which is exactly the widening §2.1 and §4.3 of the parent spec were written
to prevent regardless of where the text originated. `asked` reaching an
internal LLM call that answers from the account's own already-retrieved
chunks (§2.1–2.2 above) is not the same act as `asked` reaching an external
search engine. This spec changes nothing about `web_answer.answer()`'s
arguments; `question` keeps going to the lookup and the screen, `verdict_question=asked`
keeps going only to the verdict, unchanged from PR #876.

## 4. Test changes

`tests/unit/test_lambda_ask_agent_rag.py:534`
`test_the_answering_prompt_gets_the_original_and_no_history` encodes the
decision this spec reverses (its own docstring: *"SS2's whole claim"*) and
must be replaced, not deleted-and-forgotten, so the old claim does not simply
vanish from the suite. Replace it with two assertions in the same file, one
per branch of §3:

* `test_the_answering_prompt_gets_asked_when_rewritten_and_no_history` — same
  fixture as today's test (`ask_rewrite.standalone_question` stubbed to return
  `("rewritten", True)`), but assert `asked_with == "rewritten"` (not the
  literal caller text) **and** keep the existing negative assertions verbatim:
  `"history" not in kwargs` and `"level 3 is behind" not in str(kwargs)`. The
  new test still proves no history text reaches the prompt; it only changes
  which string is expected as the question argument.
* `test_the_answering_prompt_gets_the_original_when_not_rewritten` — stub
  `standalone_question` to return `(question, False)` (or omit the stub, since
  that is the no-history default), assert `asked_with == question`, i.e.
  §3.1's byte-identical guarantee.

Both new tests keep the "revert this line, watch it go red" discipline the
parent spec's §7 already applies to its own tests 8–10 and 13: reverting the
`asked if rewritten else question` substitution at either call site must fail
the corresponding test, not merely pass because the fixture happens to make
`asked == question`.

No change is needed to `test_the_rewrite_never_reaches_the_web_or_the_screen`
or `test_the_verdict_judges_the_rewritten_question` — §3.3 above leaves that
surface exactly as PR #876 left it.

## 5. What this does not change

* No history reaches the answering prompt. Still zero — §2 of the parent spec
  stands.
* No change to retrieval: `dashscope_utils.embed([asked])` at `:1400` is
  unchanged.
* No change to ACL or tombstone filtering: retrieval's predicate is untouched;
  the answering prompt still only quotes chunks that passed it on this call.
* No new request field, no new response field. `asked` already exists in the
  response (`:1590`, `:1600`) and is already rendered (`ask-chat.js:1140`).
* No change to `web_answer.answer()`'s arguments: `question` still goes to the
  lookup and the admission screen; `verdict_question=asked` still goes only to
  the verdict, exactly as PR #876 shipped it.
* No change to the metric route, `time_range`, or the distance gate — none of
  those are downstream of `build_rag_prompt`.

## 6. Risks

| Risk | Cost | Detected by |
|---|---|---|
| A rewrite hallucinates a fact not supported by the history (e.g. invents a name) and that fact now shapes the answer, not just the retrieval query. | The answer's wording could echo an invented noun even though the model is still constrained to cite only fenced excerpts. | `ask_rewrite`'s own budget/shape validation (parent spec §4.2, tests 3–7: a malformed or failed rewrite falls back to the original, never fabricates silently); a manual reread of turn 2/3 pairs during TEST verification (parent spec §8) should include reading the answer text, not only the "Searched for" line. |
| `asked` carries a name from a since-deleted recording into the answering prompt (§2.2 above), where previously only `question` — the caller's own current words — did. | The deleted noun can shape phrasing in an answer, not only appear in a UI status line. | Same detector the parent spec already has none of: this is the accepted, stated residue of §3.3, unchanged in kind. No new monitoring is proposed here beyond what §3.3 already carries as an open owner question (parent §10.1). |
| The retry prompt at `:1672` is missed in the edit (only the primary prompt at `:1608` updated). | The 1-in-13 language-leak retry silently reverts to pronoun-blind answering, reintroducing turn 2's failure only on the retry path, which is rarer and easy to miss in review. | Test in §4 should be parametrized over, or duplicated for, the retry call site; `test_lambda_ask_agent_rag.py`'s existing retry tests (search for `insist_language=True`) are the place to add the `asked`-argument assertion. |
| A reviewer conflates this change with reopening §2 wholesale and either blocks it on §2's argument (which does not apply, per §2 above) or, in the other direction, uses it as precedent to route full history to the answering prompt. | Either a stalled fix or a scope-creep regression of §2's actual protection. | This document's §2 is written to be quoted directly against either move; the amendment's own title ("amends §2 and §3.3... does not reopen §1's decisions 2-5") is the guardrail. |

## 7. Considered and rejected

* **Route the full rewrite context (question + resolved antecedent) into the
  prompt as a structured hint, separate from `asked`.** Rejected: this is a
  second string the answering prompt would need a "this is data, not fact"
  rule for (§2.2), for no gain over simply using `asked` in place of
  `question` — the rewrite already did the resolution; a second hint would
  just restate it.
* **Fix this by improving the rewrite instead of the answering prompt.** The
  rewrite already worked in the measured session (§0: the displayed "Searched
  for" line proves it). There is nothing to improve upstream; the defect is
  that a correct rewrite's output was discarded before the second model call.
* **Render `asked` to the model but keep rendering `question` to the reader,
  or vice versa.** Rejected as an inconsistency the parent spec's own §3.3
  reasoning forbids: if the residual exposure of `asked` is acceptable enough
  to show the reader (already shipped), it is acceptable enough to show the
  model answering on the reader's behalf; treating the two differently has no
  argument behind it in either direction.
* **Only apply this when the rewrite drew on no history (i.e., trivial
  rewrites).** This is the exact lever the parent spec's §3.3 names for the
  UI line if the residue is ever judged unacceptable. Not adopted here because
  it would remove most of the value on both surfaces for the same reason: a
  rewrite that draws on no history is usually a no-op (`asked == question`),
  so restricting to that case would leave turn-2-style follow-ups — the entire
  case this amendment exists for — unfixed.
