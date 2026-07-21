# Deadline Normalization & Clean Calendar Fields — Design

**Date:** 2026-07-21
**Repos:** `fieldsight-pipeline` (backend), `fieldsight-ui` (frontend)
**Status:** Approved design, ready for implementation plan

## Problem

Action-item deadlines reach the UI as free text (`"Tomorrow 08:00"`, `"By Friday"`,
`"EOD"`, `"ASAP"`), not as clean calendar dates. The new inline edit feature
(`fs.DateField`) assumes an ISO `YYYY-MM-DD` value; feeding it free text crashes the
whole app. We need every `create date` / `due` the UI touches to be a clean calendar
field, plus a backfill so existing stored rows match the new contract.

### Why it happens (verified, not assumed)

The ISO filter already exists — the problem is elsewhere:

- **A strict ISO gate is already in place.** `src/lambda_ingest.py:105`
  `_ISO_DATE_RE = ^\d{4}-\d{2}-\d{2}$`; `_map_action_items()`
  (`src/lambda_ingest.py:207-229`) drops any non-ISO value to `NULL` in the typed
  `deadline` column while preserving the raw string in `deadline_text`.
  `src/lambda_item_writer.py:268` imports and reuses this same function — one filter,
  two paths.
- **Upstream prompts actively manufacture dirty values.**
  `src/lambda_report_generator.py:183` instructs the model to emit
  `'Tomorrow 08:00'`, `'EOD'`; `src/lambda_meeting_minutes.py:113` mixes ISO and
  `ASAP` in one field's examples; `src/lambda_extract_session.py:153` leaves
  `deadline` unconstrained. None of the three tell the model today's date, so the
  model can only produce relative words.
- **The read path prefers the dirty value.** `src/lambda_org_api.py:1709` serves
  `deadline_text or str(deadline)` — free text wins over a perfectly good typed date.
  Confirmed by `tests/unit/test_lambda_org_api.py:2309-2317`: a row with both
  `deadline_text="Tomorrow 8am"` and `deadline="2026-07-15"` renders as the text.
- **The KPI silently undercounts.** `src/repositories/rollup.py:103`
  `overdue_actions` filters on `deadline IS NOT NULL`, so every non-ISO deadline is
  invisible to the overdue count.
- **The UI crash is a truthy-Invalid-Date bug.** `scripts/pages/tasks.js:890` feeds
  raw `row.deadline` into `DateField`; `scripts/composites/date-picker.js:66`
  `parseISO` returns an **Invalid Date object (truthy)**, so the `|| new Date()`
  fallback at `:303` never fires → `:225` `d.toISOString()` throws
  `RangeError: Invalid time value`. There is no error boundary in `scripts/`, so the
  whole React tree unmounts (blank app). Read-only render is safe because it goes
  through `resolveDeadline`.

### Corrections to the original framing

- `fieldsight-pipeline` `develop` is **75 commits behind** `main`, leads by only 2
  docs-only commits — no date work lives there. The date work is on `main`.
- `fieldsight-ui` has **no `develop` branch**; the integration branch is `dev`, where
  the `DateField` edit feature landed.
- `"approx."` was **not found** in any date context in fixtures or source. Attested
  dirty values: `Tomorrow 8am`, `By Friday`, `EOD`, `EOW`, `ASAP`, `Next week`.
  Treat `approx.` as an unmodeled class — a dry-run over real production data
  precedes any backfill assumption.

## Design decisions (approved)

1. **Backfill aggressiveness: aggressive.** Anchor on `topics.report_date` (a
   `date NOT NULL` column, `src/migrations/0003_dashboard_readmodel.sql:6`).
   Deterministic relative words resolve by rule; fuzzy values also get a date
   (`ASAP`/`EOD` → report_date; `approx. <month>` → last day of that month;
   `Next week` → report_date + 7). Unclassifiable → `NULL`.
2. **`deadline_text` column: keep, stop reading and stop writing to the read path.**
   Not dropped. Because it is retained, every computed value is traceable and the
   backfill is re-runnable — aggressive inference is therefore not an irreversible bet.

**Known consequence to flag to stakeholders:** resolving `ASAP → report_date` flips
historical ASAP items to overdue, so `overdue_actions` (`rollup.py:103`) will jump.
Semantically correct, but a visible KPI shift — announce before running the backfill.

## Components

### A. `src/deadline_normalizer.py` — pure normalization function

Single responsibility, the system's **only** date-parsing point.

```
normalize_deadline(text: str | None, report_date: date) -> date | None
```

Table-driven, zero I/O, zero network, never raises. Three layers:

1. **ISO passthrough** — `^\d{4}-\d{2}-\d{2}$` → parse and return.
2. **Deterministic relative** — `Today`, `Tomorrow`, `By <weekday>`, `EOW`,
   `Next week`, weekday names. Resolved against `report_date`.
3. **Aggressive inference** — `ASAP`/`EOD` → `report_date`;
   `approx. <month>` / bare `<month>` → last day of that month (year inferred
   forward from `report_date`); other bounded guesses as the rule table grows.

Anything unmatched → `None`. Case-insensitive, tolerant of trailing time-of-day
(`"Tomorrow 08:00"` strips the `08:00`).

Fully unit-tested against a fixture table of every attested dirty value plus `None`
and already-ISO inputs.

### B. Wire into extraction (one edit, both paths)

`src/lambda_ingest.py:207` `_map_action_items()`: replace the inline `_ISO_DATE_RE`
check with a call to `deadline_normalizer.normalize_deadline(raw, report_date)`.
Because `src/lambda_item_writer.py:268` reuses this function, the real-time
extraction path and the nightly report path both get the new behavior from one edit.

`deadline_text` continues to be written verbatim (write-through preserved for
traceability; only the *read* path stops consuming it — see D).

### C. Constrain the prompts to ISO-only

Three prompts — `src/lambda_report_generator.py:183`,
`src/lambda_meeting_minutes.py:113`, `src/lambda_extract_session.py:153`:

- **Inject the report date** into the prompt context.
- Require `deadline` to be `YYYY-MM-DD` or `null` — remove the `'Tomorrow 08:00'` /
  `EOD` / `ASAP` example values that currently *teach* dirty output.

Layer B remains as the safety net; we do not assume the model complies 100%.

### D. Flip the read path + fix PATCH

- `src/lambda_org_api.py:1709`: `deadline_text or str(deadline)` → serve the typed
  `deadline` only (`YYYY-MM-DD` or `null`). The UI now only ever receives a clean
  calendar field or null.
- `src/lambda_org_api.py:1126-1127`: the PATCH endpoint currently overwrites
  `deadline_text` with the ISO value, destroying the original wording. Change it to
  write the `deadline` column only, leaving `deadline_text` untouched.

The PATCH fix is what makes the backfill able to reuse the non-VPC pattern (E)
without a separate in-VPC DB writer.

Update `tests/unit/test_lambda_org_api.py:2309-2317` to assert the typed date now
wins.

### E. `src/backfill_deadlines.py` — existing-data backfill

Mirrors the house pattern in `src/backfill_site_coords.py`:

- Pure planner `plan_deadline_backfill(rows, ...)` — fully unit-tested, no I/O,
  calls `deadline_normalizer.normalize_deadline` per row.
- Thin `run_backfill(...)` with I/O edges injected
  (`fetch_rows_fn` / `persist_fn`).
- **Deployed non-VPC**, persisting via `PATCH /api/org/action-items/{id}` (org-api),
  avoiding the BUG-36 in-VPC-vs-outbound trap. Enabled by the D PATCH fix.

Scope: rows where `deadline IS NULL AND deadline_text IS NOT NULL`. Anchor each row
on its `topics.report_date`.

**Dry-run first (mandatory):** produce a report — per-rule hit counts, count of rows
that stay `NULL`, and the top-N unclassified raw strings by frequency — for human
review before any write. This is also where we learn whether `approx.` actually
exists in production data and how large each dirty class is.

### F. UI hardening (frontend, ship first, independent of backend)

Even after D cleans the API, the truthy-Invalid-Date white-screen must not be
guarded only by upstream cleanliness. In `fieldsight-ui`:

- `scripts/composites/date-picker.js:66` `parseISO` → return `null` when
  `isNaN(d.getTime())`, so the existing `|| new Date()` fallbacks work.
- `scripts/composites/date-field.js` — validate `^\d{4}-\d{2}-\d{2}$` before
  assigning `value` / `anchorDate`; fall back to today (or empty) otherwise.

This localizes any future bad value to the widget instead of unmounting the app.

## Sequencing

1. **F** (UI hardening) — smallest change, kills the white-screen immediately,
   independent of backend deploy.
2. **A + B** — normalizer + extraction wiring; new writes land clean.
3. **C** — prompts stop manufacturing dirty values at the source.
4. **D** — flip read path + PATCH fix; UI receives clean fields.
5. **E** — dry-run, human review, then backfill existing rows.

## Testing

- **A:** unit table over every attested dirty value + ISO + `None`, asserting exact
  resolved dates against a fixed `report_date`.
- **B:** `_map_action_items` unit test — dirty in, ISO-or-null out; `deadline_text`
  still written.
- **C:** prompt-snapshot / contract tests asserting report date present and no dirty
  example strings remain.
- **D:** update `test_lambda_org_api.py:2309-2317` (typed date wins); PATCH test
  asserts `deadline_text` untouched.
- **E:** planner unit tests (pure); dry-run report reviewed before the write step.
- **F:** `parseISO` returns `null` on invalid; `DateField` never passes non-ISO to
  `DatePicker` (guards the `RangeError` path).

## Out of scope

- Dropping the `deadline_text` column (deferred to a later iteration once the backfill
  is proven stable).
- LLM second-pass inference for unclassifiable values (aggressive rule table is the
  agreed ceiling; unmatched stays `NULL` for human edit).
- Time-of-day precision — deadlines are calendar dates only.

---

# 中文版（供快速过审）

## 问题

action-item 的 deadline 到 UI 时是自由文本（`"Tomorrow 08:00"`、`"By Friday"`、`"EOD"`、`"ASAP"`），不是干净的日历日期。新加的行内编辑（`fs.DateField`）假设值是 ISO `YYYY-MM-DD`，喂自由文本进去会**白屏整个 app**。目标：UI 碰到的每个 create date / due 都是干净日历字段；再补一次回填，让存量数据也对齐新契约。

### 根因（已核实，非猜测）

ISO 过滤器早就存在——问题不在这：

- **严格 ISO 门槛已就位**：`lambda_ingest.py:105` `^\d{4}-\d{2}-\d{2}$`；`_map_action_items()`（`:207-229`）把非 ISO 值落 NULL 到 typed `deadline` 列，原文留在 `deadline_text`。`lambda_item_writer.py:268` 复用同一函数——一个过滤器，两条路径。
- **上游 prompt 主动生产脏值**：`lambda_report_generator.py:183` 明示模型输出 `'Tomorrow 08:00'`、`'EOD'`；`lambda_meeting_minutes.py:113` 把 ISO 和 `ASAP` 混在同一字段示例里；`lambda_extract_session.py:153` 不约束 `deadline`。三处都没告诉模型今天几号，所以它只能吐相对词。
- **读路径偏好脏值**：`lambda_org_api.py:1709` 发的是 `deadline_text or str(deadline)`——自由文本赢过好好的 typed 日期。测试 `test_lambda_org_api.py:2309-2317` 坐实：一行同时有 `deadline_text="Tomorrow 8am"` 和 `deadline="2026-07-15"`，吐出来的是文本。
- **KPI 静默少算**：`rollup.py:103` `overdue_actions` 靠 `deadline IS NOT NULL`，所有非 ISO deadline 对逾期计数不可见。
- **UI 崩溃是 truthy-Invalid-Date bug**：`tasks.js:890` 把原始 `row.deadline` 喂给 `DateField`；`date-picker.js:66` `parseISO` 返回 **Invalid Date 对象（truthy）**，所以 `:303` 的 `|| new Date()` 兜底失效 → `:225` `d.toISOString()` 抛 `RangeError`。`scripts/` 里无 error boundary，整棵 React 树 unmount（白屏）。只读渲染安全（走 `resolveDeadline`）。

### 对原提法的更正

- pipeline `develop` 比 `main` **落后 75 提交**，只领先 2 个纯文档提交——日期活不在这，在 `main`。
- ui **没有 `develop` 分支**；集成分支是 `dev`，`DateField` 编辑功能落在这。
- `"approx."` 在 fixture / 源码的日期上下文里**查无实据**。有据可查的脏值：`Tomorrow 8am`、`By Friday`、`EOD`、`EOW`、`ASAP`、`Next week`。把 `approx.` 当未建模类——回填前先对真实生产数据跑 dry-run，不照夹具猜。

## 已定决策

1. **回填力度：激进**。锚点用 `topics.report_date`（`date NOT NULL` 列，`0003_dashboard_readmodel.sql:6`）。确定性相对词按规则解析；模糊值也给日期（`ASAP`/`EOD` → report_date；`approx. <月>` → 该月最后一天；`Next week` → report_date + 7）。无法归类 → `NULL`。
2. **`deadline_text` 列：保留，停读停写读路径**。不删。因为保留，每个推算值可回溯、回填可重跑——激进推算因此不是不可逆赌注。

**要向相关人打招呼的已知后果**：`ASAP → report_date` 会让历史 ASAP 条目翻成逾期，`rollup.py:103` 的 `overdue_actions` 会跳一截。语义正确，但 KPI 会明显变动——回填前先通知。

## 各组件

### A. `src/deadline_normalizer.py` —— 纯规范化函数

单一职责，全系统**唯一**日期解析点。`normalize_deadline(text, report_date) -> date | None`。表驱动、零 I/O、零网络、绝不抛异常。三层：ISO 直通 → 确定性相对词（Today/Tomorrow/By 周几/EOW/Next week）→ 激进推测（ASAP/EOD→当天；`approx. <月>`/裸月份→该月最后一天）。不匹配 → `None`。大小写不敏感，容忍尾部时间（`"Tomorrow 08:00"` 剥掉 `08:00`）。对全部脏值 + `None` + 已 ISO 输入做全单测。

### B. 接进抽取路径（改一处，两路径同时生效）

`lambda_ingest.py:207` `_map_action_items()`：把内联 `_ISO_DATE_RE` 换成调 A。因 `lambda_item_writer.py:268` 复用此函数，实时抽取 + 夜间报告两条路径一改全生效。`deadline_text` 照常写入（写留读停，供回溯）。

### C. Prompt 收紧成只准 ISO

三处（`lambda_report_generator.py:183`、`lambda_meeting_minutes.py:113`、`lambda_extract_session.py:153`）：**注入报告日期**，要求 `deadline` 只输出 `YYYY-MM-DD` 或 `null`，删掉现在那些教模型吐脏值的示例。B 层仍作兜底，不指望模型 100% 听话。

### D. 读路径翻转 + 修 PATCH

- `lambda_org_api.py:1709`：`deadline_text or str(deadline)` → 只发 typed `deadline`（ISO 或 null）。UI 从此只收干净字段或 null。
- `lambda_org_api.py:1126-1127`：PATCH 现在会把 ISO 覆写进 `deadline_text`、销毁原文。改成只写 `deadline` 列、不碰 `deadline_text`。这一修让回填能复用非 VPC 模式（E），不必再写 in-VPC 直连写入器。
- 同步更新 `test_lambda_org_api.py:2309-2317`，断言 typed 日期取胜。

### E. `src/backfill_deadlines.py` —— 存量回填

照抄 `backfill_site_coords.py` 的 house pattern：纯 planner `plan_deadline_backfill(rows, ...)`（全单测、无 I/O，逐行调 normalizer）+ 薄 runner（I/O 边界注入）+ **部署非 VPC**，经 `PATCH /api/org/action-items/{id}` 回写，避 BUG-36。范围：`deadline IS NULL AND deadline_text IS NOT NULL`，每行锚 `topics.report_date`。**强制先 dry-run**：出报告——各规则命中数、留 NULL 的行数、未归类原文按频次 top-N——人工核完再写。也在这一步确认 `approx.` 生产里到底存不存在、各脏类占比。

### F. UI 兜底（前端，独立先上）

即使 D 清干净了 API，truthy-Invalid-Date 白屏也不能只靠上游干净兜。ui 里：`date-picker.js:66` `parseISO` 在 `isNaN(getTime())` 时返回 `null`；`date-field.js` 赋值前校验 `^\d{4}-\d{2}-\d{2}$`，否则回落今天/空。把未来任何坏值锁在控件内，不再 unmount 整个 app。

## 上线顺序

1. **F**（UI 兜底）——改动最小、立刻消白屏、不依赖后端部署。
2. **A + B**——normalizer + 抽取接线；新写入即干净。
3. **C**——prompt 从源头停止生产脏值。
4. **D**——读路径翻转 + 修 PATCH；UI 收到干净字段。
5. **E**——dry-run、人工核、再回填存量。

## 测试

- **A**：对每个脏值 + ISO + `None` 做单测表，固定 `report_date` 断言精确日期。
- **B**：`_map_action_items` 单测——脏进、ISO-或-null 出，`deadline_text` 仍写。
- **C**：prompt 快照/契约测试——断言报告日期在场、无脏示例残留。
- **D**：更新 `test_lambda_org_api.py:2309-2317`（typed 取胜）；PATCH 测试断言 `deadline_text` 未动。
- **E**：planner 纯单测；dry-run 报告在写入前人工核。
- **F**：`parseISO` 非法返回 `null`；`DateField` 绝不把非 ISO 传给 `DatePicker`。

## 不在范围

- 删 `deadline_text` 列（等回填稳定后另迭代）。
- 对无法归类值做 LLM 二次推断（激进规则表即上限；不匹配留 `NULL` 供人工编辑）。
- 时刻精度——deadline 只到日历日。
