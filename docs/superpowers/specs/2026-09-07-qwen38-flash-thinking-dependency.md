# qwen3.8-flash loses detail unless thinking is on

Measured 2026-09-07 against DashScope intl, five runs per configuration, before
merging PR #706 ("Qwen models move to qwen3.8-flash").

## What was measured

One sentence of real site transcript carrying an explicit date:

> "Jesse said the slab pour on level 3 is delayed to **Friday** because the pump
> broke. Mark to chase the supplier."

The model was asked for `{"items":[{"action","responsible","due"}]}`. The score is
how many of five runs put Friday in `due`.

| model | thinking | `response_format` | due captured |
|---|---|---|---|
| `qwen3.7-max`   | off | json_object | **4/5** |
| `qwen3.6-flash` | off | json_object | **4/5** |
| `qwen3.8-flash` | off | json_object | **0/5** |
| `qwen3.8-flash` | off | *free text* | **0/5** |
| `qwen3.8-flash` | **on** | free text | **4/5** |

`responsible` was 5/5 in every configuration. Only the date moves.

**The fourth row exists because the first draft of this document did not have it.**
`_call_qwen` binds the two knobs together — thinking-on skips `response_format`,
thinking-off sends `json_object` (`src/llm_utils.py:225-241`) — so the natural
experiment changes two variables at once and cannot say which one matters. Run
free-text with thinking off and the answer is unambiguous: `response_format` is
not involved.

## What it is not

**Not a wrong model id.** All three ids return HTTP 200 and echo `model=` with the
same name — no silent fallback to another model, which was the failure shape this
repo was watching for.

**Not a flash-class weakness.** `qwen3.6-flash` scores the same as `qwen3.7-max`.
The first reading of this data was "flash is weaker than max, so keep the plus path
on max"; the 3.6-flash row inverts it. The variable is thinking, not the tier.

## Exposure map

`QwenModelPlus` / `QwenModelFast` are template parameters, but thinking is set per
function and sometimes per call. Seven functions read `QWEN_MODEL`; only five
declare `QWEN_ENABLE_THINKING`, and the other two fall to the code default of
`false` (`src/llm_utils.py:72`).

| caller | model param | thinking | JSON schema with a date? |
|---|---|---|---|
| report-generator (`template.yaml:1353`) | Plus | true | safe |
| meeting-minutes (`:1469`) | Plus | true | safe |
| extract-session **final** (`:2141`) | Plus | `True` per call | safe |
| extract-session **live** | Plus | **`False`** (`lambda_extract_session.py:1741`) | **exposed** |
| rolling-summary (`:2263`) | Plus | **false** | **exposed** (`lambda_rolling_summary.py:48,94`) |
| session-finalize (`:2638`) | Plus | **false** | **exposed** — re-summary path `lambda_session_finalize.py:181` → `rolling_summary.py:110`; and `open_points.attach_resolutions` passes `enable_thinking=False, force_json=True` explicitly (`open_points.py:309`) even on the otherwise-safe brief path |
| matcher (`:3320`) | Plus | **unset → false** | **exposed** (`lambda_programme_matcher.py:560,653`) |
| ask-agent (`:1607`) | Fast | unset → false | **different exposure — see below** |

Ask-agent is non-thinking and so runs 3.8-flash in the degraded configuration, but
it calls with `force_json=False` and returns free-text answers
(`lambda_ask_agent.py:489,1059,1081`). It has no `due` field to drop. What this
probe says about it is nothing directly; what it suggests is that the same loss of
detail would show up as thinner answers, **which has not been measured**.

Those callers run non-thinking deliberately, for latency: omitting `enable_thinking`
defaults it ON at DashScope and costs roughly 10x (finalize measured 54s -> 7.6s
when it was forced off). So "turn thinking on everywhere" is not available as a fix.

## How far this evidence reaches

**One sentence, one field, n=5.** 0/5 vs 4/5 is Fisher-exact p ~ 0.048 — real but
borderline, on a single date word, a single prompt shape, a single schema. What is
established is narrow:

> On this probe, `qwen3.8-flash` with thinking off dropped the date in 5 of 5 runs,
> in both output modes, while `qwen3.7-max` and `qwen3.6-flash` kept it in 4 of 5.

The general rule — "`qwen3.8-flash` must not be paired with `enable_thinking=false`"
— is the natural reading, but it is an extrapolation from one field to seven callers
with different prompts and schemas. Before relying on it, measure a second field and
a second schema. Do not quote the general rule as if it were the measurement.

## Consequence for PR #706

The PR changes both template parameter defaults **and** the `QWEN_MODEL` code
default in `src/llm_utils.py`, so it reaches all seven functions plus anything
relying on the code default. Five of the eight call sites above run non-thinking.

The narrow reading — "3.8-flash is bad" — is wrong; it is fine wherever thinking is
on. The trap is structural:

> The model is a stack parameter. Thinking is a function env var, or a call
> argument. **Nothing anywhere expresses that certain values of one are unsafe
> with certain values of the other**, so no test can go red.

Any fix that leaves those two independent leaves this for the next model bump.
