/* ==========================================================================
   FieldSight LoginScreen — Layer 5 composite (Sprint 2.9 / Phase I)
   --------------------------------------------------------------------------
   Cognito USER_PASSWORD_AUTH + NEW_PASSWORD_REQUIRED challenge flow.

   Used only when window.FS.api.useMocks === false. The shell mounts
   this in place of AppShell whenever FS.session has no live token.

   Two screens, driven by local state:
     • 'signIn'   — username + password form
     • 'newPwd'   — set new password (Cognito's first-login flow)

   On successful authentication we populate FS.session via .set() and
   then call /api/sites to derive the role + display_name (since the
   Cognito JWT alone doesn't carry our app-specific role payload).
   The shell observes session.onChange and re-renders past this screen
   automatically.

   Props:
     onSignedIn  () => void  — optional callback after successful sign-in

   Exported to:
     window.FieldSight.LoginScreen
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function LoginScreen(props) {
    var Button = window.FieldSight.Button;

    var refMode = React.useState('signIn');  /* 'signIn' | 'newPwd' */
    var mode    = refMode[0];
    var setMode = refMode[1];

    var refForm = React.useState({ username: '', password: '', newPassword: '' });
    var form    = refForm[0];
    var setForm = refForm[1];

    var refSession = React.useState({ challengeSession: null });
    var pending    = refSession[0];
    var setPending = refSession[1];

    var refStatus = React.useState({ phase: 'idle' });
    var status    = refStatus[0];
    var setStatus = refStatus[1];

    function update(field, val) {
      setForm(function (f) {
        var next = Object.assign({}, f);
        next[field] = val;
        return next;
      });
    }

    /* After tokens are set, hydrate the user payload via /api/sites
       (BACKEND-CONTEXT §4.2 — returns role + display_name). */
    async function hydrateUser() {
      try {
        var res = await window.FS.api.sites.getSites();
        window.FS.session.set({
          user: {
            role:         res.role,
            display_name: res.display_name,
          },
        });
      } catch (err) {
        console.warn('[login] could not load /api/sites for user payload', err);
      }
    }

    async function onSubmitSignIn(e) {
      e.preventDefault();
      if (!form.username || !form.password) return;
      setStatus({ phase: 'submitting' });
      try {
        var res = await window.FS.cognito.signIn(form.username, form.password);
        if (res.ChallengeName === 'NEW_PASSWORD_REQUIRED') {
          setPending({ challengeSession: res.Session });
          setMode('newPwd');
          setStatus({ phase: 'idle' });
          return;
        }
        var auth = res.AuthenticationResult || {};
        window.FS.session.set({
          idToken:      auth.IdToken,
          accessToken:  auth.AccessToken,
          refreshToken: auth.RefreshToken,
          expiresIn:    auth.ExpiresIn || 3600,
        });
        await hydrateUser();
        setStatus({ phase: 'done' });
        if (props.onSignedIn) props.onSignedIn();
      } catch (err) {
        setStatus({ phase: 'error', error: err });
      }
    }

    async function onSubmitNewPassword(e) {
      e.preventDefault();
      if (!form.newPassword) return;
      setStatus({ phase: 'submitting' });
      try {
        var res = await window.FS.cognito.respondToChallenge({
          challengeName: 'NEW_PASSWORD_REQUIRED',
          session:       pending.challengeSession,
          username:      form.username,
          newPassword:   form.newPassword,
        });
        var auth = res.AuthenticationResult || {};
        window.FS.session.set({
          idToken:      auth.IdToken,
          accessToken:  auth.AccessToken,
          refreshToken: auth.RefreshToken,
          expiresIn:    auth.ExpiresIn || 3600,
        });
        await hydrateUser();
        setStatus({ phase: 'done' });
        if (props.onSignedIn) props.onSignedIn();
      } catch (err) {
        setStatus({ phase: 'error', error: err });
      }
    }

    var submitting = status.phase === 'submitting';
    var errMessage = status.phase === 'error'
      ? (status.error && status.error.message) || 'Sign-in failed'
      : null;

    return React.createElement('div', { className: 'fs-login' },
      React.createElement('div', { className: 'fs-login__card' },
        React.createElement('div', { className: 'fs-login__brand' }, 'FieldSight'),
        React.createElement('div', { className: 'fs-login__caption' },
          mode === 'signIn'
            ? 'Sign in with your FieldSight account'
            : 'Set a new password to finish signing in'),

        mode === 'signIn'
          ? React.createElement('form', {
              className: 'fs-login__form', onSubmit: onSubmitSignIn,
            },
              React.createElement('label', { className: 'fs-login__field' },
                React.createElement('span', { className: 'fs-login__label' }, 'Email'),
                React.createElement('input', {
                  type:  'email', autoComplete: 'username',
                  value: form.username,
                  onChange: function (e) { update('username', e.target.value); },
                  className: 'fs-login__input',
                  required: true,
                }),
              ),
              React.createElement('label', { className: 'fs-login__field' },
                React.createElement('span', { className: 'fs-login__label' }, 'Password'),
                React.createElement('input', {
                  type:  'password', autoComplete: 'current-password',
                  value: form.password,
                  onChange: function (e) { update('password', e.target.value); },
                  className: 'fs-login__input',
                  required: true,
                }),
              ),
              errMessage ? React.createElement('div', {
                className: 'fs-login__error',
              }, errMessage) : null,
              React.createElement(Button, {
                type:     'submit',
                disabled: submitting,
                size:     'md',
              }, submitting ? 'Signing in…' : 'Sign in'),
            )
          : React.createElement('form', {
              className: 'fs-login__form', onSubmit: onSubmitNewPassword,
            },
              React.createElement('div', { className: 'fs-login__hint' },
                'Signing in as ' + form.username),
              React.createElement('label', { className: 'fs-login__field' },
                React.createElement('span', { className: 'fs-login__label' }, 'New password'),
                React.createElement('input', {
                  type: 'password', autoComplete: 'new-password',
                  value: form.newPassword,
                  onChange: function (e) { update('newPassword', e.target.value); },
                  className: 'fs-login__input',
                  required: true,
                  minLength: 8,
                }),
              ),
              errMessage ? React.createElement('div', {
                className: 'fs-login__error',
              }, errMessage) : null,
              React.createElement(Button, {
                type:     'submit',
                disabled: submitting,
                size:     'md',
              }, submitting ? 'Updating…' : 'Set password and continue'),
            ),
      ),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.LoginScreen = LoginScreen;
})();
