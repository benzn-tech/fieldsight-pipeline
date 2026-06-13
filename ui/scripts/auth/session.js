/* ==========================================================================
   FieldSight Auth · Session — token store + auto-refresh  (Sprint 8.0.2)
   --------------------------------------------------------------------------
   Holds the live Cognito tokens for the duration of a tab. Tokens live
   in MEMORY plus sessionStorage (cleared on tab close); we deliberately
   do NOT use localStorage — id/access tokens have ~1h lifetime, and
   per BACKEND-CONTEXT §7 even short-lived presigned URLs must not be
   cached in localStorage, so we apply the same hygiene to bearer tokens.

   Auto-refresh: the token-bearing fetch helper (api/_fetch.js) calls
   ensureFresh() before each request. If the id token is within the
   refresh window (default 60 s before expiry) we refresh proactively
   so the request goes out with a fresh token.

   On a 401 response, callers should hit refresh() once and retry; if
   the refresh itself fails we clear() and require a fresh signIn.

   Exported to:
     window.FS.session.{
       idToken, accessToken, refreshToken, expiresAt, user,
       isSignedIn(),
       getAccessToken(),
       set(), clear(), refresh(), ensureFresh(), onChange()
     }
   ========================================================================== */

(function () {
  'use strict';

  var KEY = 'fs.session.v1';
  var REFRESH_LEAD_MS = 60 * 1000;  /* refresh when <60 s until expiry */

  var listeners = new Set();

  var state = readFromStorage();

  function readFromStorage() {
    try {
      var raw = window.sessionStorage.getItem(KEY);
      if (!raw) return emptyState();
      var parsed = JSON.parse(raw);
      return Object.assign(emptyState(), parsed);
    } catch (e) {
      return emptyState();
    }
  }

  function emptyState() {
    return {
      idToken:      null,
      accessToken:  null,
      refreshToken: null,
      expiresAt:    0,        /* epoch ms */
      user:         null,     /* { sub, email, name, role, display_name, sites } */
    };
  }

  function writeToStorage() {
    try {
      window.sessionStorage.setItem(KEY, JSON.stringify(state));
    } catch (e) { /* private mode etc — degrade silently */ }
  }

  function emit() {
    listeners.forEach(function (cb) {
      try { cb(state); } catch (e) { console.error('[session]', e); }
    });
  }

  /* set({ idToken, accessToken, refreshToken, expiresIn, user }) */
  function set(input) {
    var now = Date.now();
    state = {
      idToken:      input.idToken      != null ? input.idToken      : state.idToken,
      accessToken:  input.accessToken  != null ? input.accessToken  : state.accessToken,
      refreshToken: input.refreshToken != null ? input.refreshToken : state.refreshToken,
      expiresAt:    input.expiresIn    != null ? now + input.expiresIn * 1000 : state.expiresAt,
      user:         input.user         != null ? input.user         : state.user,
    };
    writeToStorage();
    emit();
  }

  function clear() {
    state = emptyState();
    try { window.sessionStorage.removeItem(KEY); } catch (e) { /* noop */ }
    emit();
  }

  /* Returns the idToken (or null) after proactively refreshing if
     the token is within REFRESH_LEAD_MS of expiry. */
  async function ensureFresh() {
    if (!state.idToken) return null;
    if (Date.now() < state.expiresAt - REFRESH_LEAD_MS) return state.idToken;
    return refresh();
  }

  /* Returns the current accessToken (non-refreshing — call ensureFresh
     first if you need a guaranteed-fresh token). Used by api/_fetch.js
     to supply the Authorization: Bearer header. */
  function getAccessToken() {
    return state.accessToken;
  }

  async function refresh() {
    if (!state.refreshToken || !window.FS.cognito) {
      clear();
      return null;
    }
    try {
      var res  = await window.FS.cognito.refreshTokens(state.refreshToken);
      var auth = res.AuthenticationResult || {};
      set({
        idToken:     auth.IdToken     || state.idToken,
        accessToken: auth.AccessToken || state.accessToken,
        expiresIn:   auth.ExpiresIn   || 3600,
        /* RefreshToken is intentionally NOT returned by REFRESH_TOKEN_AUTH —
           keep the existing one. */
      });
      return state.idToken;
    } catch (err) {
      console.warn('[session] refresh failed, clearing', err && err.message);
      clear();
      return null;
    }
  }

  function onChange(cb) {
    listeners.add(cb);
    return function () { listeners.delete(cb); };
  }

  if (!window.FS) window.FS = {};
  window.FS.session = {
    /* Live state — read-only views */
    get idToken()      { return state.idToken; },
    get accessToken()  { return state.accessToken; },
    get refreshToken() { return state.refreshToken; },
    get expiresAt()    { return state.expiresAt; },
    get user()         { return state.user; },
    isSignedIn:     function () { return !!state.idToken && Date.now() < state.expiresAt; },
    getAccessToken: getAccessToken,

    /* Mutations */
    set:          set,
    clear:        clear,
    refresh:      refresh,
    ensureFresh:  ensureFresh,
    onChange:     onChange,
  };

})();
