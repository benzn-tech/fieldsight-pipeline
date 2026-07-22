# Content-Correction Phase D — Save/Cancel + Rich History (design)

**Date:** 2026-07-22
**Status:** approved (brainstorm), pending spec review → plan
**Scope:** 3 changes across 2 repos. Backend: 1 SQL change (pipeline
`repositories/content_edits.py`). Frontend: 2 components in ui
`scripts/pages/timeline.js`.

This is the **Q3** follow-up from `docs/superpowers/SESSION-HANDOFF-2026-07-22-life-sep.md`
— the content-correction (Phase D) *editing UX*, distinct from life-conversation
separation. Content corrections already re-index into the vector + relational
stores (existing content-correction wiring); the value here is (a) a safer edit
control and (b) a transparent, human-readable audit trail so a reviewer can see
at a glance *who* changed *what*, which also keeps the corrected text feeding the
stores cleanly (search-precision goal).

## Motivation

- `EditableText` commits on **blur / Ctrl+Enter** with no explicit control — easy
  to save by accident, no way to abandon an in-progress edit. Users asked for
  explicit **✓ Save / ✕ Cancel**.
- `ContentHistoryPanel` renders each edit as flat text
  `"<before>" → "<after>" · <actor_role> · <raw created_at>`. It shows the
  role but **not the person**, the whole old/new values (not *what* changed),
  and a raw ISO timestamp. Users want a readable **log**: who, when, and a
  **word-level diff** so a big rewrite vs a few-word tweak is obvious at a glance.
- The log should be visible to **anyone who can view the item** (transparency:
  a worker's edit is visible to their site_manager / PM), while *editing* stays
  restricted to reviewers.

## Decisions (resolved in brainstorm)

1. **Save/Cancel = explicit-commit (Option A).** ✓ Save writes then exits edit
   mode; ✕ Cancel restores the original and exits without writing. **Blur no
   longer auto-commits.** Ctrl+Enter = Save, Esc = Cancel.
2. **Author name = backend JOIN (Option 2).** `list_content_edits` LEFT JOINs
   `users` and returns a resolved `actor_name`. Chosen over a frontend
   member-map for a single server-side source of truth that always resolves
   (authorized by the row's `company_id`), at the cost of one small, additive,
   read-only backend change + a test deploy.
3. **History = word-level diff**, not whole-value strikethrough. A simple
   whitespace-tokenized LCS diff; unchanged words plain, removed words struck
   through (red), added words green.
4. **History visibility relaxed** from `canEditContent` to *viewable*: anyone who
   can open the topic sees the History (read-only). Edit affordances (pencil,
   `EditableText`, review buttons) stay `canEditContent`. The backend
   `get_content_history` is already company-guarded (no edit-permission
   requirement), so only the frontend gate changes.

## Change 1 — Backend: `actor_name` (pipeline `repositories/content_edits.py`)

`list_content_edits(conn, company_id, table_name, row_id)` currently:

```sql
SELECT id, company_id, table_name, row_id, field, before_text, after_text,
       actor_user_id, actor_role, created_at
FROM content_edits
WHERE company_id=%s AND table_name=%s AND row_id=%s
ORDER BY created_at DESC
```

Change to add a resolved display name:

```sql
SELECT ce.id, ce.company_id, ce.table_name, ce.row_id, ce.field,
       ce.before_text, ce.after_text, ce.actor_user_id, ce.actor_role,
       ce.created_at,
       NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), '') AS actor_name
FROM content_edits ce
LEFT JOIN users u ON u.id = ce.actor_user_id
WHERE ce.company_id=%s AND ce.table_name=%s AND ce.row_id=%s
ORDER BY ce.created_at DESC
```

- `LEFT JOIN` so an edit whose actor row is missing still returns (`actor_name`
  null → frontend falls back to `actor_role`).
- `users.first_name` / `users.last_name` confirmed present (used by
  `memberships` member query). `actor_user_id` references `users.id`.
- `get_content_history` endpoint (`lambda_org_api.py`) is unchanged — it passes
  `list_content_edits`'s rows straight through, so the new field flows out.
- **No change to writes** (`append_content_edit`) or any other read.

**Test:** integration/repo test — insert a content_edit by a known user, assert
`list_content_edits` returns that user's `actor_name`; and an edit by a
now-absent user still returns with `actor_name` null.

## Change 2 — Frontend: `EditableText` Save/Cancel (ui `timeline.js`)

`EditableText` renders a `<textarea>` when `editable`, currently committing on
`onBlur` and `Ctrl+Enter`. Edit mode is owned by the parent (`editingKey` set by
the pencil `editToggle`; `EditableText` is mounted for the active field).

Changes:

- **Remove `onBlur: commit`.** Keep `onChange`. Change `onKeyDown` to:
  Ctrl+Enter → save, Esc → cancel.
- Render a small control row beneath the textarea with **✓ Save** and
  **✕ Cancel** buttons (`IconButton`, icons `check` / `x`), disabled while busy.
- **✓ Save** = existing `commit()`; on success (after `onSaved`) call a new
  `props.onExitEdit()` so the parent clears `editingKey` and the editor closes.
  (If the value is unchanged, Save still exits — no-op write is already guarded.)
- **✕ Cancel** = `setValue(props.value || '')` then `props.onExitEdit()`; no
  write.
- Parent passes `onExitEdit: function () { setEditingKey(null); }` at each
  `EditableText` mount (topic title/details and any other content fields).
- Failure path unchanged (revert value + error toast); on failure the editor
  stays open so the user can retry or cancel.

This is React glue (state + callbacks); verified via `node --check` + dev
click-through, not a unit test.

## Change 3 — Frontend: `ContentHistoryPanel` = log + word diff (ui `timeline.js`)

**Visibility:** the History tab/panel gate moves from `canEditContent` to
"topic is viewable" (i.e. shown whenever the durable id exists and the topic is
open — the timeline already scopes which topics a caller can open). Editing
affordances stay `canEditContent`.

**Row rendering:** each `content_edits` row (one field change) renders as:

```
<field> · 2026/07/22 10:00 · edited by <actor_name || actor_role>
<word-diff of before_text → after_text>
```

- **Timestamp:** `created_at` (UTC) formatted to NZDT `YYYY/MM/DD HH:MM` (reuse
  the page's existing NZDT display convention; UTC→NZDT like other timeline
  times).
- **Author:** `edit.actor_name || edit.actor_role || 'Unknown'`.
- **Word diff:** unchanged words plain, removed words strikethrough (muted/red),
  added words green.

**Pure helpers (node:test):**

- `diffWords(before, after) → [{ type: 'same' | 'del' | 'ins', text }]` —
  whitespace-tokenized LCS diff. Tokens keep their trailing whitespace so
  re-joining segments reproduces the text. Cases: identical, pure insert, pure
  delete, mixed edit, empty→text, text→empty, full rewrite (all del + all ins).
- `formatContentEdit(edit) → { field, when, who, segments }` — assembles the
  header fields + `diffWords(before_text, after_text)`; pure, given a fixed
  "now"/timezone input so it's deterministic under test (pass the formatter a
  UTC string; assert the NZDT output).

Rendering maps `segments` to spans (`same`/`del`/`ins` CSS classes). Loading and
empty states unchanged.

## Testing summary

| Piece | Test |
|---|---|
| `diffWords`, `formatContentEdit` | `node:test` (pure), ui `tests/` |
| `list_content_edits` actor_name | pipeline repo/integration test |
| Save/Cancel glue | `node --check` + dev click-through |
| End-to-end | dev: edit a topic title → ✓ Save persists / ✕ Cancel discards; open History → rich row with name + word diff; a **view-only** role sees History but no edit affordances |

## Deploy

- **Backend:** pipeline feature branch → PR → `develop` → test stack (org-api
  lambda). Later `main` → prod. Migration-free (read-only query change).
- **Frontend:** ui feature branch → PR → `dev` (Amplify build); cache-bust
  `timeline.js?v=42 → v43`. Later `dev → customer-prod`.
- Order: backend first (so the frontend has `actor_name` to show on dev), then
  frontend.

## Out of scope (YAGNI)

- Character-level diff (word-level is enough).
- History pagination / filtering.
- "you" special-casing for the caller's own edits (show the name).
- Any change to the re-index-on-edit pipeline (already wired).
- Tightening `get_content_history`'s company-level scope to site-level (existing
  behavior; not part of this transparency change).
