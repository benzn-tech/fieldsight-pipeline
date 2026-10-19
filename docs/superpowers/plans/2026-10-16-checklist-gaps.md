# Checklist Gaps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A checklist report made from a spoken check tells the inspector which items were not covered and which failed, on the Timeline day card and in the bell.

**Architecture:** `checklist.rebuild` records the gaps next to the rows it rebuilds; they already travel into the worker's result JSON as `meta["checklists"]`. org-api sums them per report in a failure-proof helper and adds `gaps` to the checklist-report payload. The UI adds a summary line (`gapWords`) and an expandable list.

**Tech Stack:** Python 3.12 (pytest), vanilla JS + React.createElement (node:test), CSS tokens.

**Spec:** `docs/superpowers/specs/2026-10-16-checklist-gaps-design.md`

## Global Constraints

- Additive only: no migration, no flag, no new S3 key, no new IAM.
- Gap entry keys: `n` (1-based number, ordering only) and `text` (customer wording) -- never `item` (taken by `report["dropped"]`).
- `gaps` is `null` unless the status is `done` AND at least one checklist in the result carries a `gaps` key. Null renders nothing; it never renders "All items covered".
- Failure to read or sum gaps must never change `status` or raise.
- UI copy is English. Both light and dark themes must read.
- Backend branch `feat/checklist-gaps` from `origin/develop`; frontend branch `feat/checklist-gaps` from `origin/dev`. Never `git stash`, never `git add -A`.
- Run one pytest process at a time.

## Review Focus

1. A `checklists: {}` result (template matched on `kind` but its section had no items) -> `gaps` null, not "All items covered". Pinned in Task 2.
2. A result written before this change (no `gaps` keys) -> `gaps` null, UI silent. Pinned in Task 2 and Task 3.
3. Malformed `checklists` (a list, a string, a section that is not a dict) -> status still `done`, `gaps` null, no 500. Pinned in Task 2.
4. A `No` row whose evidence was not in the transcript is dropped -> it is `not_covered`, not `failed` (the form shows it blank). Pinned in Task 1.
5. Item text containing `|` (the table rewrites it to `/`) -> gap `text` is the template's original wording, unchanged. Pinned in Task 1.

---

### Task 1: `checklist.rebuild` records the gaps

**Files:**
- Modify: `src/checklist.py` (`rebuild`, around lines 128-169)
- Test: `tests/unit/test_a_checklist_is_answered_only_from_the_recording.py` (append)

**Interfaces:**
- Produces: `rebuild(...)` returns `report` with a new key
  `report["gaps"] = {"not_covered": [{"n": int, "text": str}], "failed": [{"n": int, "text": str, "comment": str, "responsible": str, "due": str}]}`.
  `apply(...)` returns the same reports (unchanged signature).

- [ ] **Step 1: Write the failing tests** (append to the test file; it already defines `model_section(rows)` -- read lines 20-45 first and reuse its exact row format and the module's transcript fixture variable)

```python
ITEMS_G = ["Formwork braced | plumb", "Edge protection installed",
           "Cover to steel checked", "Pour booked", "Hand rails on stair"]
TRANSCRIPT_G = ("formwork is braced and plumb all good. "
                "edge protection is not installed on the north side, Mike will fix it by friday. "
                "stair rails not applicable today")

def gaps_rows():
    # Item 1 Yes with evidence; item 2 No with evidence; item 3 No WITHOUT
    # evidence (dropped); item 4 not mentioned; item 5 N/A with evidence.
    return [
        "1 | Yes | | | | formwork is braced and plumb",
        "2 | No | rail missing north side | Mike | friday | edge protection is not installed on the north side",
        "3 | No | | | | cover was never measured at all",
        "5 | N/A | | | | stair rails not applicable today",
    ]

def test_THE_gaps_are_the_blank_items_and_the_no_answers():
    sec = model_section(gaps_rows())
    _, _, report = checklist.rebuild(sec, ITEMS_G, TRANSCRIPT_G)
    gaps = report["gaps"]
    assert [g["n"] for g in gaps["not_covered"]] == [3, 4]
    assert [g["text"] for g in gaps["not_covered"]] == ["Cover to steel checked", "Pour booked"]
    assert len(gaps["failed"]) == 1
    f = gaps["failed"][0]
    assert f["n"] == 2 and f["text"] == "Edge protection installed"
    assert f["comment"] == "rail missing north side" and f["responsible"] == "Mike"
    assert f["due"] == "friday"

def test_a_gap_keeps_the_customers_wording_even_with_a_pipe():
    _, _, report = checklist.rebuild(model_section([]), ITEMS_G, TRANSCRIPT_G)
    assert report["gaps"]["not_covered"][0]["text"] == "Formwork braced | plumb"
    assert len(report["gaps"]["not_covered"]) == 5 and report["gaps"]["failed"] == []

def test_a_checklist_the_model_left_out_has_every_item_not_covered():
    tpl = {"sections": [{"title": "Pre-pour", "kind": "checklist", "items": ITEMS_G}],
           "catch_all": {"title": "Other"}}
    prose = [{"title": "Other", "paragraphs": [], "line_refs": []}]
    reports = checklist.apply(prose, tpl, TRANSCRIPT_G)
    assert [g["n"] for g in reports["Pre-pour"]["gaps"]["not_covered"]] == [1, 2, 3, 4, 5]
```

If `model_section` takes a different row shape, adapt only the row strings, keeping the six columns `Item no | Answer | Comment | Responsible | Due | Evidence`.

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/unit/test_a_checklist_is_answered_only_from_the_recording.py -k "gap or left_out_has" -v`
Expected: FAIL with `KeyError: 'gaps'`.

- [ ] **Step 3: Implement** -- in `rebuild`, initialise the gaps beside `report` and fill them in the existing final loop:

```python
    report = {"items": len(items), "answered": 0, "dropped": [], "evidence": {},
              # What the inspector did not do (owner, 2026-10-16): items the
              # recording never addressed, and items answered No. Built from
              # the same `answered` map as the rows below. `text`, not `item`:
              # `dropped` already uses `item` for the number.
              "gaps": {"not_covered": [], "failed": []}}
```

and in the `for i, item in enumerate(items, start=1):` loop:

```python
        a = answered.get(i)
        if a:
            ans, comment, responsible, due, refs = a
            row = [item, ans, comment, responsible, due]
            if ans == "No":
                report["gaps"]["failed"].append({"n": i, "text": item, "comment": comment or "",
                                                 "responsible": responsible or "", "due": due or ""})
        else:
            refs, row = [], [item, "", NOT_COVERED, "", ""]
            report["gaps"]["not_covered"].append({"n": i, "text": item})
```

(Keep the existing `report = {...}` line's other keys exactly; only add `"gaps"`.)

- [ ] **Step 4: Run the whole file**

Run: `python -m pytest tests/unit/test_a_checklist_is_answered_only_from_the_recording.py -q`
Expected: all pass.

- [ ] **Step 5: Revert check** -- temporarily delete the two `report["gaps"][...]append` lines, rerun Step 2's command, confirm FAIL, restore.

- [ ] **Step 6: Commit**

```bash
git add src/checklist.py tests/unit/test_a_checklist_is_answered_only_from_the_recording.py
git commit -m "checklist: record which items were not covered and which failed"
```

---

### Task 2: org-api adds `gaps` to the checklist-report payload

**Files:**
- Modify: `src/lambda_org_api.py` (`_checklist_report_status` ~4011, `_checklist_report_payload` ~4025; add `_checklist_gaps` just above them)
- Modify: `tests/unit/test_a_spoken_check_makes_its_own_report.py:114` (stub returns a 3-tuple)
- Create: `tests/unit/test_a_checklist_report_names_its_gaps.py`

**Interfaces:**
- Consumes: result JSON with top-level `checklists: {title: {..., "gaps": {"not_covered": [...], "failed": [...]}}}` (Task 1).
- Produces: `_checklist_gaps(result) -> dict | None`; `_checklist_report_status(result_key) -> (status, err, gaps)`; payload key `gaps = {"notCovered": [{"n","text","checklist"}], "failed": [{"n","text","comment","responsible","due","checklist"}]} | None`.

- [ ] **Step 1: Write the failing tests**

```python
"""A checklist report names what was not done (owner, 2026-10-16).

THE test runs the real status and summing code over a real result body
through a fake s3() -- not a stubbed status function, which would leave the
new code a seam nobody executes."""
import io
import json

import pytest

org = pytest.importorskip("lambda_org_api")

KEY = "session_report_results/x/2026-10-16/day/r1.json"


class FakeS3:
    def __init__(self, body=None, missing=False):
        self.body, self.missing = body, missing

    def get_object(self, Bucket, Key):
        if self.missing:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(json.dumps(self.body).encode("utf-8"))}


def status(monkeypatch, body=None, missing=False):
    monkeypatch.setattr(org, "s3", lambda: FakeS3(body, missing))
    return org._checklist_report_status(KEY)


GAPS = {"not_covered": [{"n": 3, "text": "Cover to steel checked"}],
        "failed": [{"n": 2, "text": "Edge protection installed", "comment": "north side",
                    "responsible": "Mike", "due": "Fri 17 Oct 2026"}]}


def test_THE_a_done_report_carries_its_gaps_with_the_checklist_named(monkeypatch):
    body = {"status": "done", "checklists": {
        "Pre-pour": {"items": 5, "gaps": GAPS},
        "Housekeeping": {"items": 2, "gaps": {"not_covered": [{"n": 1, "text": "Bins emptied"}], "failed": []}}}}
    st, err, gaps = status(monkeypatch, body)
    assert (st, err) == ("done", None)
    assert gaps["notCovered"] == [
        {"n": 3, "text": "Cover to steel checked", "checklist": "Pre-pour"},
        {"n": 1, "text": "Bins emptied", "checklist": "Housekeeping"}]
    assert gaps["failed"] == [dict(GAPS["failed"][0], checklist="Pre-pour")]


def test_all_covered_is_two_empty_lists_not_null(monkeypatch):
    body = {"status": "done", "checklists": {"Pre-pour": {"gaps": {"not_covered": [], "failed": []}}}}
    assert status(monkeypatch, body)[2] == {"notCovered": [], "failed": []}


@pytest.mark.parametrize("checklists", [
    None, {}, {"Pre-pour": {"items": 5}}, [], "x", {"Pre-pour": "x"},
    {"Pre-pour": {"gaps": "x"}}, {"Pre-pour": {"gaps": {"not_covered": "x"}}},
])
def test_no_gaps_known_is_null_and_the_status_stands(monkeypatch, checklists):
    body = {"status": "done"}
    if checklists is not None:
        body["checklists"] = checklists
    st, err, gaps = status(monkeypatch, body)
    assert st == "done" and gaps is None


def test_a_report_not_done_has_no_gaps(monkeypatch):
    assert status(monkeypatch, missing=True) == ("pending", None, None)
    body = {"status": "error", "error": "boom", "checklists": {"P": {"gaps": GAPS}}}
    assert status(monkeypatch, body) == ("error", "boom", None)


def test_the_payload_passes_the_gaps_through(monkeypatch):
    import datetime
    import uuid
    monkeypatch.setattr(org, "s3", lambda: FakeS3({"status": "done", "checklists": {"P": {"gaps": GAPS}}}))
    p = org._checklist_report_payload({
        "id": uuid.UUID(int=1), "report_date": datetime.date(2026, 10, 16), "check_name": "pre-pour",
        "template_name": "Pre-pour", "template_id": uuid.UUID(int=2), "start_at": "10:00:00",
        "end_at": "10:05:00", "segments": None, "request_id": "r1", "result_key": KEY})
    assert p["status"] == "done" and p["gaps"]["failed"][0]["checklist"] == "P"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/unit/test_a_checklist_report_names_its_gaps.py -v`
Expected: FAIL (`ValueError: not enough values to unpack` / `KeyError: 'gaps'`).

- [ ] **Step 3: Implement** in `src/lambda_org_api.py`:

```python
def _checklist_gaps(result):
    """What the inspector did not do, summed over the report's checklists
    (owner, 2026-10-16), or None when the result says nothing about it.

    None, never two empty lists, unless at least one checklist carries `gaps`:
    a result from before gaps were recorded, or `checklists: {}`, must not read
    as "all items covered". Never raises -- a malformed result only loses its
    gaps, never its status."""
    try:
        checklists = result.get("checklists")
        if not isinstance(checklists, dict):
            return None
        out, known = {"notCovered": [], "failed": []}, False
        for title, rep in checklists.items():
            gaps = rep.get("gaps") if isinstance(rep, dict) else None
            if not isinstance(gaps, dict):
                continue
            nc, failed = gaps.get("not_covered"), gaps.get("failed")
            if not isinstance(nc, list) or not isinstance(failed, list):
                continue
            known = True
            out["notCovered"] += [dict(g, checklist=title) for g in nc if isinstance(g, dict)]
            out["failed"] += [dict(g, checklist=title) for g in failed if isinstance(g, dict)]
        return out if known else None
    except Exception:
        logger.exception("checklist gaps unreadable")
        return None
```

Change `_checklist_report_status` to return three values (update its docstring's first line to mention gaps):

```python
    result = json.loads(obj["Body"].read().decode("utf-8"))
    status = result.get("status") or "pending"
    return status, result.get("error"), (_checklist_gaps(result) if status == "done" else None)
```

and the `NoSuchKey` branch to `return "pending", None, None`. In `_checklist_report_payload`:

```python
    status, err, gaps = _checklist_report_status(r["result_key"])
    return {..., "status": status, "error": err, "gaps": gaps}
```

(keep every existing key; add `"gaps": gaps` last). Confirm `logger` is the module's logger name with `grep -n "^logger" src/lambda_org_api.py`.

- [ ] **Step 4: Update the old stub** in `tests/unit/test_a_spoken_check_makes_its_own_report.py:114`:

```python
    monkeypatch.setattr(org, "_checklist_report_status", lambda key: ("done", None, None))
```

- [ ] **Step 5: Run both files**

Run: `python -m pytest tests/unit/test_a_checklist_report_names_its_gaps.py tests/unit/test_a_spoken_check_makes_its_own_report.py -q`
Expected: all pass. Then `grep -rn "_checklist_report_status" tests src` -- every caller/stub uses 3 values.

- [ ] **Step 6: Revert check** -- make `_checklist_gaps` `return None` at its top; THE test must FAIL; restore.

- [ ] **Step 7: Commit**

```bash
git add src/lambda_org_api.py tests/unit/test_a_checklist_report_names_its_gaps.py tests/unit/test_a_spoken_check_makes_its_own_report.py
git commit -m "org-api: a checklist report names its not-covered and failed items"
```

---

### Task 3: frontend shows the gaps (fieldsight-ui)

Work in a fresh worktree of fieldsight-ui from `origin/dev` (`git worktree add ../fsui-checklist-gaps -b feat/checklist-gaps origin/dev`).

**Files:**
- Modify: `scripts/api/checklist-reports.js` (add `gapWords`, export it on `window.FS.checklistReports` and `module.exports`)
- Modify: `scripts/pages/timeline.js` (`ChecklistReportsCard`, ~1846-1880)
- Modify: `scripts/composites/notification-bell.js` (~361, the report row title)
- Modify: `styles/composites.css` (after `.fs-checklist-reports__row--none`, ~14002)
- Test: `tests/a-checklist-report-names-its-gaps.test.js` (create)

**Interfaces:**
- Consumes: payload `gaps` from Task 2 (`notCovered[]`, `failed[]` each with `n`, `text`, `checklist`; failed also `comment`, `responsible`, `due`), or `null`/absent.
- Produces: `FS.checklistReports.gapWords(r) -> string`.

- [ ] **Step 1: Write the failing test**

```js
'use strict';

/*
 * A checklist report names what was not done (owner, 2026-10-16): items the
 * recording never addressed, and items answered No. Unknown gaps say nothing
 * -- never "All items covered".
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

global.window = global.window || {};
global.window.localStorage = global.window.localStorage || { getItem: () => null, setItem: () => {} };
const cr = require('../scripts/api/checklist-reports.js');
const read = (...p) => fs.readFileSync(path.join(__dirname, '..', ...p), 'utf8').replace(/\r\n/g, '\n');

const R = { id: 'r1', status: 'done', templateName: 'Pre-pour', checkName: 'level 2', startAt: '10:59:09' };
const G = (nc, f) => ({ notCovered: Array(nc).fill({ n: 1, text: 'x', checklist: 'P' }),
                        failed: Array(f).fill({ n: 2, text: 'y', checklist: 'P' }) });

test('THE a done report says how many items were not covered and failed', () => {
  assert.strictEqual(cr.gapWords(Object.assign({}, R, { gaps: G(3, 2) })), '3 not covered · 2 failed');
  assert.strictEqual(cr.gapWords(Object.assign({}, R, { gaps: G(1, 0) })), '1 not covered');
  assert.strictEqual(cr.gapWords(Object.assign({}, R, { gaps: G(0, 1) })), '1 failed');
});

test('nothing missing is said only when the gaps are known', () => {
  assert.strictEqual(cr.gapWords(Object.assign({}, R, { gaps: G(0, 0) })), 'All items covered');
  assert.strictEqual(cr.gapWords(Object.assign({}, R, { gaps: null })), '');
  assert.strictEqual(cr.gapWords(R), '');
  assert.strictEqual(cr.gapWords(Object.assign({}, R, { status: 'pending', gaps: G(3, 0) })), '');
});

test('the day card and the bell both show the gap words', () => {
  assert.match(read('scripts', 'pages', 'timeline.js'), /gapWords\(/);
  assert.match(read('scripts', 'composites', 'notification-bell.js'), /gapWords\(/);
  assert.match(read('styles', 'composites.css'), /\.fs-checklist-reports__gaps/);
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `node --test tests/a-checklist-report-names-its-gaps.test.js`
Expected: FAIL (`cr.gapWords is not a function`).

- [ ] **Step 3: Implement `gapWords`** in `checklist-reports.js`, after `unmatchedWords`:

```js
  /* "3 not covered · 2 failed" (owner, 2026-10-16). '' when the gaps are not
     known -- an old report, or one still being written -- never "All items
     covered" on a guess. */
  function gapWords(r) {
    if (!r || r.status !== 'done' || !r.gaps) return '';
    var nc = (r.gaps.notCovered || []).length, f = (r.gaps.failed || []).length;
    if (!nc && !f) return 'All items covered';
    var parts = [];
    if (nc) parts.push(nc + ' not covered');
    if (f) parts.push(f + ' failed');
    return parts.join(' · ');
  }
```

Add `gapWords: gapWords,` to the `window.FS.checklistReports` object and to `module.exports`.

- [ ] **Step 4: Day card** -- in `ChecklistReportsCard` (`timeline.js`), add an `open` state keyed by report id and render the summary + expansion inside each report row:

```js
    var o = React.useState({}); var open = o[0], setOpen = o[1];
```

(place it next to the existing `React.useState(null)` call, before any early `return` so hook order is stable), and replace the report row's children with:

```js
        var gw = cr && cr.gapWords ? cr.gapWords(r) : '';
        var hasList = r.gaps && ((r.gaps.notCovered || []).length || (r.gaps.failed || []).length);
        return h('div', { key: r.id, className: 'fs-checklist-reports__item' },
          h('div', { className: 'fs-checklist-reports__row' },
            h('span', { className: 'fs-checklist-reports__name' },
              cr ? cr.words(Object.assign({}, r, { date: data.date })) : r.templateName),
            gw ? (hasList
              ? h('button', { type: 'button', className: 'fs-checklist-reports__gaps',
                  'aria-expanded': !!open[r.id],
                  onClick: function () { setOpen(Object.assign({}, open, { [r.id]: !open[r.id] })); } }, gw)
              : h('span', { className: 'fs-checklist-reports__gaps fs-checklist-reports__gaps--ok' }, gw))
              : null,
            r.status === 'done' && cr ? h('button', {
              type: 'button', className: 'fs-btn fs-btn--secondary fs-btn--sm',
              onClick: function () { cr.download(Object.assign({}, r, { date: data.date }), data.folder); },
            }, 'Download') : null),
          open[r.id] && hasList ? h('div', { className: 'fs-checklist-reports__gap-lists' },
            (r.gaps.notCovered || []).length ? h('div', null,
              h('div', { className: 'fs-checklist-reports__gap-head' }, 'Not covered'),
              h('ul', null, r.gaps.notCovered.map(function (g, i) {
                return h('li', { key: 'n' + i }, g.n + '. ' + g.text);
              }))) : null,
            (r.gaps.failed || []).length ? h('div', null,
              h('div', { className: 'fs-checklist-reports__gap-head' }, 'Failed'),
              h('ul', null, r.gaps.failed.map(function (g, i) {
                var extra = [g.comment, g.responsible && ('Responsible: ' + g.responsible),
                             g.due && ('Due: ' + g.due)].filter(Boolean).join(' — ');
                return h('li', { key: 'f' + i }, g.n + '. ' + g.text + (extra ? ' — ' + extra : ''));
              }))) : null) : null);
```

If the file avoids ES2015 computed keys, build the object with a temp var instead of `{ [r.id]: ... }` (check with `grep -n "\[.*\]:" scripts/pages/timeline.js | head`).

- [ ] **Step 5: Bell** -- in `notification-bell.js` report row, under the title span add:

```js
                          checksApi.gapWords && checksApi.gapWords(r)
                            ? h('span', { className: 'fs-bell__row-meta' }, checksApi.gapWords(r))
                            : null
```

Check `fs-bell__row-meta` (or the bell's existing secondary-line class) exists with `grep -n "fs-bell__row-" styles/*.css`; use the existing secondary class.

- [ ] **Step 6: CSS** -- after `.fs-checklist-reports__row--none` in `styles/composites.css`, using existing tokens only (check names with `grep -n "^\s*--text-secondary\|--status-warn\|--border" styles/tokens*.css styles/*.css | head`):

```css
.fs-checklist-reports__gaps {
  margin-left: 8px; padding: 0; border: 0; background: none; font: inherit;
  color: var(--status-warning-text, var(--text-secondary)); cursor: pointer; text-decoration: underline;
}
.fs-checklist-reports__gaps--ok { color: var(--text-secondary); cursor: default; text-decoration: none; }
.fs-checklist-reports__gap-lists { margin: 4px 0 8px 12px; color: var(--text-primary); font-size: 0.92em; }
.fs-checklist-reports__gap-lists ul { margin: 2px 0 6px; padding-left: 18px; }
.fs-checklist-reports__gap-head { font-weight: 600; color: var(--text-secondary); }
```

Replace `--status-warning-text` with the warning text token the repo actually defines for BOTH themes; if none, use `var(--text-secondary)`.

- [ ] **Step 7: Run the new and neighbouring tests**

Run: `node --test tests/a-checklist-report-names-its-gaps.test.js tests/a-spoken-check-report-waits-on-the-timeline-and-the-bell.test.js`
Expected: pass. Then the full suite: `node --test tests/` -- compare failures against the same command on `origin/dev` (baseline), report only new ones.

- [ ] **Step 8: Commit**

```bash
git add scripts/api/checklist-reports.js scripts/pages/timeline.js scripts/composites/notification-bell.js styles/composites.css tests/a-checklist-report-names-its-gaps.test.js
git commit -m "Checklist reports name the items not covered and failed"
```

---

### Task 4: integrate, deploy to TEST/dev, verify

- [ ] **Step 1: Backend full unit suite** in the worktree: `python -m pytest tests/unit -q -x -p no:cacheprovider` (check the CI workflow's pip install line first; local onnxruntime differs). Compare failures with a baseline run on `origin/develop` (merge-base) -- report only new ones.
- [ ] **Step 2: Rebase both branches on fresh `origin/develop` / `origin/dev`**, rerun the targeted tests, push, open PRs (backend -> `develop`, frontend -> `dev`). Merge on green CI (develop/dev merges are mine; if the classifier blocks, ask the owner).
- [ ] **Step 3: Confirm the TEST deploy ran for the merge commit** (`gh run list --workflow deploy.yml` matched on `headSha`), and the Amplify dev job for the frontend merge.
- [ ] **Step 4: Verify on TEST with real data** -- find a recent TEST `checklist_reports` row (`fieldsight_test` via rds-data), re-request its report through the existing generate path or wait for a new spoken check, then invoke `fieldsight-test-org-api` `GET /api/org/checklist-reports/recent` (synthetic authorizer event, `MSYS_NO_PATHCONV=1`) and confirm `gaps` against the Word file's rows by wording.
- [ ] **Step 5: Browser check on dev** (Chrome): the Timeline day card shows the summary and the lists expand; the bell line shows the summary; both themes legible (measure contrast, transitions off). If Chrome is unavailable, record that and continue.
