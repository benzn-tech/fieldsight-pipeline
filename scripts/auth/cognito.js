/* ==========================================================================
   FieldSight Auth · Cognito IDP — Sprint 2.9 (Phase I) + Sprint 8.0.2
   --------------------------------------------------------------------------
   Thin wrapper over the Cognito IDP REST surface AND the OAuth2/PKCE
   Hosted UI redirect flow. Configured per BACKEND-CONTEXT §3:

     Region:    ap-southeast-2
     User pool: ap-southeast-2_ps7XIQGHB
     Client ID: 5npb81jbj1hgh9tsck25kan3os

   This module is invoked only when window.FS.api.useMocks === false.
   The prototype defaults to mocks so the preview HTMLs keep working
   without a real Cognito user.

   THREE AUTH STRATEGIES:

   1. Username+Password (direct API — for CLI / native apps):
        FS.cognito.signIn(username, password)
        FS.cognito.respondToChallenge(...)  (NEW_PASSWORD_REQUIRED)
        FS.cognito.refreshTokens(refreshToken)

   2. Hosted UI redirect (OAuth2 + PKCE — preferred for web):
        FS.auth.login()          → redirects to Cognito Hosted UI
        FS.auth.handleCallback() → exchanges code for tokens on return
        FS.auth.logout()         → clears session + redirects to logout

   3. Token refresh — shared, called automatically by session.js.

   Exported:
     window.FS.cognito.{ signIn, respondToChallenge, refreshTokens, config }
     window.FS.auth.{   login, handleCallback, logout }
   ========================================================================== */

(function () {
  'use strict';

  var DEFAULTS = {
    region:        'ap-southeast-2',
    poolId:        'ap-southeast-2_ps7XIQGHB',
    clientId:      '5npb81jbj1hgh9tsck25kan3os',
    hostedUiDomain: null,  /* e.g. 'fieldsight.auth.ap-southeast-2.amazoncognito.com' */
  };

  function config() {
    var override = window.FS_COGNITO_CONFIG || {};
    return {
      region:         override.region         || DEFAULTS.region,
      poolId:         override.poolId         || DEFAULTS.poolId,
      clientId:       override.clientId       || DEFAULTS.clientId,
      hostedUiDomain: override.hostedUiDomain || DEFAULTS.hostedUiDomain,
    };
  }

  function idpEndpoint() {
    return 'https://cognito-idp.' + config().region + '.amazonaws.com/';
  }

  function hostedUiBase() {
    var cfg = config();
    if (!cfg.hostedUiDomain) return null;
    return 'https://' + cfg.hostedUiDomain;
  }

  /* ---------- IDP JSON-1.1 calls (strategy 1 + refresh) ------------------ */

  async function idpCall(action, body) {
    var res = await fetch(idpEndpoint(), {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-amz-json-1.1',
        'X-Amz-Target': 'AWSCognitoIdentityProviderService.' + action,
      },
      body: JSON.stringify(body),
    });
    var json = null;
    try { json = await res.json(); } catch (e) { /* surface as error */ }
    if (!res.ok) {
      var err = new Error((json && json.message) || ('Cognito ' + action + ' failed: ' + res.status));
      err.code   = json && json.__type;
      err.status = res.status;
      throw err;
    }
    return json;
  }

  async function signIn(username, password) {
    var cfg = config();
    return idpCall('InitiateAuth', {
      AuthFlow:       'USER_PASSWORD_AUTH',
      ClientId:       cfg.clientId,
      AuthParameters: { USERNAME: username, PASSWORD: password },
    });
  }

  async function respondToChallenge(opts) {
    var cfg = config();
    return idpCall('RespondToAuthChallenge', {
      ChallengeName: opts.challengeName,
      ClientId:      cfg.clientId,
      Session:       opts.session,
      ChallengeResponses: {
        USERNAME:     opts.username,
        NEW_PASSWORD: opts.newPassword,
      },
    });
  }

  async function refreshTokens(refreshToken) {
    var cfg = config();
    return idpCall('InitiateAuth', {
      AuthFlow:       'REFRESH_TOKEN_AUTH',
      ClientId:       cfg.clientId,
      AuthParameters: { REFRESH_TOKEN: refreshToken },
    });
    /* Note: REFRESH_TOKEN_AUTH does NOT return a new RefreshToken — re-use
       the existing one until it expires (~30 days by default). */
  }

  /* ---------- PKCE helpers ----------------------------------------------- */

  async function generateCodeVerifier() {
    var array = new Uint8Array(32);
    window.crypto.getRandomValues(array);
    return btoa(String.fromCharCode.apply(null, array))
      .replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
  }

  async function generateCodeChallenge(verifier) {
    var encoder = new TextEncoder();
    var data    = encoder.encode(verifier);
    var digest  = await window.crypto.subtle.digest('SHA-256', data);
    return btoa(String.fromCharCode.apply(null, new Uint8Array(digest)))
      .replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
  }

  /* ---------- Hosted UI redirect (strategy 2) ----------------------------- */

  var PKCE_KEY = 'fs.pkce.v1';

  async function login(redirectUri) {
    var base = hostedUiBase();
    if (!base) {
      console.warn('[FS.auth.login] hostedUiDomain not configured in FS_COGNITO_CONFIG.');
      return;
    }
    var cfg      = config();
    var verifier = await generateCodeVerifier();
    var challenge = await generateCodeChallenge(verifier);
    var state    = String(Math.random()).slice(2);

    try { sessionStorage.setItem(PKCE_KEY, JSON.stringify({ verifier: verifier, state: state })); }
    catch (e) { /* private mode — degrade gracefully */ }

    var redirect = redirectUri || window.location.origin + window.location.pathname;
    var params = [
      'response_type=code',
      'client_id=' + encodeURIComponent(cfg.clientId),
      'redirect_uri=' + encodeURIComponent(redirect),
      'code_challenge=' + encodeURIComponent(challenge),
      'code_challenge_method=S256',
      'state=' + encodeURIComponent(state),
    ].join('&');

    window.location.href = base + '/login?' + params;
  }

  async function handleCallback(redirectUri) {
    var base = hostedUiBase();
    if (!base) return null;

    var cfg    = config();
    var search = new URLSearchParams(window.location.search);
    var code   = search.get('code');
    var state  = search.get('state');
    if (!code) return null;

    var stored;
    try { stored = JSON.parse(sessionStorage.getItem(PKCE_KEY) || 'null'); }
    catch (e) { stored = null; }

    if (!stored || stored.state !== state) {
      console.warn('[FS.auth.handleCallback] state mismatch — possible CSRF.');
      return null;
    }

    try { sessionStorage.removeItem(PKCE_KEY); } catch (e) { /* noop */ }

    var redirect = redirectUri || window.location.origin + window.location.pathname;

    var body = new URLSearchParams({
      grant_type:    'authorization_code',
      client_id:     cfg.clientId,
      code:          code,
      redirect_uri:  redirect,
      code_verifier: stored.verifier,
    });

    var res = await fetch(base + '/oauth2/token', {
      method:  'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body:    body.toString(),
    });

    if (!res.ok) {
      var json = null;
      try { json = await res.json(); } catch (e) { /* noop */ }
      throw new Error((json && json.error_description) || 'Token exchange failed: ' + res.status);
    }

    return res.json();
    /* Caller (LoginScreen / SessionGate) should call FS.session.set({
       idToken, accessToken, refreshToken, expiresIn }) with the result. */
  }

  function logout(logoutUri) {
    /* Clear local session first. */
    if (window.FS && window.FS.session) window.FS.session.clear();

    var base = hostedUiBase();
    if (!base) {
      /* No Hosted UI configured — just navigate to root. */
      window.location.href = logoutUri || window.location.origin;
      return;
    }
    var cfg   = config();
    var dest  = logoutUri || window.location.origin;
    window.location.href = base + '/logout'
      + '?client_id=' + encodeURIComponent(cfg.clientId)
      + '&logout_uri=' + encodeURIComponent(dest);
  }

  if (!window.FS) window.FS = {};

  window.FS.cognito = {
    signIn:             signIn,
    respondToChallenge: respondToChallenge,
    refreshTokens:      refreshTokens,
    config:             config,
  };

  window.FS.auth = {
    login:          login,
    handleCallback: handleCallback,
    logout:         logout,
  };

})();
