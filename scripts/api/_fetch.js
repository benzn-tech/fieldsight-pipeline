/* ==========================================================================
   FieldSight API · _fetch — shared real-backend helper
   --------------------------------------------------------------------------
   Used by every FS.api.* module when window.FS.api.useMocks === false.

   Responsibilities:
     • Attach Authorization: Bearer <accessToken>
     • Auto-refresh the token when it's within the refresh window
     • CloudFront SPA-fallback trap (BACKEND-CONTEXT §8.2 / BUG-20):
       a 404 may arrive as 200 with content-type:text/html. Always
       inspect content-type; if it isn't application/json, treat the
       response as { _notFound: true }.
     • Exponential retry (3 attempts, 1s/2s/4s) on 5xx and network errors.
     • Per-request 10 s timeout (overridable via opts.timeoutMs).
     • X-Request-Id header (UUID v4) for server-side correlation.
     • Retry once on 401 after refreshing the session.
     • Surface 403 with role-aware payload (BACKEND-CONTEXT §8.4):
       returns { _accessDenied: true, error } so the UI can render an
       empathetic state instead of a generic toast.
     • Honour the no-localStorage rule for tokens — those live in
       sessionStorage via FS.session.

   Exported to:
     window.FS.api.request(path, opts)
       opts: {
         method:    'GET' | 'POST' | 'PATCH' | 'DELETE' | 'PUT'
         params:    object → query string (skips null/undefined)
         body:      object → JSON-encoded body
         signal:    AbortSignal
         allowAnon: boolean — skip auth header (e.g. /api/health)
         timeoutMs: number — per-request timeout in ms (default 10000)
       }
       → resolves to either:
            the JSON body, or
            { _notFound: true,     status, raw }, or
            { _accessDenied: true, status, error }
          and rejects on transport / unexpected errors.

     window.FS.api.setBaseUrl(url)
       Override the base URL at runtime (e.g. point at staging vs local).
   ========================================================================== */

(function () {
  'use strict';

  var DEFAULT_TIMEOUT_MS = 10000;
  var RETRY_DELAYS_MS    = [1000, 2000, 4000];

  /* ---------- Utilities --------------------------------------------------- */

  function buildQuery(params) {
    if (!params) return '';
    var parts = [];
    Object.keys(params).forEach(function (k) {
      var v = params[k];
      if (v == null || v === '') return;
      parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(v));
    });
    return parts.length ? '?' + parts.join('&') : '';
  }

  function uuidV4() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
      var r = Math.random() * 16 | 0;
      var v = c === 'x' ? r : (r & 0x3 | 0x8);
      return v.toString(16);
    });
  }

  /* BUG-20: CloudFront may serve a cached HTML shell as a 200 even for
     JSON-endpoint paths. Also guard against text/html arriving on any
     status (2xx or otherwise) — never trust an HTML body. */
  function isJsonResponse(res) {
    var ct = (res.headers.get && res.headers.get('content-type')) || '';
    /* Explicit text/html check (including status-200 CF trap): */
    if (ct.indexOf('text/html') !== -1) return false;
    return ct.indexOf('application/json') !== -1;
  }

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  /* ---------- Transport-level fetch with timeout + retry ------------------ */

  /* One low-level fetch attempt with a per-request timeout.
     Returns the Response or throws on timeout/network error. */
  async function fetchWithTimeout(url, fetchOpts, timeoutMs) {
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, timeoutMs);

    /* Merge caller's AbortSignal with our timeout signal. */
    var callerSignal = fetchOpts.signal;
    if (callerSignal) {
      callerSignal.addEventListener('abort', function () { controller.abort(); });
    }

    try {
      return await fetch(url, Object.assign({}, fetchOpts, { signal: controller.signal }));
    } finally {
      clearTimeout(timer);
    }
  }

  /* Retry wrapper: retries on 5xx or network-level errors.
     4xx responses are returned immediately (caller decides). */
  async function fetchWithRetry(url, fetchOpts, timeoutMs) {
    var maxAttempts = RETRY_DELAYS_MS.length + 1;
    var lastErr;
    for (var attempt = 0; attempt < maxAttempts; attempt++) {
      try {
        var res = await fetchWithTimeout(url, fetchOpts, timeoutMs);
        if (res.status < 500) return res;          /* 1xx-4xx — no retry */
        lastErr = new Error('HTTP ' + res.status);
        lastErr.status = res.status;
        lastErr._response = res;                   /* carry body for callers */
      } catch (err) {
        lastErr = err;
        /* AbortError from caller signal should not be retried. */
        if (err.name === 'AbortError' && fetchOpts.signal && fetchOpts.signal.aborted) {
          throw err;
        }
      }
      if (attempt < maxAttempts - 1) {
        await sleep(RETRY_DELAYS_MS[attempt]);
      }
    }
    /* All attempts exhausted — surface the last 5xx response if available,
       otherwise throw the transport error. */
    if (lastErr && lastErr._response) return lastErr._response;
    throw lastErr;
  }

  /* ---------- Auth-aware request ----------------------------------------- */

  async function rawRequest(path, opts) {
    opts = opts || {};
    var base = (window.FS && window.FS.api && window.FS.api.baseUrl) || '/api';
    var url  = base + path + buildQuery(opts.params);

    var headers = Object.assign({}, opts.headers || {});

    headers['X-Request-Id'] = uuidV4();

    if (opts.body !== undefined && opts.body !== null && !(opts.body instanceof FormData)) {
      headers['Content-Type'] = 'application/json';
    }

    if (!opts.allowAnon) {
      var session = window.FS && window.FS.session;
      if (session) {
        var token = await session.ensureFresh();
        if (token) {
          /* Prefer accessToken for Bearer if available (API Gateway validates it).
             ensureFresh returns idToken; use getAccessToken() when present. */
          var bearer = (session.getAccessToken && session.getAccessToken()) || token;
          headers['Authorization'] = 'Bearer ' + bearer;
        }
      }
    }

    var timeoutMs = opts.timeoutMs != null ? opts.timeoutMs : DEFAULT_TIMEOUT_MS;

    return fetchWithRetry(url, {
      method:  opts.method || 'GET',
      headers: headers,
      body:    opts.body !== undefined && opts.body !== null
                 ? (opts.body instanceof FormData ? opts.body : JSON.stringify(opts.body))
                 : undefined,
      signal:  opts.signal,
    }, timeoutMs);
  }

  async function request(path, opts) {
    opts = opts || {};
    var res = await rawRequest(path, opts);

    /* 401 — refresh once and retry. */
    if (res.status === 401 && !opts._retried && window.FS.session) {
      var refreshed = await window.FS.session.refresh();
      if (refreshed) {
        return request(path, Object.assign({}, opts, { _retried: true }));
      }
      return { _accessDenied: true, status: 401,
               error: 'Sign-in expired. Please sign in again.' };
    }

    /* BUG-20: Never trust an HTML body — catches CF cached-HTML 200 trap. */
    if (!isJsonResponse(res)) {
      return { _notFound: true, status: res.status };
    }

    var body = null;
    try { body = await res.json(); } catch (e) { /* fallthrough */ }

    if (res.status === 403) {
      return {
        _accessDenied: true,
        status:        403,
        error:         (body && body.error) || 'Access denied.',
      };
    }
    if (res.status === 404) {
      return { _notFound: true, status: 404, raw: body };
    }
    if (!res.ok) {
      var err = new Error((body && body.message) || ('HTTP ' + res.status));
      err.status = res.status;
      err.body   = body;
      throw err;
    }

    return body;
  }

  function setBaseUrl(url) {
    if (!window.FS) window.FS = {};
    if (!window.FS.api) window.FS.api = {};
    window.FS.api.baseUrl = url;
  }

  if (!window.FS) window.FS = {};
  if (!window.FS.api) window.FS.api = {};
  window.FS.api.request   = request;
  window.FS.api.setBaseUrl = setBaseUrl;

})();
