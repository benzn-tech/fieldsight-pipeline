# QR Terminal Sign-In v2 — Web Implementation Plan (fieldsight-ui)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (recommended) or executing-plans. No TDD/test harness for these UI files — verify with `node --check` + a browser smoke; bump `?v=`. Steps use `- [ ]`.

**Goal:** The web "Log in a terminal" now sends the web session's refresh token to the create endpoint and renders a v2 QR (code-only, no username).

**Architecture:** Two small edits to the shipped v1 web pieces: `FS.api.qrLogin.create()` includes the refresh token in the POST body; `QrLoginModal` builds the QR from `{v:2, c, env}`.

**Tech Stack:** Vanilla JS, browser React, `orgRequest`, `FS.session`/cognito.js. No build.

**Design spec:** `GrandTime/docs/superpowers/specs/2026-07-31-qr-login-refresh-handoff-design.md` (§5).
**Base:** cut `feat/qr-login-v2-web` off **current `origin/dev`** (which does NOT yet have the v1 web PR #139; the v1 web PR is still open — either merge #139 first then branch, or branch off dev and re-apply; simplest: branch off `feat/qr-login-web` (the v1 branch) since these are edits ON TOP of v1).

## Global Constraints
- No build/npm. Tokens-only for colour/spacing; BEM. Bump `?v=` on changed files. Token-bearing calls via `orgRequest`. **Never** `console.log` / persist the refresh token beyond the create call. QR content = `{v:2, c:<code>, env}` (H ECC, no logo).

---

### Task 1: `create()` sends the web refresh token

**Files:** Modify `scripts/api/qr-login.js`.

**Interfaces:** `FS.api.qrLogin.create()` → still resolves `{code, expiresAt, ttlSeconds}`; now POSTs `{ refreshToken }`.

- [ ] **Step 1:** In `create()`, obtain the web session's refresh token. Grep `scripts/auth/cognito.js` + `_fetch.js`/`FS.session` for where the refresh token lives (e.g. `FS.session.refreshToken`, or a `localStorage` key cognito.js writes). Use the established accessor — do NOT read storage directly if an accessor exists.
- [ ] **Step 2:** Change the real branch to send it:
```javascript
    var rt = (window.FS.session && window.FS.session.refreshToken) || <the-accessor-you-found>;
    return window.FS.api.orgRequest('/auth/qr/create', { method: 'POST', body: { refreshToken: rt } });
```
Keep the `useMocks` mock branch (return a fake `{code,expiresAt,ttlSeconds}`; no real token needed in mock).
- [ ] **Step 3:** `node --check scripts/api/qr-login.js` → clean. Confirm no `console.log` of `rt`.
- [ ] **Step 4:** Commit `feat(ui): qr create sends the web refresh token for terminal handoff`.

---

### Task 2: v2 QR payload (drop username)

**Files:** Modify `scripts/composites/qr-login-modal.js`.

- [ ] **Step 1:** In the payload builder change `{ v:1, u: email, c: res.code, env: envString() }` → `{ v:2, c: res.code, env: envString() }`. Remove the now-unused `email` read (and its `FS.session.user.email` line) if nothing else uses it.
- [ ] **Step 2:** Keep everything else (countdown, Regenerate `disabled:state.loading`, `_notFound`/`_accessDenied`/`!res.code` guard, buildQrDataUrl H-ECC). 
- [ ] **Step 3:** `node --check scripts/composites/qr-login-modal.js` → clean; grep the file for `v: 1`/`u:` → none remain.
- [ ] **Step 4:** Bump `?v=` on `qr-login-modal.js` (+ `qr-login.js`) in `app-shell-preview.html`. Commit `feat(ui): v2 QR payload (code-only, drop username)`.

---

### Task 3: Browser smoke (manual — after backend redeem live)
- [ ] Serve locally (node static server; python3 is unavailable), open Settings → "Log in a terminal" with `useMocks` on → QR renders `{v:2,c,env}`, countdown, regenerate. With backend live + authed → the QR encodes a real code; a dev terminal redeems it end-to-end.

## Self-Review
**Spec §5 coverage:** create-sends-RT → Task 1; payload `{v:2,c,env}` → Task 2. **Placeholder:** the refresh-token accessor is "grep and use the established one" — a real lookup, since cognito.js owns the session (the exact key is repo-specific; the implementer reads it). **Consistency:** `{v:2,c,env}` matches the mobile parser (v2 plan) + backend redeem (code-only).
