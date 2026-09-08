# External corroboration without Anthropic

Status: spec. Supersedes the vendor half of
`2026-08-31-ask-external-corroboration-design.md`; every honesty rule in that
document stands unchanged.

## 1. What is being asked for, and what is already built

The product gap is differentiator ①: **the answer must not be limited to what
we collected.** When a meeting touches something outside our corpus, the agent
goes and looks, and it says so while it looks.

Almost all of that exists and is switched off. Verified in the working tree,
not recalled:

| piece | state |
|---|---|
| four-step pipeline, honesty rules | `src/corroboration.py` |
| which claims are worth checking | `src/corroboration_gate.py`, `MAX_ENTITIES = 3` |
| the second pass is already a SEPARATE request | `ask-chat.js:490` fires `corroborate()` after the answer lands and patches the card |
| **"Checking the web…" while it looks** | `ask-chat.js` `_pending` branch — already the exact words asked for |
| "Web check unavailable" on failure | `_failed` branch, deliberately a visible muted line |
| four result states + source domain per claim | `CORROB_STATE`, `sourceHost()` |
| the switch | `EnableExternalCorroboration`, `false` on both stacks |

So this is **not** a feature to build. It is a vendor to replace, plus one
display bug the vendor swap introduces, plus a budget to re-cut.

**Streaming is not required and should not be coupled to this.** The reason
the user reached for streaming was to say "I am searching now" without being
held to the answer's latency budget. The second pass already runs on its own
request after the answer is on screen, and the pending state already says those
words. Streaming remains worth doing for the FIRST answer — it is unrelated to
this and belongs in its own project.

## 2. Why Anthropic has to go, and what replaces it

`corroboration_client.py` posts to `api.anthropic.com` and uses Anthropic's
server-side `web_search_20250305` tool. The search runs on their side; the
shared `llm_utils` client cannot do this at all (it sends no `tools`).

Measured 2026-09-08 against OpenRouter, one entity + one claim, n=3 per
configuration because temperature 0 is not deterministic on this endpoint:

| configuration | elapsed | sources |
|---|---|---|
| `google/gemini-3.8-flash` + `plugins:[{"id":"web"}]` | **10.3 / 11.4 / 11.3 s** | 6 / 4 / 7 |
| `google/gemini-3.8-flash:online` | 12.4 / 13.0 / 12.6 s | 5 / 7 / 5 |
| `google/gemini-3.8-flash`, no search (control) | 4.2 / 4.9 / 4.2 s | 0, empty body |
| `meta/muse-spark-1.3-contributor` + web plugin | 16.1 / 16.5 / 18.2 s | **0** |

Three decisions fall straight out.

**The plugin form, not the `:online` suffix.** Same model, same question; the
suffix was over budget on all three runs. This is a measurement, not a
documented difference.

**muse-spark is disqualified, and not for being slow.** It returned no sources
and wrote the search as prose:

> "I'll search the web to verify the NZS 3604 claim. **Initial results support
> the claim...**"

HTTP 200, 1200 tokens, nothing raised. It narrated a search it never ran and
then asserted its findings. For a feature whose only job is to check claims
against the outside world, a confident fabricated corroboration is the worst
output in the system — worse than the feature being off. This is the same shape
already recorded in `anthropic-tooluse-silent-shapes` (a tool call written into
visible text while the tool never runs), reproduced on a second vendor.

**The control returned an empty body.** Gemini with no plugin gave
`completion_tokens: 0` and no content. Whatever else that is, it means "no web
plugin configured" fails as an empty answer rather than as an error — so the
existing `HTTP 200 + empty content = failure` guard must cover this path too.

## 3. The display bug the swap introduces

Anthropic returns real result URLs. Gemini returns Google grounding redirects:

```json
{"type": "url_citation",
 "url_citation": {
   "url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQFb5s…",
   "title": "wikipedia.org",
   "start_index": 0, "end_index": 178}}
```

`corroboration.py:246` builds `{"title", "url", "published"}` and the UI's
`sourceHost()` derives the displayed domain by parsing `url`. Ported naively,
**every source on every claim renders as `vertexaisearch.cloud.google.com`.**

That is not cosmetic. The 2026-08-31 design puts the source domain on every
claim precisely because "a reader judges *is this a source I trust* from the
domain", and this product's whole differentiator is that external information
is visibly separated from what the room said. A card that attributes all
external evidence to a Google redirect host destroys the thing the feature
exists to provide.

**Decision:** the source record gains an explicit `domain`, set from Gemini's
`title` (which carries the host) and, on any vendor that returns real URLs,
from the URL. The UI prefers `domain` and falls back to parsing `url`. `title`
stops being overloaded as both "headline" and "host".

Open question for the owner, not for the implementer: Gemini's first source for
the probe question was `wikipedia.org`. Whether that is an acceptable authority
for construction-compliance claims is a product judgement. The pipeline can
report the domain honestly; it cannot make a weak source strong.

## 3.5 The fabrication guard does not exist yet, and the obvious place to put it lies

Review found that §6's requirement — a no-annotation payload must never become a
corroborated state — is not enforced anywhere today, and the pipeline is
positively arranged to let it through:

- `corroboration_client.py:131` sets `Reply.searched`. **No caller reads it.**
- `corroboration.py:300` logs `search_error` and then **runs reconcile on
  `search.text` anyway.**

So muse-spark's output — 200 OK, confident prose, zero sources — reaches
reconcile, which reads the fabricated findings as if they were web results and
can return `corroborated`. The card renders a **"Confirmed" chip with no
sources**, because `renderCorroborationItem` simply omits the source row when
`sources` is empty. That is a fabricated external corroboration presented to a
customer as verified. It is the single worst output this system can produce and
it is reachable today.

The tempting fix is also wrong. Every failure in `corroborate()` returns
`timed_out=True`, and the UI renders that as *"The check ran out of time"* — a
false sentence for a vendor that answered in four seconds without searching.
The 2026-08-31 design already separates `truncated` from `timed_out` because
"a reader who sees three cards deserves to know which"; **"did not search" is a
third thing** and needs its own flag and its own words.

**Decision.** `searched` becomes load-bearing:

- The client sets `searched=False` when the response carries no web results,
  whatever the prose says.
- `corroborate()` returns `{corroborations: [], searched: false,
  timed_out: false}` and **never calls reconcile** on unsearched text.
- The UI gains a state for it, distinct from both the timeout line and the
  failure line: the web check did not happen. Copy is the owner's to approve;
  it must not say "not found", which asserts a search that did not run.

This also replaces an Anthropic-specific safeguard that disappears with the
vendor. `corroboration_client.py:27-33` keeps thinking ON precisely because
disabling it makes Claude write the tool call into visible text instead of
running it — a *prevention* for this exact failure. Gemini offers no documented
equivalent, so the detection has to carry the weight the prevention used to.

## 4. Re-cutting the budget

Current: `HARD_STOP 24s` = extract 4 + search 12 + reconcile 6, sized for
haiku. Gemini's search alone measured 10.3–11.4s, leaving about one second of
margin inside a 12s slice.

The owner has relaxed the latency requirement for this path, on the correct
reasoning that a user who has been told "checking the web" is not waiting on a
promise of immediacy. **That relaxation is real but it is not unlimited:**
`/ask/corroborate` is served through API Gateway REST, which terminates the
integration at 29s regardless of what any budget here says. Raising
`HARD_STOP` past that converts a shaped `timed_out: true` body into a raw
gateway error — strictly worse, because the module's contract is that a missed
deadline is reported, never disguised as an empty result.

**These three numbers must be measured, not reasoned about.** The budget in the
file today was measured; replacing measured numbers with estimates is a
regression even when the estimates are sensible.

An earlier draft of this section proposed extract 3 + search 15 + reconcile 4
on the reasoning that classification is cheap. **Measured on Gemini, n=3, that
was wrong in the dangerous direction:**

| step | draft estimate | measured |
|---|---|---|
| extract | 3s | **6.10 / 7.60 / 6.24 s** |
| search | 15s | 10.3 / 11.4 / 11.3 s |
| reconcile | 4s | 2.52 / 4.07 / 4.40 s |

Worst case 7.6 + 11.4 + 4.4 = **23.4s against a 24s hard stop** — no margin.
The estimate was half the real cost of the step it was most confident about.
`tests/unit/test_corroboration_steps.py` asserts only that the budgets sum
under `HARD_STOP`, so the wrong numbers would have shipped green.

Extract is slow because it is verbose: 618–796 completion tokens for three
entities. The obvious fix does not work — capping `max_tokens` to 300 truncated
the JSON on **3 of 3** runs (`finish_reason: length`), which turns a slow step
into a broken one. Output has to be shortened by asking for less, then
re-measured.

**Proposed:** `HARD_STOP` 24 → **27**, budgets extract 8 + search 13 +
reconcile 5 = 26. 27 still sits under API Gateway's 29s so a missed deadline is
still reported as a shaped `timed_out` body rather than a raw gateway error,
and the UI's 35s client timeout (`ask.js` `MODEL_CALL_TIMEOUT_MS`) is already
past the gateway deliberately — nobody should "fix" it down to match this.

## 5. Scope

In:
1. `corroboration_client.py` → OpenRouter, `plugins:[{"id":"web"}]`, response
   read from `annotations`; Anthropic request/response handling deleted, not
   left behind a flag.
2. `sources` gain `domain`; UI prefers it.
3. Budgets re-cut against measured extract/reconcile times.
4. The empty-body guard extended to cover "plugin missing → empty content".
5. A dedicated credential. **Not** `QWEN_API_KEY`: on `AskAgentFunction` that
   is `!If [UsesSeparateChatVendor, QwenChatApiKey, DashScopeApiKey]`, so on a
   stack still pointing at DashScope it resolves to the wrong vendor's key and
   401s everything. A new `CorroborationApiKey` parameter and
   `CORROBORATION_API_KEY` env var, wired in BOTH workflow files.
6. `CORROBORATION_CHEAP_MODEL`'s default. It is `claude-haiku-4-5`
   (`corroboration.py:56`) and extract and reconcile share the client, so
   pointing the client at OpenRouter without changing this makes every extract
   fail against an invalid model id — reported to the user as
   `timed_out: true`, forever, on 100% of requests.
7. The `searched` guard of §3.5, backend and UI.
8. Mocks and tests pinning the Anthropic shape: `tests/unit/
   test_corroboration_client.py` (including a test asserting the client IS
   Anthropic), `test_corroboration_steps.py`, and the UI's mock sources in
   `scripts/api/ask.js`.

Out:
- **Removing `ANTHROPIC_API_KEY` from `AskAgentFunction`.** It was in an earlier
  draft and review showed it is unsafe here: `llm_utils._call_anthropic` reads
  it whenever `LLM_PROVIDER=anthropic`, and `deploy-prod.yml`'s fallback is
  `${{ vars.PROD_LLM_PROVIDER || 'anthropic' }}`. Removing the key makes an
  invisible repo variable load-bearing for every prod Ask answer. Change that
  fallback first, in its own change, verify, and only then remove the key.
- Streaming (own project; see §1).
- The gate's policy. `MAX_ENTITIES = 3` and the person/site exclusions are
  privacy decisions from the 2026-08-31 design and are not reopened here.
- Turning the switch on. That stays a commercial decision by the owner, and
  the parameter description saying so stays exactly as written.

## 6. How this is verified

Unit tests cannot show that a search ran — that is the exact failure muse-spark
demonstrated. Two levels:

- **Driven tests** for the response mapping: given a recorded OpenRouter
  payload, the client yields sources carrying the right `domain`; given a
  no-annotation payload with confident prose, it yields **zero** sources and a
  recorded reason, never a corroborated state.
- **A live probe against TEST**, recorded in the PR: n≥3, real entity, asserting
  sources are present, domains are real hosts, and elapsed is inside the new
  budget. A green unit suite over a vendor swap says nothing about whether the
  vendor searched.

## 7. Risks

| risk | why it is not hypothetical |
|---|---|
| Fabricated corroboration | measured on muse-spark, this document §2 |
| Every source shown as a Google redirect | measured, §3 |
| Estimated budgets replacing measured ones | §4 |
| An unwired switch | this repo has shipped a toggle whose middle segment was missing, with zero errors; verify `ENABLE_EXTERNAL_CORROBORATION` and the new model id by reading the deployed function's env, not the template |
