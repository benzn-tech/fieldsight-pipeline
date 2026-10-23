# Gemini provider order on OpenRouter — design

Date: 2026-10-23 · Line: ASR+LLM优化v3 (Ask orchestration / web) · Status: rev 2 (Fable review applied, §9)
Supersedes the draft-hedge design (rev 1-2 of this file, same day): the measurement
it gated on found a routing cause, not a random tail (§2).

## 1. Problem

Every call `corroboration_client.call` makes goes to google/gemini-3.8-flash on
OpenRouter with no provider preference: the web chain's draft, verify and
compose, the Ask precheck, the history rewrite, and `corroboration.py`. After
#1177 (draft ceiling 12 s) TEST still lost about one web turn in four, and the
verify often ends "unverified" at ~11 s.

## 2. Evidence (owner-run benches, 2026-10-23/24, generic questions, no records)

| bench | arm | n | result |
|---|---|---|---|
| paired drafts, default routing | served by Google (Vertex) | 56 | p50 5.2, p90 9.75 s |
| | served by Google AI Studio | 24 | p50 14.1, p90 14.9 s, 24/24 > 13 s |
| | pair correlation | 40 pairs | 0.05 (routing is per request) |
| drafts, order [vertex], fallbacks on (two runs; first overwritten on disk) | | 20 + 20 | 11/40 still served by AI Studio, all 13.5-15 s — the same share as default routing, so this `order` did not visibly steer anything |
| verify (web), vertex-first / default (second run on disk) | | 6 + 6 | 5/6 vs 1/6 HTTP 504 "operation was aborted" at ~11 s, each `provider_name: Google, previous_errors: 429` (the default-arm one is the direct evidence that default routing tries Vertex first) |
| drafts, **only** AI Studio, no fallback | | 8 | 3.4-5.1 s, 8/8 ok |
| drafts, **only** Vertex, no fallback | | 8 | 6 at 2.9-5.6 s, 2 at 20.3 and 28.4 s |

Reading: AI Studio itself is fast. Vertex on OpenRouter is rate-limited (429);
OpenRouter then retries Vertex for ~10 s before falling back, so a call
"served by AI Studio" arrives at ~14 s, and a call with a ~11 s budget (verify)
is aborted before the fallback finishes. Same token counts on both providers
(median ~170-185 output, 0 reasoning), so the time is not generation.

The hedge (rev 2) simulated on the paired data: over-ceiling 30 % → 17.5 % at a
52 % extra-call rate. Dropped.

## 3. Decision

Send OpenRouter a provider preference on every gemini call from this client:

```
"provider": {"order": ["google-ai-studio", "google-vertex"], "allow_fallbacks": true}
```

- AI Studio first (measured fast); Vertex stays as the fallback, so an AI Studio
  outage degrades to today's behaviour instead of failing.
- Applied only when the model id starts with `google/`. A non-Google model gets
  no `provider` key (an order naming providers that cannot serve the model is
  meaningless and must not change routing).
- `CORROBORATION_PROVIDER_ORDER` (comma-separated slugs), read per call, default
  `google-ai-studio,google-vertex`. `off` (or empty) = no `provider` key.
  Wired as template Parameter `CorroborationProviderOrder` (AllowedPattern
  `^(off|[a-z0-9-]+(,[a-z0-9-]+)*)$`) into both functions that carry
  `CORROBORATION_API_KEY`, passed by both workflows only when
  `TEST_/PROD_CORROBORATION_PROVIDER_ORDER` is set. Rollback = set the repo
  variable to `off` and redeploy (a console edit alone is wiped by the next SAM
  deploy).
- Only the new variable is read per call; `DEFAULT_MODEL` / `CHEAP_MODEL` stay
  import-time.
- Lives in `corroboration_client.call`, so draft, verify, compose, precheck,
  rewrite and corroboration all get it. `llm_tools_client` (judge, DeepSeek) has
  its own request body and is not touched.

## 4. Observability

`_parse` keeps OpenRouter's top-level `provider` on `Reply.provider` (new slot;
the line is emitted inside `call()` only, since tests inject duck-typed replies
without it). The
LLM_USAGE line keeps `provider=openrouter` (existing queries filter on it); one
extra INFO line `corroboration: served_by=<provider> caller=<caller>
latency_ms=<n>` carries who served it. No prompt or answer text.

## 5. Tests (unit)

1. google model, default env → payload carries the order above, `allow_fallbacks`
   true.
2. non-google model → no `provider` key.
3. `CORROBORATION_PROVIDER_ORDER=""` → no `provider` key; a custom value →
   that order, whitespace trimmed, empty items dropped.
4. env read per call (monkeypatch after import takes effect).
5. `_parse` keeps `provider`; missing / non-string → None.
6. Existing client and web-answer suites pass unchanged.

## 6. Gates

**6.1 Offline, the shipped config** (owner-run, 40 drafts + 10 verifies with
exactly the §3 `provider` object). Pass: drafts over 12 s materially below the
30 % measured under default routing; served_by AI Studio on most rows; verifies
return sources.

**6.1 result (2026-10-24, owner-run, 50 calls): PASS for the draft.**
40/40 drafts served by Google AI Studio, p50 3.73, p90 4.17, max 4.5 s, 0 over
12 s (was 30 % under default routing). The 10 verifies (web plugin) were ALL
served by Google (Vertex) despite the order: 10/10 ok with sources, p50 8.5 s,
2 over 12 s (13.5, 32.7). So with the web plugin OpenRouter does not use AI
Studio; the verify is not fixed by this change and not made worse. Follow-up
(separate): bench the verify with `only: [google-ai-studio]` to learn whether AI
Studio can serve the plugin at all.

**6.2 TEST.** Deploy to TEST; run the routing eval's explicit-web turns (call count stated
first). Pass: served_by shows AI Studio on the draft/verify lines; no draft lost;
verify outcomes no worse than the #1177 check (2 verified / 1 unverified / 1
lost of 4). Then release alone to prod and read prod served_by + web outcomes on
the next real turns.

## 7. Risks

- AI Studio may rate-limit too at higher volume. By the same mechanism
  OpenRouter would retry AI Studio before falling back to a Vertex that may
  itself be 429ing — possibly worse than today. Unmeasured; read served_by and
  latency after a week, and `off` is one variable away.
- AI Studio safety blocks come back as finish_reason SAFETY with empty content,
  counted as "empty completion"; watch that rate after rollout.
- AI Studio's web-plugin verify was not benched separately (the plugin is
  OpenRouter's, provider-independent); the TEST gate covers it.

## 8. Out of scope

The draft hedge; DRAFT_BUDGET / VERIFY_FLOOR (#1177 stays); the judge client.

## 9. Review log

rev 2 — Fable: HIGH shipped config never benched (→ §6.1); HIGH vertex-first
`order` not shown to steer (→ §2 wording); HIGH env-only rollback is wiped by SAM
(→ template Parameter + workflow wiring, `off` sentinel); MEDIUM §2 figures
reconciled with the files on disk; MEDIUM AI Studio 429 risk restated; LOW
Reply slot, import-time models, INFO logging confirmed, SAFETY empties.
