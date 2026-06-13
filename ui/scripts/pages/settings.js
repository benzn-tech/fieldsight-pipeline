/* ==========================================================================
   FieldSight Settings Page — Sprint 7.3 + 7.6
   --------------------------------------------------------------------------
   /settings — user preferences: theme + density + default landing override.

   Middle column:
     • Section 1 — Theme: Light / Dark / Auto radio group
       Calls FS.theme.set(mode) instantly; "Auto" shows resolved mode.
     • Section 2 — Density: Comfortable / Compact radio group (Sprint 7.6)
       Calls FS.density.set(mode) instantly; applies data-density on <html>.
     • Section 3 — Default landing: dropdown of visible nav items.
       First option unsets the override (role default).
       Persists to localStorage['fs.settings.defaultLanding'].

   Right detail:
     • Static summary card: current theme, density, and effective landing.

   All prefs are localStorage-only; documented as Sprint 8+ migration
   target when /api/user/prefs lands.

   Registers as window.FieldSight.PAGES['/settings']
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  var LANDING_KEY = 'fs.settings.defaultLanding';

  /* ---------- localStorage helpers -------------------------------------- */

  function readLandingOverride() {
    try { return localStorage.getItem(LANDING_KEY) || null; } catch (_) { return null; }
  }

  function writeLandingOverride(path) {
    try {
      if (path) localStorage.setItem(LANDING_KEY, path);
      else localStorage.removeItem(LANDING_KEY);
    } catch (_) {}
  }

  function readThemeStored() {
    return window.FS && window.FS.theme ? window.FS.theme.getStored() : 'auto';
  }

  function readThemeResolved() {
    return window.FS && window.FS.theme ? window.FS.theme.get() : 'light';
  }

  function readDensityStored() {
    return window.FS && window.FS.density ? window.FS.density.getStored() : 'comfortable';
  }

  /* ---------- SettingsContext ------------------------------------------ */

  var SettingsContext = React.createContext(null);

  function SettingsProvider(props) {
    var refState = React.useState(function () {
      return {
        themeStored:     readThemeStored(),
        themeResolved:   readThemeResolved(),
        densityStored:   readDensityStored(),
        landingOverride: readLandingOverride(),
      };
    });
    var state    = refState[0];
    var setState = refState[1];

    function handleSetTheme(mode) {
      if (window.FS && window.FS.theme) window.FS.theme.set(mode);
      setState(function (s) {
        return Object.assign({}, s, {
          themeStored:   readThemeStored(),
          themeResolved: readThemeResolved(),
        });
      });
    }

    function handleSetDensity(mode) {
      if (window.FS && window.FS.density) window.FS.density.set(mode);
      setState(function (s) {
        return Object.assign({}, s, { densityStored: readDensityStored() });
      });
    }

    function handleSetLanding(path) {
      writeLandingOverride(path || null);
      setState(function (s) { return Object.assign({}, s, { landingOverride: path || null }); });
    }

    var user = (window.AuthMock && window.AuthMock.currentUser) || {};
    var ctx = {
      state:        state,
      user:         user,
      setTheme:     handleSetTheme,
      setDensity:   handleSetDensity,
      setLanding:   handleSetLanding,
    };
    return React.createElement(SettingsContext.Provider, { value: ctx }, props.children);
  }

  /* ---------- SettingsMiddleColumn -------------------------------------- */

  function SettingsMiddleColumn() {
    var ctx = React.useContext(SettingsContext);
    if (!ctx) return null;

    var state        = ctx.state;
    var user         = ctx.user;
    var setTheme     = ctx.setTheme;
    var setDensity   = ctx.setDensity;
    var setLanding   = ctx.setLanding;

    var visibleItems = window.FS && window.FS.getVisibleNavItems
      ? window.FS.getVisibleNavItems(user)
      : [];
    var roleDefault = window.FS && window.FS.getDefaultLanding
      ? window.FS.getDefaultLanding(user)
      : '/today';

    var themeOptions = [
      { value: 'light', label: 'Light', caption: null },
      { value: 'dark',  label: 'Dark',  caption: null },
      {
        value:   'auto',
        label:   'Auto',
        caption: 'Matches your system, currently ' + state.themeResolved,
      },
    ];

    var densityOptions = [
      {
        value:   'comfortable',
        label:   'Comfortable',
        caption: 'Default spacing — optimised for field use with gloves.',
      },
      {
        value:   'compact',
        label:   'Compact',
        caption: 'Reduced row height and padding — fits more on screen.',
      },
    ];

    return React.createElement('div', { className: 'fs-settings' },

      React.createElement('div', { className: 'fs-settings__header' },
        React.createElement('h2', { className: 'fs-settings__title' }, 'Settings'),
        React.createElement('div', { className: 'fs-settings__subtitle' }, 'App preferences'),
      ),

      /* ---- Section 1: Theme ---- */
      React.createElement('section', { className: 'fs-settings__section' },
        React.createElement('div', { className: 'fs-settings__section-title' }, 'Theme'),
        React.createElement('div', { className: 'fs-settings__section-desc' },
          'Choose how FieldSight appears to you.'),
        React.createElement('div', { className: 'fs-settings__radio-group', role: 'radiogroup', 'aria-label': 'Theme' },
          themeOptions.map(function (opt) {
            var checked = state.themeStored === opt.value;
            return React.createElement('label', {
              key:       opt.value,
              className: 'fs-settings__radio-row' + (checked ? ' fs-settings__radio-row--checked' : ''),
            },
              React.createElement('input', {
                type:     'radio',
                name:     'fs-theme',
                value:    opt.value,
                checked:  checked,
                onChange: function () { setTheme(opt.value); },
                className: 'fs-settings__radio-input',
              }),
              React.createElement('div', { className: 'fs-settings__radio-text' },
                React.createElement('span', { className: 'fs-settings__radio-label' }, opt.label),
                opt.caption
                  ? React.createElement('span', { className: 'fs-settings__radio-caption' }, opt.caption)
                  : null,
              ),
            );
          }),
        ),
      ),

      /* ---- Section 2: Density ---- */
      React.createElement('section', { className: 'fs-settings__section' },
        React.createElement('div', { className: 'fs-settings__section-title' }, 'Display density'),
        React.createElement('div', { className: 'fs-settings__section-desc' },
          'Control how much information fits on screen at once.'),
        React.createElement('div', { className: 'fs-settings__radio-group', role: 'radiogroup', 'aria-label': 'Display density' },
          densityOptions.map(function (opt) {
            var checked = state.densityStored === opt.value;
            return React.createElement('label', {
              key:       opt.value,
              className: 'fs-settings__radio-row' + (checked ? ' fs-settings__radio-row--checked' : ''),
            },
              React.createElement('input', {
                type:     'radio',
                name:     'fs-density',
                value:    opt.value,
                checked:  checked,
                onChange: function () { setDensity(opt.value); },
                className: 'fs-settings__radio-input',
              }),
              React.createElement('div', { className: 'fs-settings__radio-text' },
                React.createElement('span', { className: 'fs-settings__radio-label' }, opt.label),
                React.createElement('span', { className: 'fs-settings__radio-caption' }, opt.caption),
              ),
            );
          }),
        ),
      ),

      /* ---- Section 3: Default landing ---- */
      React.createElement('section', { className: 'fs-settings__section' },
        React.createElement('div', { className: 'fs-settings__section-title' }, 'Default landing page'),
        React.createElement('div', { className: 'fs-settings__section-desc' },
          'Where you land when you open the app or navigate to the root.'),
        React.createElement('div', { className: 'fs-settings__field-row' },
          React.createElement('label', {
            className: 'fs-settings__label',
            htmlFor:   'fs-settings-landing',
          }, 'On open, go to'),
          React.createElement('select', {
            id:        'fs-settings-landing',
            className: 'fs-settings__select',
            value:     state.landingOverride || '',
            onChange:  function (e) { setLanding(e.target.value || null); },
          },
            React.createElement('option', { value: '' },
              'Use my role\'s default (' + roleDefault + ')'),
            visibleItems.map(function (item) {
              return React.createElement('option', { key: item.key, value: item.path }, item.label);
            }),
          ),
        ),
        state.landingOverride
          ? React.createElement('div', { className: 'fs-settings__field-hint' },
              'Override active. Clear the dropdown to restore role default.')
          : null,
      ),

      /* ---- Section 4: Help (Sprint 8.11.1) ---- */
      React.createElement('section', { className: 'fs-settings__section' },
        React.createElement('div', { className: 'fs-settings__section-title' }, 'Help'),
        React.createElement('div', { className: 'fs-settings__section-desc' },
          'Replay the welcome tour the next time you open the app.'),
        React.createElement('button', {
          type:      'button',
          className: 'fs-settings__link-btn',
          onClick:   function () {
            try { localStorage.removeItem('fs.onboarded'); } catch (_) {}
            if (window.FS && window.FS.toast) {
              window.FS.toast.show({
                message: 'Onboarding will run on next reload',
                tone:    'info',
              });
            }
          },
        }, 'Reset onboarding'),
      ),

    );
  }

  /* ---------- SettingsRightDetail --------------------------------------- */

  function SettingsRightDetail() {
    var ctx = React.useContext(SettingsContext);
    if (!ctx) {
      return React.createElement('div', { className: 'fs-settings-summary fs-settings-summary--empty' },
        'Select a preference to see details.');
    }

    var state       = ctx.state;
    var user        = ctx.user;
    var roleDefault = window.FS && window.FS.getDefaultLanding
      ? window.FS.getDefaultLanding(user)
      : '/today';

    /* Look up the display label for the effective landing */
    var effectiveLanding = state.landingOverride || roleDefault;
    var landingLabel     = effectiveLanding;
    if (window.FS && window.FS.NAV_ITEMS) {
      var items = window.FS.NAV_ITEMS;
      Object.keys(items).forEach(function (k) {
        if (items[k].path === effectiveLanding) landingLabel = items[k].label;
      });
    }

    var themeStoredLabel   = { light: 'Light', dark: 'Dark', auto: 'Auto' }[state.themeStored]
      || state.themeStored;
    var themeResolvedLabel = state.themeResolved.charAt(0).toUpperCase()
      + state.themeResolved.slice(1);
    var densityLabel       = { comfortable: 'Comfortable', compact: 'Compact' }[state.densityStored]
      || state.densityStored;

    function Row(label, value) {
      return React.createElement('div', { className: 'fs-settings-summary__row' },
        React.createElement('div', { className: 'fs-settings-summary__label' }, label),
        React.createElement('div', { className: 'fs-settings-summary__value' }, value),
      );
    }

    return React.createElement('div', { className: 'fs-settings-summary' },

      React.createElement('div', { className: 'fs-settings-summary__title' }, 'Your preferences'),

      React.createElement('div', { className: 'fs-settings-summary__rows' },
        Row('Theme preference', themeStoredLabel),
        Row('Resolved to',      themeResolvedLabel),
        Row('Display density',  densityLabel),
        Row('Default landing',
          state.landingOverride
            ? landingLabel + ' (override)'
            : 'Role default · ' + roleDefault),
      ),

      React.createElement('div', { className: 'fs-settings-summary__note' },
        'Preferences are stored in your browser. Changes take effect immediately.'),

    );
  }

  /* ---------- Register -------------------------------------------------- */

  if (!window.FieldSight) window.FieldSight = {};
  if (!window.FieldSight.PAGES) window.FieldSight.PAGES = {};
  window.FieldSight.PAGES['/settings'] = {
    Middle:   SettingsMiddleColumn,
    Right:    SettingsRightDetail,
    Provider: SettingsProvider,
    layout:   'full-width',   /* Sprint 10 A — form page; summary panel via RightDrawer */
  };

})();
