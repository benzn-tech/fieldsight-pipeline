# Content-Correction Phase D Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give content edits an explicit Save/Cancel control and turn the History panel into a readable, per-person, word-diff audit log.

**Architecture:** One additive backend SQL change resolves an author display name; the rest is frontend. Two pure helpers (`diffWords`, `formatContentEdit`) do the diff/format logic and are unit-tested with `node:test`; the React components (`EditableText`, `ContentHistoryPanel`) are thin glue verified by `node --check` + a dev click-through.

**Tech Stack:** Python + psycopg (pipeline org-api), single-file React via `React.createElement` served as `text/babel` (ui), Node built-in test runner.

**Spec:** `docs/superpowers/specs/2026-07-22-content-correction-phase-d-design.md`

## Global Constraints

- **Dev artifacts (comments, commit messages, docs) in English.**
- **Backend repo:** pipeline worktree `C:/Users/camil/.claude/worktrees/content-correction-phase-d` (branch `feat/content-correction-phase-d`, off `develop`). Build venv with `UV_LINK_MODE=copy`; run tests `uv run python -m pytest tests -q`. Integration tests are marked `@pytest.mark.integration` and **skip locally without `TEST_DATABASE_URL`** (CI does not run them either) — the behavioral gate for DB SQL is an **Aurora Data API rolled-back transaction** against `fieldsight_test` (profile `fieldsight-deployer`, region `ap-southeast-2`, cluster `arn:aws:rds:ap-southeast-2:509194952652:cluster:fieldsight-db-test-dbcluster-hywiixu8ihi9`, secret `arn:aws:secretsmanager:ap-southeast-2:509194952652:secret:rds!cluster-1757a281-ee31-460d-b56e-950817921010-Ansbey`, db `fieldsight_test`). Prefix any `aws logs`/`/aws/...` arg with `export MSYS_NO_PATHCONV=1` (BUG-28).
- **Frontend repo:** ui worktree (created in Task 2) off `dev`. Tests: `node --test tests/<file>` and `node --check scripts/pages/timeline.js`. Bump cache-bust `app-shell-preview.html` `timeline.js?v=42 → v43`.
- **Deploy order:** backend first (`develop → test` stack, org-api lambda) so the frontend has `actor_name` on dev; then frontend (ui `dev`, Amplify). Prod later (`main` / `customer-prod`).
- **No new fields written**; `append_content_edit` and all writes are untouched. History visibility is already correct (History tab is not `canEditContent`-gated) — do not add or change gates.

---

### Task 1: Backend — resolve `actor_name` in `list_content_edits`

**Files:**
- Modify: `src/repositories/content_edits.py` (`list_content_edits`, ~line 21)
- Test: `tests/integration/test_content_edits_repo.py` (create)

**Interfaces:**
- Consumes: `users.upsert_user(db, sub, email, company_id=, first_name=, last_name=) → {id,...}`; `companies.create_company(db, name) → {id,...}`; `content_edits.append_content_edit(conn, company_id, table_name, row_id, field, before_text, after_text, actor_user_id, actor_role)`.
- Produces: `list_content_edits(...)` rows now each carry `actor_name` (string, or `None` when the actor user row is absent). No signature change.

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_content_edits_repo.py`:

```python
import pytest
from repositories import companies, users, content_edits

pytestmark = pytest.mark.integration


def test_list_content_edits_resolves_actor_name(db):
    co = companies.create_company(db, "CE-Co")
    editor = users.upsert_user(db, "sub-ce-1", "ed@ce.nz", company_id=co["id"],
                               first_name="Bailey", last_name="Lin")
    content_edits.append_content_edit(
        db, co["id"], "topics", "row-1", "topic_title",
        "Old title", "New title", editor["id"], "site_manager")

    edits = content_edits.list_content_edits(db, co["id"], "topics", "row-1")
    assert len(edits) == 1
    assert edits[0]["actor_name"] == "Bailey Lin"
    assert edits[0]["before_text"] == "Old title"
    assert edits[0]["after_text"] == "New title"


def test_list_content_edits_actor_name_null_when_user_absent(db):
    co = companies.create_company(db, "CE-Co2")
    content_edits.append_content_edit(
        db, co["id"], "topics", "row-2", "topic_title",
        "A", "B", None, "admin")  # no actor_user_id
    edits = content_edits.list_content_edits(db, co["id"], "topics", "row-2")
    assert len(edits) == 1
    assert edits[0]["actor_name"] is None
```

- [ ] **Step 2: Run the test (expect SKIP locally, or FAIL with a DB)**

Run: `cd C:/Users/camil/.claude/worktrees/content-correction-phase-d && export UV_LINK_MODE=copy && uv run python -m pytest tests/integration/test_content_edits_repo.py -q`
Expected: **SKIPPED** (no `TEST_DATABASE_URL`) — this is normal here. If a `TEST_DATABASE_URL` is set, it must FAIL with `KeyError: 'actor_name'` before the code change.

- [ ] **Step 3: Implement the JOIN**

In `src/repositories/content_edits.py`, replace the body of `list_content_edits`:

```python
def list_content_edits(conn, company_id, table_name, row_id):
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS_QUALIFIED}, "
        f"       NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), '') AS actor_name "
        f"FROM content_edits ce "
        f"LEFT JOIN users u ON u.id = ce.actor_user_id "
        f"WHERE ce.company_id=%s AND ce.table_name=%s AND ce.row_id=%s "
        f"ORDER BY ce.created_at DESC",
        (company_id, table_name, row_id),
    ).fetchall()
```

Add a qualified column list next to `_COLS` at the top of the file (keep `_COLS` for `append_content_edit`'s `RETURNING`):

```python
_COLS_QUALIFIED = ", ".join("ce." + c for c in _COLS.split(", "))
```

- [ ] **Step 4: Verify the SQL behavior via Aurora Data API (behavioral gate)**

Because the integration test skips locally, verify the actual SQL against `fieldsight_test` in a **rolled-back** transaction. Run (Git Bash):

```bash
export AWS_PROFILE=fieldsight-deployer AWS_REGION=ap-southeast-2
CL=arn:aws:rds:ap-southeast-2:509194952652:cluster:fieldsight-db-test-dbcluster-hywiixu8ihi9
SEC=arn:aws:secretsmanager:ap-southeast-2:509194952652:secret:rds!cluster-1757a281-ee31-460d-b56e-950817921010-Ansbey
TX=$(aws rds-data begin-transaction --resource-arn "$CL" --secret-arn "$SEC" --database fieldsight_test --query transactionId --output text)
# seed a user + a content_edit, then run the new query, then roll back
aws rds-data execute-statement --resource-arn "$CL" --secret-arn "$SEC" --database fieldsight_test --transaction-id "$TX" \
  --sql "INSERT INTO users (id, cognito_sub, email, first_name, last_name) VALUES (gen_random_uuid(),'ce-verify-sub','v@x.nz','Bailey','Lin') RETURNING id" >/tmp/u.json
UID=$(node -e "let d='';process.stdin.on('data',c=>d+=c);process.stdin.on('end',()=>console.log(JSON.parse(d).records[0][0].stringValue))" </tmp/u.json)
aws rds-data execute-statement --resource-arn "$CL" --secret-arn "$SEC" --database fieldsight_test --transaction-id "$TX" \
  --sql "INSERT INTO content_edits (company_id, table_name, row_id, field, before_text, after_text, actor_user_id, actor_role) SELECT company_id, 'topics','ce-verify-row','topic_title','Old','New', '$UID','site_manager' FROM users WHERE id='$UID' LIMIT 0"
# (the SELECT above is a no-op guard; insert the row explicitly:)
aws rds-data execute-statement --resource-arn "$CL" --secret-arn "$SEC" --database fieldsight_test --transaction-id "$TX" \
  --sql "INSERT INTO content_edits (company_id, table_name, row_id, field, before_text, after_text, actor_user_id, actor_role) VALUES ((SELECT company_id FROM users WHERE id='$UID' LIMIT 1),'topics','ce-verify-row','topic_title','Old','New','$UID','site_manager')" 2>/dev/null || \
aws rds-data execute-statement --resource-arn "$CL" --secret-arn "$SEC" --database fieldsight_test --transaction-id "$TX" \
  --sql "INSERT INTO content_edits (company_id, table_name, row_id, field, before_text, after_text, actor_user_id, actor_role) VALUES ((SELECT id FROM companies LIMIT 1),'topics','ce-verify-row','topic_title','Old','New','$UID','site_manager')"
aws rds-data execute-statement --resource-arn "$CL" --secret-arn "$SEC" --database fieldsight_test --transaction-id "$TX" \
  --sql "SELECT ce.field, NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)),'') AS actor_name FROM content_edits ce LEFT JOIN users u ON u.id=ce.actor_user_id WHERE ce.row_id='ce-verify-row'"
aws rds-data rollback-transaction --resource-arn "$CL" --secret-arn "$SEC" --transaction-id "$TX"
```
Expected: the SELECT returns `actor_name = "Bailey Lin"`. (The `users` insert assumes `users` allows a row without a company/membership for this probe; if a NOT NULL column blocks it, seed `company_id` from an existing `companies` row — the fallback INSERT above does this. Adjust column names to the real `users` schema if the probe errors; the goal is only to confirm the JOIN resolves the name.) Roll back leaves `fieldsight_test` unchanged.

- [ ] **Step 5: Commit**

```bash
cd C:/Users/camil/.claude/worktrees/content-correction-phase-d
git add src/repositories/content_edits.py tests/integration/test_content_edits_repo.py
git commit -m "feat(org-api): resolve actor_name in content_edits history via users JOIN"
```

- [ ] **Step 6: Deploy backend to test + confirm through the endpoint**

Push the branch and open a PR to `develop` (merge is human-gated — the auto classifier blocks agent self-merge; ask the user to run `gh pr merge <n> --merge -R benzn-tech/fieldsight-pipeline`). After `develop` deploys the org-api lambda to the test stack, the frontend on dev will receive `actor_name`. (Full end-to-end confirmation happens in Task 6 on dev.)

---

### Task 2: Frontend — `diffWords` pure helper + ui worktree

**Files:**
- Create ui worktree: `C:/Users/camil/.claude/worktrees/content-correction-phase-d-ui` (branch `feat/content-correction-phase-d`, off `dev`)
- Modify: `scripts/pages/timeline.js` (add helper near the other module-level helpers, ~line 100; extend the `module.exports` block at the bottom)
- Test: `tests/content-edit-format.test.js` (create)

**Interfaces:**
- Produces: `diffWords(before, after) → Array<{type:'same'|'del'|'ins', text:string}>`. Whitespace-tokenized LCS diff; tokens keep trailing whitespace so joining all `text` reproduces `after` (for same+ins) / `before` (for same+del). Consecutive same-type runs are merged. Empty inputs → `[]` or a single `del`/`ins`.

- [ ] **Step 1: Create the ui worktree**

```bash
cd C:/Users/camil/Dropbox/fieldsight-ui
git fetch origin
git worktree add -b feat/content-correction-phase-d "C:/Users/camil/.claude/worktrees/content-correction-phase-d-ui" origin/dev
```

- [ ] **Step 2: Write the failing test**

Create `C:/Users/camil/.claude/worktrees/content-correction-phase-d-ui/tests/content-edit-format.test.js`:

```javascript
'use strict';
const test = require('node:test');
const assert = require('node:assert');
global.window = global.window || {};
global.React = global.React || {};
const { diffWords } = require('../scripts/pages/timeline.js');

const join = (segs, types) => segs.filter(s => types.includes(s.type)).map(s => s.text).join('');

test('diffWords: identical text is one same segment', () => {
  assert.deepStrictEqual(diffWords('a b c', 'a b c'), [{ type: 'same', text: 'a b c' }]);
});
test('diffWords: pure insert', () => {
  const segs = diffWords('a c', 'a b c');
  assert.deepStrictEqual(segs.map(s => s.type), ['same', 'ins', 'same']);
  assert.strictEqual(join(segs, ['same', 'ins']), 'a b c');
});
test('diffWords: pure delete', () => {
  const segs = diffWords('a b c', 'a c');
  assert.deepStrictEqual(segs.map(s => s.type), ['same', 'del', 'same']);
  assert.strictEqual(join(segs, ['same', 'del']), 'a b c');
});
test('diffWords: replaced word is del then ins', () => {
  const segs = diffWords('a b c', 'a x c');
  assert.deepStrictEqual(segs.map(s => s.type), ['same', 'del', 'ins', 'same']);
  assert.strictEqual(join(segs, ['same', 'del']), 'a b c');
  assert.strictEqual(join(segs, ['same', 'ins']), 'a x c');
});
test('diffWords: empty before → single ins; empty after → single del', () => {
  assert.deepStrictEqual(diffWords('', 'a b'), [{ type: 'ins', text: 'a b' }]);
  assert.deepStrictEqual(diffWords('a b', ''), [{ type: 'del', text: 'a b' }]);
});
test('diffWords: full rewrite → all del then all ins, reconstructs both sides', () => {
  const segs = diffWords('a b', 'x y');
  assert.strictEqual(join(segs, ['same', 'del']), 'a b');
  assert.strictEqual(join(segs, ['same', 'ins']), 'x y');
});
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `cd C:/Users/camil/.claude/worktrees/content-correction-phase-d-ui && node --test tests/content-edit-format.test.js`
Expected: FAIL — `diffWords is not a function` (helper + export not present yet).

- [ ] **Step 4: Implement `diffWords`**

In `scripts/pages/timeline.js`, add near the other module-level helpers (after `reconcileTopicOverrides`, ~line 100), inside the IIFE:

```javascript
  /* content-correction Phase D — whitespace-tokenized LCS word diff. Tokens
     keep their trailing whitespace so joining same+ins reproduces `after` and
     same+del reproduces `before`. Consecutive same-type runs are merged. */
  function _tokenizeWords(s) { return (s || '').match(/\S+\s*/g) || []; }
  function diffWords(before, after) {
    var a = _tokenizeWords(before), b = _tokenizeWords(after);
    var m = a.length, n = b.length;
    var dp = [];
    for (var i = 0; i <= m; i++) { var row = []; for (var j = 0; j <= n; j++) row.push(0); dp.push(row); }
    for (var i = m - 1; i >= 0; i--) {
      for (var j = n - 1; j >= 0; j--) {
        dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
    var segs = [];
    function push(type, text) {
      if (segs.length && segs[segs.length - 1].type === type) segs[segs.length - 1].text += text;
      else segs.push({ type: type, text: text });
    }
    var i = 0, j = 0;
    while (i < m && j < n) {
      if (a[i] === b[j]) { push('same', a[i]); i++; j++; }
      else if (dp[i + 1][j] >= dp[i][j + 1]) { push('del', a[i]); i++; }
      else { push('ins', b[j]); j++; }
    }
    while (i < m) { push('del', a[i]); i++; }
    while (j < n) { push('ins', b[j]); j++; }
    return segs;
  }
```

At the bottom `module.exports` block, add `diffWords`:

```javascript
    module.exports = {
      applyTopicOverrides: applyTopicOverrides,
      partitionTopics: partitionTopics,
      reconcileTopicOverrides: reconcileTopicOverrides,
      diffWords: diffWords,
    };
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `node --test tests/content-edit-format.test.js`
Expected: PASS (6 tests). Also run `node --check scripts/pages/timeline.js` → no output.

- [ ] **Step 6: Commit**

```bash
git add scripts/pages/timeline.js tests/content-edit-format.test.js
git commit -m "feat(ui): diffWords LCS word-diff helper for content history"
```

---

### Task 3: Frontend — `formatContentEdit` + `formatEditTime`

**Files:**
- Modify: `scripts/pages/timeline.js` (add helpers next to `diffWords`; extend `module.exports`)
- Test: `tests/content-edit-format.test.js` (extend)

**Interfaces:**
- Consumes: `diffWords` (Task 2).
- Produces:
  - `formatEditTime(isoUtc) → string` — a UTC timestamp (ISO, or `"YYYY-MM-DD HH:MM:SS+00:00"`) formatted to NZ local time `YYYY/MM/DD HH:MM` (via `Intl` `timeZone:'Pacific/Auckland'`, DST-correct). Empty/invalid → `''`/the raw string.
  - `formatContentEdit(edit) → { field, when, who, segments }` where `when = formatEditTime(edit.created_at)`, `who = edit.actor_name || edit.actor_role || 'Unknown'`, `segments = diffWords(edit.before_text||'', edit.after_text||'')`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/content-edit-format.test.js`:

```javascript
const { formatEditTime, formatContentEdit } = require('../scripts/pages/timeline.js');

test('formatEditTime: UTC → NZ standard time (winter, +12)', () => {
  // 2026-07-22 is NZ winter (NZST, UTC+12): 03:14Z → 15:14
  assert.strictEqual(formatEditTime('2026-07-22T03:14:00+00:00'), '2026/07/22 15:14');
});
test('formatEditTime: UTC → NZ daylight time (summer, +13)', () => {
  // 2026-01-15 is NZ summer (NZDT, UTC+13): 03:14Z → 16:14
  assert.strictEqual(formatEditTime('2026-01-15T03:14:00+00:00'), '2026/01/15 16:14');
});
test('formatEditTime: DB space-separated timestamp with microseconds', () => {
  assert.strictEqual(formatEditTime('2026-07-22 03:14:53.757118+00:00'), '2026/07/22 15:14');
});
test('formatEditTime: empty → empty', () => {
  assert.strictEqual(formatEditTime(''), '');
  assert.strictEqual(formatEditTime(null), '');
});
test('formatContentEdit: assembles field/when/who/segments with name preferred over role', () => {
  const out = formatContentEdit({
    field: 'topic_title', created_at: '2026-07-22T03:14:00+00:00',
    actor_name: 'Bailey Lin', actor_role: 'site_manager',
    before_text: 'a b c', after_text: 'a x c',
  });
  assert.strictEqual(out.field, 'topic_title');
  assert.strictEqual(out.when, '2026/07/22 15:14');
  assert.strictEqual(out.who, 'Bailey Lin');
  assert.deepStrictEqual(out.segments.map(s => s.type), ['same', 'del', 'ins', 'same']);
});
test('formatContentEdit: falls back to actor_role, then Unknown', () => {
  assert.strictEqual(formatContentEdit({ actor_role: 'admin', before_text: '', after_text: 'x' }).who, 'admin');
  assert.strictEqual(formatContentEdit({ before_text: '', after_text: 'x' }).who, 'Unknown');
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `node --test tests/content-edit-format.test.js`
Expected: the new tests FAIL (`formatEditTime`/`formatContentEdit` not a function); Task 2's tests still pass.

- [ ] **Step 3: Implement the helpers**

Add next to `diffWords` in `scripts/pages/timeline.js`:

```javascript
  /* content-correction Phase D — a UTC content_edits.created_at → NZ local
     "YYYY/MM/DD HH:MM". Intl with an explicit IANA zone is DST-correct and is
     NOT the BUG-19 naive-parse pattern (the input carries a +00:00 offset). */
  function formatEditTime(iso) {
    if (!iso) return '';
    var d = new Date(String(iso).replace(' ', 'T'));
    if (isNaN(d.getTime())) return String(iso);
    var parts = new Intl.DateTimeFormat('en-NZ', {
      timeZone: 'Pacific/Auckland', year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hour12: false,
    }).formatToParts(d);
    var p = {};
    parts.forEach(function (x) { p[x.type] = x.value; });
    var hour = p.hour === '24' ? '00' : p.hour;   // Intl may emit '24' at midnight
    return p.year + '/' + p.month + '/' + p.day + ' ' + hour + ':' + p.minute;
  }

  /* content-correction Phase D — one content_edits row → display parts. */
  function formatContentEdit(edit) {
    edit = edit || {};
    return {
      field: edit.field,
      when: formatEditTime(edit.created_at),
      who: edit.actor_name || edit.actor_role || 'Unknown',
      segments: diffWords(edit.before_text || '', edit.after_text || ''),
    };
  }
```

Extend `module.exports`:

```javascript
      diffWords: diffWords,
      formatEditTime: formatEditTime,
      formatContentEdit: formatContentEdit,
```

- [ ] **Step 4: Run to verify it passes**

Run: `node --test tests/content-edit-format.test.js`
Expected: PASS (all tests). `node --check scripts/pages/timeline.js` → clean.

- [ ] **Step 5: Commit**

```bash
git add scripts/pages/timeline.js tests/content-edit-format.test.js
git commit -m "feat(ui): formatContentEdit + NZ-time formatter for content history"
```

---

### Task 4: Frontend — `ContentHistoryPanel` rich rendering

**Files:**
- Modify: `scripts/pages/timeline.js` (`ContentHistoryPanel`, ~line 1746-1768)
- Test: none new (rendering glue; the logic is Task 2/3, already tested)

**Interfaces:**
- Consumes: `formatContentEdit` (Task 3).

- [ ] **Step 1: Replace the render body**

Replace the `return React.createElement('ul', ...)` block in `ContentHistoryPanel` (the list of `<li>` rows) with:

```javascript
    return React.createElement('ul', { className: 'fs-content-history' },
      data.edits.map(function (e) {
        var f = formatContentEdit(e);
        return React.createElement('li', { key: e.id, className: 'fs-content-history__item' },
          React.createElement('div', { className: 'fs-content-history__meta' },
            React.createElement('span', { className: 'fs-content-history__field' }, f.field),
            ' · ' + f.when + ' · edited by ' + f.who),
          React.createElement('div', { className: 'fs-content-history__diff' },
            f.segments.map(function (seg, i) {
              var cls = seg.type === 'del' ? 'fs-content-history__del'
                      : seg.type === 'ins' ? 'fs-content-history__ins'
                      : 'fs-content-history__same';
              return React.createElement('span', { key: i, className: cls }, seg.text);
            })));
      }));
```

Leave the `loading` and empty (`No edits yet.`) branches unchanged. Do **not** change any `canEditContent` gate — the History tab is already viewable by anyone who can open the topic (`EvidenceTabs` at ~line 2039 renders on `topicRowId`, not on edit permission).

- [ ] **Step 2: Add styles**

In the styles file that holds `.fs-content-history` (grep `fs-content-history` under `styles/` to find it; if none, add to the timeline/topic-detail stylesheet):

```css
.fs-content-history__item { margin-bottom: 12px; }
.fs-content-history__meta { font-size: 12px; color: var(--text-tertiary); margin-bottom: 2px; }
.fs-content-history__field { font-weight: 600; color: var(--text-secondary); }
.fs-content-history__diff { font-size: 13px; line-height: 1.5; white-space: pre-wrap; }
.fs-content-history__del { text-decoration: line-through; color: var(--danger, #d64545); }
.fs-content-history__ins { color: var(--success, #2e7d32); }
```

- [ ] **Step 3: Syntax check**

Run: `node --check scripts/pages/timeline.js`
Expected: no output. (Behavioral check happens on dev in Task 6.)

- [ ] **Step 4: Commit**

```bash
git add scripts/pages/timeline.js styles/
git commit -m "feat(ui): rich content-history rows — author name + word diff"
```

---

### Task 5: Frontend — `EditableText` Save/Cancel

**Files:**
- Modify: `scripts/pages/timeline.js` (`EditableText` ~line 1686-1741; and each `EditableText` mount site to pass `onExitEdit`)

**Interfaces:**
- Consumes: existing `commit()` behavior and `props.onSaved`.
- Produces: `EditableText` accepts a new optional `props.onExitEdit()` callback, invoked after a successful Save and on Cancel, so the parent can clear its `editingKey`.

- [ ] **Step 1: Rewrite `EditableText`'s commit + controls**

Change `commit()` so a successful save also exits, and add a `cancel()`; replace `onBlur`/`onKeyDown` and add a Save/Cancel control row. Replace the `commit` function's success tail and the returned JSX:

In `commit()`, after `if (props.onSaved) props.onSaved(res);` add `if (props.onExitEdit) props.onExitEdit();`. (On the failure branches, do NOT exit — leave the editor open.)

Add a `cancel` function right after `commit`:

```javascript
    function cancel() {
      setValue(props.value || '');
      if (props.onExitEdit) props.onExitEdit();
    }
```

Replace the returned `React.createElement(React.Fragment, ... textarea ...)` with:

```javascript
    var IconBtn = window.FieldSight.IconButton;
    return React.createElement(React.Fragment, null,
      React.createElement('textarea', {
        className: 'fs-content-edit' + (busy ? ' fs-content-edit--busy' : ''),
        value: value, rows: props.rows || 2, disabled: busy, autoFocus: true,
        'aria-label': props.ariaLabel || props.field,
        onChange: function (e) { setValue(e.target.value); },
        onKeyDown: function (e) {
          if (e.ctrlKey && e.key === 'Enter') { e.preventDefault(); commit(); }
          else if (e.key === 'Escape') { e.preventDefault(); cancel(); }
        },
      }),
      React.createElement('div', { className: 'fs-content-edit__controls' },
        IconBtn ? React.createElement(IconBtn, {
          icon: 'check', size: 'sm', variant: 'ghost', disabled: busy,
          ariaLabel: 'Save', onClick: commit,
        }) : null,
        IconBtn ? React.createElement(IconBtn, {
          icon: 'x', size: 'sm', variant: 'ghost', disabled: busy,
          ariaLabel: 'Cancel', onClick: cancel,
        }) : null),
      candidates.length > 0 ? React.createElement(GlossaryConfirm, {
        candidates: candidates,
        onConfirmed: function (term) {
          setCandidates(function (cur) { return cur.filter(function (c) { return c !== term; }); });
        },
      }) : null,
    );
```

Note: `onBlur: commit` is removed (blur no longer auto-commits).

- [ ] **Step 2: Pass `onExitEdit` at each `EditableText` mount**

Each place that renders `EditableText` with `editable: true` sits inside a component that owns the edit-mode toggle via `setEditingKey`. For each mount, add `onExitEdit: function () { setEditingKey(null); }`. Grep to enumerate: `grep -n "React.createElement(EditableText" scripts/pages/timeline.js`. For every hit whose `editable` can be true (topic title, topic details/summary, action item text/responsible, finding observation/recommended_action, safety observation), add the prop. Example (topic title mount ~line 1794 area):

```javascript
      React.createElement(EditableText, {
        editable: canEditContent && !!topicRowId, table: 'topics', id: topicRowId,
        field: 'topic_title', value: topic.topic_title || '', /* ...existing... */
        onExitEdit: function () { setEditingKey(null); },
      }),
```

Where a mount is NOT wrapped by an `editingKey` toggle (always-on editors, if any), pass `onExitEdit` as a no-op `function () {}` so Save/Cancel still function (Cancel restores the value; nothing to close).

- [ ] **Step 3: Add control styles**

In the stylesheet with `.fs-content-edit`:

```css
.fs-content-edit__controls { display: flex; gap: 4px; margin-top: 4px; }
```

- [ ] **Step 4: Syntax check**

Run: `node --check scripts/pages/timeline.js`
Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add scripts/pages/timeline.js styles/
git commit -m "feat(ui): explicit Save/Cancel for content editing (no blur auto-commit)"
```

---

### Task 6: Cache-bust, deploy, and dev end-to-end verification

**Files:**
- Modify: `app-shell-preview.html` (`timeline.js?v=42 → v43`)

- [ ] **Step 1: Bump cache-bust**

In `app-shell-preview.html`, change `scripts/pages/timeline.js?v=42` to `?v=43`. Commit:

```bash
git add app-shell-preview.html
git commit -m "chore(ui): cache-bust timeline.js v43 (content-correction Phase D)"
```

- [ ] **Step 2: Full frontend test + syntax gate**

Run: `node --test tests/content-edit-format.test.js && node --test tests/timeline-redaction-overrides.test.js && node --check scripts/pages/timeline.js`
Expected: all PASS, no syntax output.

- [ ] **Step 3: Deploy backend (if not already) then frontend**

Backend: push `feat/content-correction-phase-d` (pipeline) → PR to `develop` → ask user to merge → confirm the test-stack org-api build. Frontend: push `feat/content-correction-phase-d` (ui) → PR to `dev` → ask user to merge → poll the Amplify `dev` job to SUCCEED (`aws amplify get-job --app-id d2fssznicvuckr --branch-name dev --job-id <n>`).

- [ ] **Step 4: Dev click-through (behavioral gate)**

On `https://dev.d2fssznicvuckr.amplifyapp.com`, single-user timeline of a topic with a durable id:
1. Click the pencil on a topic title → textarea + ✓/✕ appear. Edit text → **✕ Cancel** → value restored, editor closed, no write. Re-edit → **✓ Save** → persists, editor closes; reload confirms the new value.
2. Open the **History** tab → the edit shows as `topic_title · <NZ time> · edited by <name>` with the changed words struck-through (old) / green (new).
3. Confirm a **view-only** perspective (a role without content:edit) still sees the History tab (read-only) but no pencil/Save controls.

- [ ] **Step 5: Update handoff + memory**

Mark Q3 done in `docs/superpowers/SESSION-HANDOFF-2026-07-22-life-sep.md` and the `fieldsight-content-correction` / life-sep memory as appropriate.

---

## Self-Review

**Spec coverage:** Backend `actor_name` JOIN → Task 1. Save/Cancel (Option A, blur removed, Ctrl+Enter/Esc) → Task 5. History word-diff + author + NZ time → Tasks 2/3/4. Visibility (already viewable) → verified in Task 4 note + Task 6 step 4.3. Deploy order (backend→frontend) → Task 1 step 6 + Task 6. All spec sections covered.

**Placeholder scan:** No TBD/TODO; all code shown; the one conditional ("adjust column names if the users probe errors") is an explicit, bounded verification fallback, not a placeholder.

**Type consistency:** `diffWords → [{type,text}]` used identically in Tasks 2/3/4. `formatContentEdit → {field,when,who,segments}` produced in Task 3, consumed in Task 4. `onExitEdit` defined in Task 5 step 1, passed in step 2. `actor_name` produced by Task 1, consumed by `formatContentEdit` in Task 3. Consistent.
