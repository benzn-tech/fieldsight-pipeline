# Plan — external corroboration without Anthropic

Spec: `docs/superpowers/specs/2026-09-08-corroboration-off-anthropic-design.md`
(revised after adversarial review; the review's findings are folded in as §3.5,
the §4 correction, and scope items 5–8).

Six steps, each shippable on its own and each ending in a state where the switch
is still `false` on both stacks. The feature stays off for the whole plan; the
last step is a live probe on TEST with it on, and turning it on for real remains
the owner's commercial call.

## Step 0 — make the fabrication survivable BEFORE the vendor changes

**This ships first and independently, against Anthropic.** It is the one defect
that is live today: `Reply.searched` is written and never read, and
`corroboration.py:300` logs a search-tool error then reconciles on the text
anyway. A vendor that answers without searching gets a "Confirmed" chip with no
sources under it.

Doing it first means the guard is in place and tested *before* Gemini arrives,
rather than being written in the same change as the thing that needs guarding.

- `corroborate()` reads `search.searched`; when false it returns
  `{corroborations: [], searched: false, timed_out: false}` and never calls
  `_reconcile`.
- `searched` is added to the response body. Existing consumers ignore unknown
  keys, so this is additive.
- Tests, driven not grepped: a recorded reply with prose and zero web results
  yields zero corroborations, `searched: false`, and `timed_out: false` — and
  the reconcile step is asserted **not** to have been called.

Verification that it can fail: revert the `searched` read and the new test must
go red.

## Step 1 — the UI learns the third word

`_pending` / `_failed` / `timed_out` exist. "We did not search" does not, and
routing it through `timed_out` prints *"The check ran out of time"* about a
request that finished in four seconds.

- A distinct branch for `searched === false`, muted like `_failed`, never
  `not_found` (which asserts a search that did not happen).
- Fix the neighbouring hole while here: an all-empty body currently renders
  `null`, so a UI-on/backend-off misconfiguration looks like working-and-empty —
  the shape this repo has shipped three times. Render the muted line instead.
- Copy goes to the owner before merge. Suggested: **"Couldn't check the web
  for this answer."**

Frontend repo, own PR, base `dev`. Independent of every backend step: old
backend simply never sets the flag.

## Step 2 — the credential and the model ids

No behaviour change; wiring only, so it can be verified by reading a deployed
function rather than by running the feature.

- New CFN parameter `CorroborationApiKey`, env `CORROBORATION_API_KEY` on
  `AskAgentFunction`, sourced from the `OPENROUTER_API_KEY` secret in **both**
  workflow files.
- **Not** `QWEN_API_KEY`: on that function it is
  `!If [UsesSeparateChatVendor, QwenChatApiKey, DashScopeApiKey]` and resolves
  to the wrong vendor's key on a DashScope-pointed stack.
- `CORROBORATION_MODEL` and `CORROBORATION_CHEAP_MODEL` defaults become
  OpenRouter ids. `claude-haiku-4-5` is not a valid OpenRouter id, and extract
  and reconcile share the client — left alone, every extract fails and the user
  is told `timed_out` on 100% of requests.
- Both workflow files are a known rebase hotspot; land this when nothing else
  is touching them, and re-fetch immediately before merging.

Verify by reading the deployed function's env, not the template. A toggle whose
middle segment is missing has shipped here before with zero errors.

## Step 3 — the client

`corroboration_client.py` posts to OpenRouter with
`plugins: [{"id": "web"}]` — the plugin form, not the `:online` suffix, which
was over budget on 3 of 3 runs.

- Sources come from `annotations[].url_citation`; `searched` is true only when
  at least one is present.
- The Anthropic request and response handling is **deleted**, not left behind a
  flag. Two vendors in one client is how the wrong one gets called.
- The `HTTP 200 + empty content` guard covers the plugin-missing case, which
  returns `completion_tokens: 0` and no content rather than an error.
- `tests/unit/test_corroboration_client.py` is rewritten; it currently contains
  a test asserting the client *is* Anthropic on every stack.

## Step 4 — sources carry their own domain

Gemini returns Google grounding redirects, so parsing the URL for a host makes
every source read `vertexaisearch.cloud.google.com`.

- `_sources()` emits `domain`, from the annotation's `title` (which carries the
  host) or from the URL when a vendor returns a real one.
- UI prefers `domain`, falls back to `sourceHost(url)`. Backward compatible in
  both deploy orders.
- The link href stays the redirect — it works, and inventing a URL we did not
  get would be worse. The tooltip becomes the domain rather than `title`, which
  now duplicates it.
- `published` will be absent on this vendor. Record it: the 2026-08-31 design
  names retrieval date and source domain as the two trust carriers, and one is
  going away. Do not synthesise a date.

## Step 5 — the budget, from measurement

Measured on Gemini, n=3: extract 6.10/7.60/6.24, search 10.3/11.4/11.3,
reconcile 2.52/4.07/4.40. Worst case 23.4s against today's 24s hard stop.

- `HARD_STOP_SECONDS` 24 → **27**; extract 8 + search 13 + reconcile 5 = 26.
  27 stays under API Gateway's 29s, so a missed deadline is still a shaped
  `timed_out` body instead of a raw gateway error.
- Do **not** touch the UI's 35s client timeout. It is deliberately past the
  gateway so the gateway's own 504 arrives instead of a client-side abort.
- Extract writes 618–796 tokens for three entities. Shorten what is asked for,
  then re-measure. Do not cap `max_tokens`: 300 truncated the JSON on 3 of 3
  runs, turning a slow step into a broken one.
- The existing test asserts only that the budgets sum under `HARD_STOP`, which
  the wrong estimates also satisfied. Add a test that pins each budget against
  the measured worst case, with the measurements in the docstring so the next
  person can see what they are allowed to change and why.

## Step 6 — prove it searched, on TEST, before anyone believes it

A green unit suite over a vendor swap says nothing about whether the vendor
searched — that is precisely what muse-spark demonstrated.

Set `TEST_ENABLE_EXTERNAL_CORROBORATION=true`, deploy, and record in the PR:
n ≥ 3 real questions, asserting sources present, domains are real hosts (not
the redirect host), elapsed inside the new budget, and at least one entity
reaching a non-`not_found` state. Then decide with the owner whether it stays
on for TEST.

**TEST holds real pilot recordings**, and this is the one path that sends
anything derived from a customer's meeting to a third party. Step 6 is the
first moment that actually happens, and it needs the owner's explicit yes on
the day — not this plan's.

## Ordering and conflicts

Steps 0 and 1 are safe to land at any time and improve today's behaviour on
their own. Steps 2–5 must land in order; 2 and 5 both touch the workflow files
and `template.yaml`, which other sessions are editing tonight, so each re-checks
`origin/develop` immediately before merging.

Nothing in steps 0–5 changes what any user sees while
`ENABLE_EXTERNAL_CORROBORATION=false`, which it is on both stacks throughout.

## Out of scope, deliberately

- **Removing `ANTHROPIC_API_KEY` from `AskAgentFunction`.** `llm_utils`
  reads it whenever `LLM_PROVIDER=anthropic`, and `deploy-prod.yml` falls back
  to exactly that when the repo variable is unset. Removing the key makes an
  invisible variable load-bearing for every prod Ask answer. Change the
  fallback first, in its own change.
- **Streaming.** The reason it was raised — telling the user we are searching
  without owing them an immediate answer — is already satisfied: the second pass
  is a separate request and the pending state already says "Checking the web…".
  Streaming is worth doing for the *first* answer and belongs in its own
  project.
- The gate's policy. `MAX_ENTITIES = 3` and the person/site exclusions are
  privacy decisions from the 2026-08-31 design.
