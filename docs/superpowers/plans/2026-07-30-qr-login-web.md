# QR Passwordless Sign-In — Web Implementation Plan (fieldsight-ui)

> **For agentic workers:** the `ui/` subtree has **no build step and no test harness** (see `ui/CLAUDE.md`): verify with `node --check` on each changed JS, browser smoke-test, and bump `?v=N` cache-busters. Steps use checkbox (`- [ ]`) syntax. There is no TDD loop here — each task ends with a manual verification step.

**Goal:** Add a "Log in a terminal" affordance in the web app that mints a one-time code (`POST /api/org/auth/qr/create`) and renders it as a QR the terminal scans.

**Architecture:** A tiny API method (`FS.api.qrLogin.create()`) over the existing token-bearing `FS.api.request`, a vendored pure-JS QR generator, and a composite modal that draws the QR + a 90 s countdown + "Regenerate". Opened from a button on the Settings page.

**Tech Stack:** Vanilla JS + browser React (Babel), BEM CSS + `tokens.css`, `FS.api.request`, `FS.session`. No npm/build.

**Depends on:** the backend plan's `POST /api/org/auth/qr/create` being live (test then prod). Until then, develop against `useMocks` (mock code) — see Task 2.

**Design spec:** `../specs/...` → the canonical spec is in the GrandTime repo:
`C:/Users/camil/Dropbox/GrandTime/docs/superpowers/specs/2026-07-30-qr-login-design.md` (§10 Web).

## Global Constraints

- **No build step / no npm** (`ui/CLAUDE.md`). Vendor the QR lib as a single committed file.
- **Tokens only** for colour/spacing; **BEM** class names `.fs-{block}__{element}--{modifier}`.
- **Bump `?v=N`** cache-busters on any preview HTML that loads a changed/added `.js`/`.css`.
- **Token-bearing calls** go through `FS.api.request` (it attaches `Authorization`, refreshes, handles 401/403). org base already includes `/api/org`, so the path is `'/auth/qr/create'`.
- **QR content** = `{"v":1,"u":<FS.session.user.email>,"c":<code>,"env":<env>}`. High error-correction (`H`), **no** centre logo.
- **`env`** = the web's environment string: `"prod"` for the client site. (If a single build serves both, derive from the API base — but v1 web = prod.)
- **Never** persist the code (no localStorage); it lives only in the modal's React state for its 90 s life.

---

## File Structure

- **Create:** `ui/scripts/vendor/qrcode-generator.js` — vendored MIT QR encoder (Kazuhiko Arase, single file).
- **Create:** `ui/scripts/api/qr-login.js` — `FS.api.qrLogin.create()`.
- **Create:** `ui/scripts/composites/qr-login-modal.js` — the modal (QR canvas + countdown + regenerate).
- **Create:** `ui/styles/… ` (or extend `components.css`) — `.fs-qr-login*` styles.
- **Modify:** `ui/scripts/pages/settings.js` — a "Log in a terminal" button that opens the modal.
- **Modify:** the preview/shell HTML(s) that list script tags (e.g. `ui/app-shell-preview.html`) — add the 3 new scripts + bump `?v=`.

---

### Task 1: Vendor the QR generator

**Files:** Create `ui/scripts/vendor/qrcode-generator.js`

- [ ] **Step 1: Add the library**

Vendor **qrcode-generator** (MIT, Kazuhiko Arase) — the single-file UMD build `qrcode.js` from `github.com/kazuhikoarase/qrcode-generator/tree/master/js`. Save it verbatim as `ui/scripts/vendor/qrcode-generator.js`. It exposes a global `qrcode(typeNumber, errorCorrectionLevel)` factory with `.addData(str)`, `.make()`, and `.createDataURL(cellSize, margin)` / `.createSvgTag(...)`.

Keep the file unmodified (a vendored dependency). Prepend a one-line comment: `/* vendored: qrcode-generator MIT — do not edit. https://github.com/kazuhikoarase/qrcode-generator */`.

- [ ] **Step 2: Verify it loads**

Run: `node --check ui/scripts/vendor/qrcode-generator.js`
Expected: no output (valid JS). (It's UMD; under `node --check` we only confirm it parses.)

- [ ] **Step 3: Commit**

```bash
git add ui/scripts/vendor/qrcode-generator.js
git commit -m "chore(ui): vendor qrcode-generator (MIT) for terminal login QR"
```

---

### Task 2: API method `FS.api.qrLogin.create()`

**Files:** Create `ui/scripts/api/qr-login.js`

**Interfaces:**
- Produces: `window.FS.api.qrLogin.create()` → resolves to `{ code, expiresAt, ttlSeconds }`.

- [ ] **Step 1: Write the module**

Create `ui/scripts/api/qr-login.js`:

```javascript
/* FieldSight API · qr-login — mint a one-time terminal login code.
   POST /api/org/auth/qr/create (authed). Mirrors the useMocks pattern of the
   other api/* modules. */
(function () {
  'use strict';
  if (!window.FS) window.FS = {};
  if (!window.FS.api) window.FS.api = {};

  async function create() {
    if (window.FS.api.useMocks) {
      /* Dev/mock: a fake code so the modal renders before the backend is live. */
      var now = Math.floor(Date.now() / 1000);
      return { code: 'MOCK-' + uuidish(), expiresAt: now + 90, ttlSeconds: 90 };
    }
    return window.FS.api.request('/auth/qr/create', { method: 'POST', body: {} });
  }

  function uuidish() {
    return (window.crypto && window.crypto.randomUUID)
      ? window.crypto.randomUUID()
      : String(Math.random()).slice(2);
  }

  window.FS.api.qrLogin = { create: create };
})();
```

- [ ] **Step 2: Verify**

Run: `node --check ui/scripts/api/qr-login.js`
Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add ui/scripts/api/qr-login.js
git commit -m "feat(ui): FS.api.qrLogin.create — mint terminal login code"
```

---

### Task 3: The QR-login modal composite

**Files:**
- Create `ui/scripts/composites/qr-login-modal.js`
- Modify `ui/styles/components.css` (append `.fs-qr-login*`)

**Interfaces:**
- Consumes: `FS.api.qrLogin.create`, `FS.session.user.email`, global `qrcode`.
- Produces: `window.FieldSight.QrLoginModal` (React component taking `{ onClose }`).

- [ ] **Step 1: Write the component**

Create `ui/scripts/composites/qr-login-modal.js`:

```javascript
/* FieldSight composite · QrLoginModal — shows a scannable terminal-login QR
   with a live 90s countdown and a Regenerate action. */
(function () {
  'use strict';
  var e = React.createElement;

  function envString() {
    /* v1 web = prod. If a single build ever serves both, derive from the API base. */
    return (window.FS_ENV && window.FS_ENV.name) || 'prod';
  }

  function buildQrDataUrl(payloadObj) {
    var qr = window.qrcode(0, 'H');           /* type 0 = auto-fit; H = high ECC */
    qr.addData(JSON.stringify(payloadObj));
    qr.make();
    return qr.createDataURL(6, 4);            /* cellSize 6px, margin 4 cells */
  }

  function QrLoginModal(props) {
    var email = (window.FS.session && window.FS.session.user && window.FS.session.user.email) || '';
    var st = React.useState({ loading: true, error: null, dataUrl: null, expiresAt: 0 });
    var state = st[0], setState = st[1];
    var remaining = React.useState(0);
    var setRemaining = remaining[1];

    function refresh() {
      setState({ loading: true, error: null, dataUrl: null, expiresAt: 0 });
      window.FS.api.qrLogin.create().then(function (res) {
        if (res && res._accessDenied) { setState({ loading: false, error: res.error }); return; }
        var payload = { v: 1, u: email, c: res.code, env: envString() };
        setState({ loading: false, error: null, dataUrl: buildQrDataUrl(payload), expiresAt: res.expiresAt });
      }).catch(function (err) {
        setState({ loading: false, error: (err && err.message) || 'Could not create a login code' });
      });
    }

    React.useEffect(function () { refresh(); }, []);

    /* Countdown tick. */
    React.useEffect(function () {
      if (!state.expiresAt) return;
      var t = setInterval(function () {
        var left = Math.max(0, state.expiresAt - Math.floor(Date.now() / 1000));
        setRemaining(left);
      }, 1000);
      return function () { clearInterval(t); };
    }, [state.expiresAt]);

    var left = Math.max(0, state.expiresAt - Math.floor(Date.now() / 1000));
    var expired = state.expiresAt && left <= 0;

    return e('div', { className: 'fs-qr-login' },
      e('h2', { className: 'fs-qr-login__title' }, 'Log in a terminal'),
      e('p', { className: 'fs-qr-login__hint' },
        'On the terminal, tap “Scan QR to Sign in” and point it at this code. Expires in 90 seconds.'),
      state.loading ? e('div', { className: 'fs-qr-login__status' }, 'Generating…') :
      state.error   ? e('div', { className: 'fs-qr-login__status fs-qr-login__status--error' }, state.error) :
        e('div', { className: 'fs-qr-login__code' },
          e('img', { className: 'fs-qr-login__img' + (expired ? ' fs-qr-login__img--expired' : ''),
                     src: state.dataUrl, alt: 'Login QR code', width: 240, height: 240 }),
          e('div', { className: 'fs-qr-login__ttl' },
            expired ? 'Expired' : ('Expires in ' + left + 's'))
        ),
      e('div', { className: 'fs-qr-login__actions' },
        e('button', { className: 'fs-btn fs-btn--secondary', onClick: refresh }, 'Regenerate'),
        e('button', { className: 'fs-btn', onClick: props.onClose }, 'Done')
      )
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.QrLoginModal = QrLoginModal;
})();
```

> The `left`/`remaining` split above keeps the countdown re-rendering each second; the implementer may simplify to a single state var if preferred — behaviour (a 1 Hz countdown + expired state) is what matters.

- [ ] **Step 2: Add styles**

Append to `ui/styles/components.css` (tokens only, both themes inherit via tokens):

```css
.fs-qr-login { display: flex; flex-direction: column; gap: var(--space-4); padding: var(--space-5); max-width: 360px; }
.fs-qr-login__title { font: var(--type-title); color: var(--text-primary); }
.fs-qr-login__hint  { font: var(--type-body-sm); color: var(--text-secondary); }
.fs-qr-login__code  { display: flex; flex-direction: column; align-items: center; gap: var(--space-2); }
.fs-qr-login__img   { border-radius: var(--radius-md); background: #fff; padding: var(--space-2); }
.fs-qr-login__img--expired { opacity: 0.3; }
.fs-qr-login__ttl   { font: var(--type-body-sm); color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.fs-qr-login__status { padding: var(--space-4); color: var(--text-secondary); }
.fs-qr-login__status--error { color: var(--color-danger-700); }
.fs-qr-login__actions { display: flex; gap: var(--space-3); justify-content: flex-end; }
```
(Use the exact token names present in `styles/tokens.css`; adjust any that differ — grep `tokens.css` for `--space-`, `--type-`, `--radius-`, `--text-`, `--color-danger` before finalising.)

- [ ] **Step 3: Verify**

Run: `node --check ui/scripts/composites/qr-login-modal.js`
Expected: no output.

- [ ] **Step 4: Commit**

```bash
git add ui/scripts/composites/qr-login-modal.js ui/styles/components.css
git commit -m "feat(ui): QrLoginModal composite (QR + 90s countdown + regenerate)"
```

---

### Task 4: Settings entry + script registration

**Files:**
- Modify `ui/scripts/pages/settings.js`
- Modify `ui/app-shell-preview.html` (+ any other shell/preview HTML that lists script tags)

- [ ] **Step 1: Add the button + modal wiring in Settings**

In `settings.js`, inside `SettingsMiddleColumn` (or the appropriate section), add a row/button that opens the modal. Use a local `React.useState` for open/closed and render `FieldSight.QrLoginModal` inside the app's existing modal-overlay composite (grep `settings.js` / `composites/modal-overlay.js` for the established modal pattern — mirror how another settings action opens a modal). Sketch:

```javascript
var modal = React.useState(false);
var showQr = modal[0], setShowQr = modal[1];
// ...in the render, a settings row:
e('button', { className: 'fs-btn fs-btn--secondary', onClick: function () { setShowQr(true); } },
  'Log in a terminal'),
// ...and conditionally the overlay:
showQr && e(window.FieldSight.ModalOverlay, { onClose: function () { setShowQr(false); } },
  e(window.FieldSight.QrLoginModal, { onClose: function () { setShowQr(false); } }))
```
Match the exact overlay component name/props used elsewhere in the repo (do not invent one).

- [ ] **Step 2: Register the new scripts in the shell HTML**

In `app-shell-preview.html` (and any deployed shell HTML that enumerates `scripts/*`), add — in load order, **before** `settings.js` and after React — with a fresh cache-buster:
```html
<script src="scripts/vendor/qrcode-generator.js?v=1"></script>
<script src="scripts/api/qr-login.js?v=1"></script>
<script src="scripts/composites/qr-login-modal.js?v=1"></script>
```
Bump the `?v=` on `settings.js` and `components.css` (they changed).

- [ ] **Step 3: Verify (manual — no test harness)**

Run: `node --check ui/scripts/pages/settings.js`
Then browser smoke-test:
```bash
cd ui && python3 -m http.server 8765   # open http://localhost:8765/app-shell-preview.html?dev=1
```
- Sign in (or dev role), open Settings → "Log in a terminal".
- With `useMocks` on: a QR renders, countdown ticks 90→0, "Regenerate" resets it, "Expired" shows at 0.
- With the backend live (`useMocks` off, authed): the QR encodes the real `{v,u,c,env}`; scan it with a dev terminal (mobile plan Task 4) → the terminal signs in.

- [ ] **Step 4: Commit**

```bash
git add ui/scripts/pages/settings.js ui/app-shell-preview.html
git commit -m "feat(ui): Settings → Log in a terminal (opens QR modal)"
```

---

## Self-Review

**Spec coverage** (§10 Web): create call → Task 2; QR render (H ECC, no logo) + 90 s countdown + regenerate → Task 3; no-build vendored generator → Task 1; Settings entry → Task 4; payload `{v,u,c,env}` with `u` from `FS.session.user.email` → Task 3.

**Placeholder scan:** Task 4 Step 1 intentionally says "mirror the repo's modal-overlay pattern / do not invent a component name" — this is a *follow-existing-pattern* instruction (the repo already has `composites/modal-overlay.js`), not an unwritten blank; the button + conditional-render code is given. Token names in Task 3 Step 2 are flagged to verify against `tokens.css` (they vary by repo).

**Consistency:** payload key order/shape `{v:1,u,c,env}` matches the mobile parser (`QrLoginParser`) and the spec §4. `FS.api.qrLogin.create()` returns `{code,expiresAt,ttlSeconds}` — the exact shape the backend create endpoint returns (backend plan Task 2).

**Cross-plan dependency:** real (non-mock) verification needs the backend create endpoint live; `useMocks` path lets the UI be built + demoed first.
