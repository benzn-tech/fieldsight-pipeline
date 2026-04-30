/* ==========================================================================
   FieldSight AppShell — React/createElement
   Exported to window.FieldSight.AppShell
   ========================================================================== */

/* global React, ReactDOM, window */

const STORAGE_KEYS = {
  middleWidth:  'fs.appshell.middleWidth',
  navCollapsed: 'fs.appshell.navCollapsed',
};

const MIDDLE_WIDTH_DEFAULT = 320;

/* ---------- Weather Indicator + Popover -------------------------------- */
/* Click reveals a popover with current conditions, next 12h hourly,
   and a 7-day daily forecast. Mock data only — Sprint 2 wires the
   real MetService API. */
function WeatherIndicator() {
  const NavIcon = window.FieldSight && window.FieldSight.NavIcon;
  const weatherData = window.FieldSight.MockData
    ? window.FieldSight.MockData.WEATHER
    : null;

  const [open, setOpen] = React.useState(false);
  const wrapRef = React.useRef(null);

  /* Close on outside click */
  React.useEffect(function() {
    if (!open) return;
    function onClick(e) {
      if (wrapRef.current && !wrapRef.current.contains(e.target)) {
        setOpen(false);
      }
    }
    document.addEventListener('mousedown', onClick);
    return function() { document.removeEventListener('mousedown', onClick); };
  }, [open]);

  /* Close on Escape */
  React.useEffect(function() {
    if (!open) return;
    function onKey(e) { if (e.key === 'Escape') setOpen(false); }
    document.addEventListener('keydown', onKey);
    return function() { document.removeEventListener('keydown', onKey); };
  }, [open]);

  if (!weatherData) return null;

  const current = weatherData.current;

  return React.createElement('div', {
    ref: wrapRef,
    style: { position: 'relative', display: 'inline-block' },
  },
    React.createElement('button', {
      type: 'button',
      onClick: function() { setOpen(function(o) { return !o; }); },
      className: 'fs-utility-item' + (open ? ' fs-utility-item--active' : ''),
      title: 'Site weather · ' + current.temp + '°C · ' + current.wind,
      'aria-label': 'Site weather, ' + current.temp + ' degrees',
      'aria-expanded': open,
    },
      NavIcon && React.createElement(NavIcon, { name: current.condition, size: 16 }),
      React.createElement('span', { className: 'fs-utility-item__text' },
        current.temp + '°'),
    ),

    open ? React.createElement(WeatherPopover, {
      data: weatherData,
      onClose: function() { setOpen(false); },
    }) : null,
  );
}

/* ---------- Weather popover content ----------------------------------- */
function WeatherPopover(props) {
  const NavIcon = window.FieldSight && window.FieldSight.NavIcon;
  const data = props.data;
  const current = data.current;

  return React.createElement('div', {
    className: 'fs-weather-popover',
    role: 'dialog',
    'aria-label': 'Weather forecast',
  },

    /* Current */
    React.createElement('div', { className: 'fs-weather-popover__current' },
      React.createElement('div', { className: 'fs-weather-popover__current-icon' },
        NavIcon && React.createElement(NavIcon, { name: current.condition, size: 36 }),
      ),
      React.createElement('div', null,
        React.createElement('div', { className: 'fs-weather-popover__current-temp' },
          current.temp + '°'),
        React.createElement('div', { className: 'fs-weather-popover__current-label' },
          current.conditionLabel),
        React.createElement('div', { className: 'fs-weather-popover__current-meta' },
          'Wind ' + current.wind + ' · Humidity ' + current.humidity),
      ),
    ),

    /* Next 12h hourly strip */
    React.createElement('div', { className: 'fs-weather-popover__section-label' },
      'Next 12 hours'),
    React.createElement('div', { className: 'fs-weather-popover__hourly' },
      data.hourly.map(function(h, i) {
        return React.createElement('div', {
          key: i, className: 'fs-weather-popover__hour',
        },
          React.createElement('div', { className: 'fs-weather-popover__hour-time' },
            h.hour),
          NavIcon && React.createElement(NavIcon, { name: h.condition, size: 16 }),
          React.createElement('div', { className: 'fs-weather-popover__hour-temp' },
            h.temp + '°'),
        );
      }),
    ),

    /* 7-day daily */
    React.createElement('div', { className: 'fs-weather-popover__section-label' },
      '7-day forecast'),
    React.createElement('div', { className: 'fs-weather-popover__daily' },
      data.daily.map(function(d, i) {
        return React.createElement('div', {
          key: i, className: 'fs-weather-popover__day',
        },
          React.createElement('span', { className: 'fs-weather-popover__day-name' },
            d.day),
          React.createElement('span', { className: 'fs-weather-popover__day-date' },
            d.date),
          NavIcon && React.createElement(NavIcon, { name: d.condition, size: 16 }),
          React.createElement('span', { className: 'fs-weather-popover__day-range' },
            d.high + '° / ' + d.low + '°'),
        );
      }),
    ),
  );
}
const MIDDLE_WIDTH_MIN     = 280;
const MIDDLE_WIDTH_MAX     = 480;

/* ---------- Date subtitle helper ------------------------------------------ */
function formatTodayDate() {
  var d = new Date();
  var days   = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  var months = ['Jan','Feb','Mar','Apr','May','Jun',
                'Jul','Aug','Sep','Oct','Nov','Dec'];
  return days[d.getDay()] + ' · ' + d.getDate() + ' ' + months[d.getMonth()];
}

/* ---------- MiddleColumn -------------------------------------------------- */
function MiddleColumn({ route, width, onWidthChange, onSelect, selectedItem }) {
  const t = window.FS.tokens;

  const routeLabel = (route || '/').replace(/^\//, '') || 'today';
  const title = routeLabel
    .split('-').map(w => w[0].toUpperCase() + w.slice(1)).join(' ');

  const style = {
    width: width + 'px',
    background: t.surface.panel,
    borderRight: '1px solid ' + t.border.subtle,
  };

  const headerStyle = {
    height: '56px',
    display: 'flex',
    alignItems: 'center',
    padding: '0 20px',
    borderBottom: '1px solid ' + t.border.subtle,
    flexShrink: 0,
    gap: '12px',
  };

  const contentStyle = {
    flex: 1,
    overflowY: 'auto',
    padding: '24px 20px',
    display: 'flex',
    flexDirection: 'column',
    gap: '8px',
  };

  return React.createElement('div', { style: style, className: 'middle-column' },

    React.createElement('div', { style: headerStyle, className: 'middle-column__header' },
      React.createElement('div', {
        style: { display: 'flex', flexDirection: 'column', flex: 1, gap: '2px' },
      },
        React.createElement('span', {
          style: {
            fontWeight: t.typography.fontWeight.semibold,
            fontSize: t.typography.fontSize.base,
            color: t.text.primary,
            lineHeight: 1.2,
          },
        }, title),
        route === '/today' ? React.createElement('span', {
          style: { fontSize: '11px', color: t.text.tertiary, lineHeight: 1.2 },
        }, formatTodayDate()) : null,
      ),

      /* Right-side utility area: weather + future bell etc. */
      React.createElement('div', { className: 'middle-column__utility' },
        React.createElement(WeatherIndicator),
      ),
    ),

    React.createElement('div', { style: contentStyle },
      (function() {
        var page = window.FieldSight.getPageForRoute && window.FieldSight.getPageForRoute(route);
        if (page && page.Middle) {
          return React.createElement(page.Middle, {
            onSelect:     onSelect,
            selectedItem: selectedItem,
          });
        }
        /* Fallback placeholder for unregistered routes — Sprint 2 fills in
           remaining pages; until then, visualise the route as a friendly
           coming-soon state rather than a bare line of text. */
        var NavIcon = window.FieldSight && window.FieldSight.NavIcon;
        return React.createElement('div', {
          className: 'fs-page-placeholder',
          style: {
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            textAlign: 'center',
            gap: '12px',
            padding: '40px 24px',
            margin: '24px 0',
            background: t.surface.panelMuted,
            border: '1px dashed ' + t.border.subtle,
            borderRadius: '12px',
            color: t.text.tertiary,
          },
        },
          React.createElement('div', {
            style: {
              width: '48px', height: '48px', borderRadius: '50%',
              background: t.surface.panel,
              border: '1px solid ' + t.border.subtle,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            },
          },
            NavIcon ? React.createElement(NavIcon, {
              name: 'hammer', size: 22, color: t.text.disabled,
            }) : null,
          ),
          React.createElement('div', {
            style: {
              fontSize: t.typography.fontSize.base,
              fontWeight: t.typography.fontWeight.semibold,
              color: t.text.secondary,
            },
          }, title),
          React.createElement('div', {
            style: { fontSize: t.typography.fontSize.sm, lineHeight: 1.5 },
          }, 'Coming in Sprint 2 — this page is not yet wired up.'),
        );
      })(),
    ),

    /* Drag handle on right edge — controlled by AppShell */
    window.FieldSight.DragDivider ? React.createElement(
      window.FieldSight.DragDivider,
      {
        value: width,
        onChange: onWidthChange,
        min: MIDDLE_WIDTH_MIN,
        max: MIDDLE_WIDTH_MAX,
        storageKey: STORAGE_KEYS.middleWidth,
        ariaLabel: 'Resize middle column',
      }
    ) : null,
  );
}

/* ---------- RightDetail --------------------------------------------------- */
function RightDetail({ route, selectedItem, onClose }) {
  const t = window.FS.tokens;

  var page = window.FieldSight.getPageForRoute && window.FieldSight.getPageForRoute(route);
  if (page && page.Right) {
    return React.createElement('div', {
      className: 'right-detail',
      style: { background: t.surface.app, height: '100%', overflow: 'hidden' },
    },
      React.createElement(page.Right, {
        selectedItem: selectedItem,
        onClose: onClose,
      }),
    );
  }

  /* Default empty state for unregistered routes */
  const iconWrapStyle = {
    width: '60px', height: '60px', borderRadius: '50%',
    background: t.surface.panel,
    border: '1px solid ' + t.border.subtle,
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    boxShadow: t.shadow.sm,
  };

  return React.createElement('div', {
    style: { background: t.surface.app, color: t.text.tertiary },
    className: 'right-detail',
  },
    React.createElement('div', { style: iconWrapStyle },
      window.FieldSight && window.FieldSight.NavIcon
        ? React.createElement(window.FieldSight.NavIcon, {
            name: 'panel-right-open', size: 28, color: t.text.disabled,
          })
        : null,
    ),
    React.createElement('div', {
      style: {
        fontWeight: t.typography.fontWeight.semibold,
        fontSize: t.typography.fontSize.base,
        color: t.text.secondary,
      },
    }, 'Select an item'),
    React.createElement('div', {
      style: { fontSize: t.typography.fontSize.sm, color: t.text.tertiary },
    }, 'Choose from the list to view details'),
  );
}

/* ---------- AppShell ------------------------------------------------------ */
function AppShell({ showDevSwitcher = false }) {
  const dd = window.FieldSight.DragDivider;
  const lgBreakpoint = window.FS.tokens.breakpoint ? window.FS.tokens.breakpoint.lg : '64rem';

  const [user, setUser]   = React.useState(function() { return window.AuthMock.currentUser; });
  const [route, setRoute] = React.useState(function() { return window.FS.Router.getCurrentRoute().path; });

  /* Persisted nav-collapsed state */
  const [isCollapsed, setCollapsed] = React.useState(function() {
    var stored = dd ? dd.read(STORAGE_KEYS.navCollapsed, null) : null;
    if (stored === 1) return true;
    if (stored === 0) return false;
    return window.matchMedia('(max-width: ' + lgBreakpoint + ')').matches;
  });

  /* Persisted middle column width */
  const [middleWidth, setMiddleWidth] = React.useState(function() {
    return (dd && dd.read(STORAGE_KEYS.middleWidth, MIDDLE_WIDTH_DEFAULT)) || MIDDLE_WIDTH_DEFAULT;
  });

  /* Selected item for right detail panel */
  const [selectedItem, setSelectedItem] = React.useState(null);

  /* Clear selection on route change — different page = fresh selection */
  React.useEffect(function() {
    setSelectedItem(null);
  }, [route]);

  React.useEffect(function() {
    return window.AuthMock.onChange(function(u) { setUser(Object.assign({}, u)); });
  }, []);

  React.useEffect(function() {
    return window.FS.Router.subscribe(function(r) { setRoute(r.path); });
  }, []);

  /* Redirect if user can't see the current route */
  React.useEffect(function() {
    var entries = Object.entries(window.FS.NAV_ITEMS);
    var found = null;
    for (var i = 0; i < entries.length; i++) {
      if (entries[i][1].path === route) { found = entries[i][0]; break; }
    }
    if (found && !window.FS.canSeeNav(found, user)) {
      window.FS.Router.navigate(window.FS.getDefaultLanding(user));
    }
  }, [user, route]);

  /* Keyboard shortcut ⌘/Ctrl+B — ignored when typing in text fields */
  React.useEffect(function() {
    function onKey(e) {
      var target = e.target;
      var tag = target && target.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || (target && target.isContentEditable)) return;
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'b') {
        e.preventDefault();
        toggleCollapse();
      }
    }
    window.addEventListener('keydown', onKey);
    return function() { window.removeEventListener('keydown', onKey); };
  });

  /* Auto-collapse on viewport changes — only if user has no stored preference */
  React.useEffect(function() {
    var mq = window.matchMedia('(max-width: ' + lgBreakpoint + ')');
    function onChange(e) {
      var stored = dd ? dd.read(STORAGE_KEYS.navCollapsed, null) : null;
      if (stored == null) setCollapsed(e.matches);
    }
    mq.addEventListener('change', onChange);
    return function() { mq.removeEventListener('change', onChange); };
  }, [lgBreakpoint]);

  function toggleCollapse() {
    setCollapsed(function(c) {
      var next = !c;
      if (dd) dd.write(STORAGE_KEYS.navCollapsed, next ? 1 : 0);
      return next;
    });
  }

  function navigate(path) {
    window.FS.Router.navigate(path);
  }

  var shellStyle = {
    background: window.FS.tokens.surface.app,
    fontFamily: window.FS.tokens.typography.fontFamily.sans,
    color: window.FS.tokens.text.primary,
  };

  /* `has-selection` lets the mobile media query swap which pane is
     visible: middle when nothing is selected, right detail when something is. */
  var shellClassName = 'app-shell' + (selectedItem ? ' has-selection' : '');

  return React.createElement('div', { style: shellStyle, className: shellClassName },

    React.createElement(window.FieldSight.LeftNav, {
      user: user,
      currentRoute: route,
      isCollapsed: isCollapsed,
      onToggleCollapse: toggleCollapse,
      onNavigate: navigate,
    }),

    React.createElement(MiddleColumn, {
      route: route,
      width: middleWidth,
      onWidthChange: setMiddleWidth,
      onSelect: setSelectedItem,
      selectedItem: selectedItem,
    }),

    React.createElement(RightDetail, {
      route: route,
      selectedItem: selectedItem,
      onClose: function() { setSelectedItem(null); },
    }),

    showDevSwitcher && window.FieldSight.DevRoleSwitcher
      ? React.createElement(window.FieldSight.DevRoleSwitcher)
      : null,
  );
}

/* ---------- SessionGate (Sprint 3, P-08) ---------------------------------- */
/* Decides whether to mount the main AppShell or the LoginScreen.
   • useMocks=true (preview default): always renders AppShell. The
     mock auth-mock.js still drives roles for the dev switcher.
   • useMocks=false: renders LoginScreen until FS.session.isSignedIn(),
     then re-renders AppShell. Subscribes to session.onChange so a
     successful sign-in (or a refresh failure that clears the session)
     swaps the screen in place.

   Captured at first mount: which mode we're in. Flipping useMocks at
   runtime requires a refresh — fine for development. */
function SessionGate(opts) {
  const useMocks = window.FS && window.FS.api && window.FS.api.useMocks !== false;
  const session  = window.FS && window.FS.session;
  const Login    = window.FieldSight && window.FieldSight.LoginScreen;

  /* Short-circuit when running against fixtures: no session needed. */
  if (useMocks || !session) {
    return React.createElement(AppShell, opts);
  }

  const [signedIn, setSignedIn] = React.useState(function () {
    return session.isSignedIn();
  });

  React.useEffect(function () {
    return session.onChange(function () {
      setSignedIn(session.isSignedIn());
    });
  }, []);

  if (!signedIn) {
    if (!Login) {
      console.error('[SessionGate] LoginScreen composite missing');
      return null;
    }
    return React.createElement(Login, {
      onSignedIn: function () { setSignedIn(true); },
    });
  }

  return React.createElement(AppShell, opts);
}

/* ---------- Mount helper -------------------------------------------------- */
function mountAppShell(containerId, opts) {
  containerId = containerId || 'root';
  opts = opts || {};
  var el = document.getElementById(containerId);
  if (!el) { console.error('[AppShell] No element #' + containerId); return; }
  var root = ReactDOM.createRoot(el);
  root.render(React.createElement(SessionGate, opts));
}

if (!window.FieldSight) window.FieldSight = {};
Object.assign(window.FieldSight, {
  AppShell:       AppShell,
  SessionGate:    SessionGate,
  MiddleColumn:   MiddleColumn,
  RightDetail:    RightDetail,
  mountAppShell:  mountAppShell,
});
