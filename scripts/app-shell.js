/* ==========================================================================
   FieldSight AppShell — React/createElement
   Exported to window.FieldSight.AppShell
   ========================================================================== */

/* global React, ReactDOM, window */

const STORAGE_KEYS = {
  middleWidth:  'fs.appshell.middleWidth',
  navCollapsed: 'fs.appshell.navCollapsed',
  theme:        'fs.settings.theme',
  density:      'fs.settings.density',
};

const MIDDLE_WIDTH_DEFAULT = 320;

/* ---------- Mobile bottom-nav item icons (mirrors NAV_ICONS in left-nav.js) */
const NAV_ICONS_BOTTOM = {
  today:     'calendar-check',
  timeline:  'gantt-chart',
  tasks:     'check-square',
  safety:    'shield-alert',
  programme: 'gantt-chart',
  quality:   'badge-check',
  evidence:  'folder-open',
  reports:   'file-text',
  sites:     'map-pin',
  team:      'users',
  activity:  'activity',
  settings:  'settings',
  portfolio: 'layout-dashboard',
  regional:  'map',
  executive: 'briefcase',
};

/* ---------- BottomNav (mobile only — rendered by AppShell) -------------- */
/* Top 5 visible nav items shown as tabs; remainder accessible via More drawer. */
function BottomNav({ user, currentRoute, onNavigate }) {
  const NavIcon   = window.FieldSight && window.FieldSight.NavIcon;
  const [moreOpen, setMoreOpen] = React.useState(false);

  const visibleItems = (window.FS.getVisibleNavItems ? window.FS.getVisibleNavItems(user) : [])
    .filter(function (i) { return i.key !== 'settings'; });

  const primary = visibleItems.slice(0, 4);
  const overflow = visibleItems.slice(4);
  const anyOverflowActive = overflow.some(function (i) { return i.path === currentRoute; });

  function closeMore() { setMoreOpen(false); }

  return React.createElement(React.Fragment, null,
    /* Backdrop for more-sheet */
    React.createElement('div', {
      className: 'fs-bottom-nav__more-sheet-backdrop' + (moreOpen ? ' fs-bottom-nav__more-sheet-backdrop--open' : ''),
      onClick:   closeMore,
      'aria-hidden': true,
    }),

    /* More sheet */
    React.createElement('div', {
      className:    'fs-bottom-nav__more-sheet' + (moreOpen ? ' fs-bottom-nav__more-sheet--open' : ''),
      role:         'menu',
      'aria-label': 'More navigation items',
    },
      overflow.map(function (item) {
        var active = currentRoute === item.path;
        return React.createElement('button', {
          key:          item.key,
          type:         'button',
          className:    'fs-bottom-nav__more-item' + (active ? ' fs-bottom-nav__more-item--active' : ''),
          role:         'menuitem',
          onClick:      function () { onNavigate(item.path); closeMore(); },
        },
          NavIcon ? React.createElement(NavIcon, {
            name: NAV_ICONS_BOTTOM[item.key] || 'circle',
            size: 18,
            color: active ? 'var(--color-accent-500)' : 'rgba(255,255,255,0.65)',
          }) : null,
          item.label,
        );
      }),
    ),

    /* Bottom tab bar */
    React.createElement('nav', {
      className:    'fs-bottom-nav',
      role:         'navigation',
      'aria-label': 'Main navigation',
    },
      primary.map(function (item) {
        var active = currentRoute === item.path;
        return React.createElement('button', {
          key:          item.key,
          type:         'button',
          className:    'fs-bottom-nav__item' + (active ? ' fs-bottom-nav__item--active' : ''),
          'aria-label': item.label,
          'aria-current': active ? 'page' : undefined,
          onClick:      function () { onNavigate(item.path); },
        },
          NavIcon ? React.createElement(NavIcon, {
            name: NAV_ICONS_BOTTOM[item.key] || 'circle',
            size: 20,
            color: active ? 'var(--color-accent-500)' : 'rgba(255,255,255,0.55)',
          }) : null,
          React.createElement('span', null, item.label),
        );
      }),

      /* More button — only if there are overflow items */
      overflow.length > 0
        ? React.createElement('button', {
            type:         'button',
            className:    'fs-bottom-nav__item' + (anyOverflowActive ? ' fs-bottom-nav__item--active' : ''),
            'aria-label': 'More navigation items',
            'aria-expanded': moreOpen,
            onClick:      function () { setMoreOpen(function (o) { return !o; }); },
          },
            NavIcon ? React.createElement(NavIcon, {
              name: 'more-horizontal',
              size: 20,
              color: anyOverflowActive ? 'var(--color-accent-500)' : 'rgba(255,255,255,0.55)',
            }) : null,
            React.createElement('span', null, 'More'),
          )
        : null,
    ),
  );
}

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

/* ---------- Share / copy-link helper (Sprint 8.10.2) -------------------- */
async function shareCurrentLink() {
  var url = window.location.href;
  /* Prefer Web Share API on mobile when available. */
  if (navigator.share && /Mobi|Android|iPhone|iPad/i.test(navigator.userAgent || '')) {
    try {
      await navigator.share({ title: document.title, url: url });
      return;
    } catch (_) { /* user dismissed — fall through to clipboard */ }
  }
  try {
    await navigator.clipboard.writeText(url);
    if (window.FS && window.FS.toast) {
      window.FS.toast.show({ message: 'Link copied to clipboard', tone: 'success' });
    }
  } catch (_) {
    if (window.FS && window.FS.toast) {
      window.FS.toast.show({
        message: 'Copy failed — URL: ' + url,
        tone:    'warning',
        duration: 8000,
      });
    }
  }
}

/* ---------- MiddleColumn -------------------------------------------------- */
function MiddleColumn({ route, width, onWidthChange, onSelect, selectedItem, fullWidth, onSearchOpen }) {
  const t = window.FS.tokens;

  const routeLabel = (route || '/').replace(/^\//, '') || 'today';
  const title = routeLabel
    .split('-').map(w => w[0].toUpperCase() + w.slice(1)).join(' ');

  /* Sprint 4.7 — full-width pages (currently /programme) ignore the
     middle-column width slider and let the column flex to fill the
     entire content area. The static right detail is suppressed at
     the AppShell level; a slide-in drawer takes its place when the
     user selects something. */
  const style = fullWidth
    ? {
        flex: 1,
        minWidth: 0,
        background: 'var(--surface-panel)',
        borderRight: '1px solid var(--border-subtle)',
      }
    : {
        width: width + 'px',
        background: 'var(--surface-panel)',
        borderRight: '1px solid var(--border-subtle)',
      };

  const headerStyle = {
    height: '56px',
    display: 'flex',
    alignItems: 'center',
    padding: '0 20px',
    borderBottom: '1px solid var(--border-subtle)',
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

  return React.createElement('div', {
    style: style, className: 'middle-column',
    id: 'fs-main-content',   /* Sprint 8.5.3 — skip-nav target */
    tabIndex: -1,
  },

    React.createElement('div', { style: headerStyle, className: 'middle-column__header' },
      React.createElement('div', {
        style: { display: 'flex', flexDirection: 'column', flex: 1, gap: '2px' },
      },
        React.createElement('span', {
          style: {
            fontWeight: t.typography.fontWeight.semibold,
            fontSize: t.typography.fontSize.base,
            color: 'var(--text-primary)',
            lineHeight: 1.2,
          },
        }, title),
        route === '/today' ? React.createElement('span', {
          style: { fontSize: '11px', color: 'var(--text-tertiary)', lineHeight: 1.2 },
        }, formatTodayDate()) : null,
      ),

      /* Right-side utility area: search + share + weather */
      React.createElement('div', { className: 'middle-column__utility' },
        onSearchOpen ? React.createElement('button', {
          type:         'button',
          className:    'fs-utility-item fs-search-btn',
          onClick:      onSearchOpen,
          title:        'Search  (⌘K)',
          'aria-label': 'Open search (Cmd+K)',
        },
          window.FieldSight.NavIcon && React.createElement(window.FieldSight.NavIcon, {
            name: 'search', size: 16,
          }),
        ) : null,
        /* Sprint 8.10.2 — copy-link / share button */
        React.createElement('button', {
          type:         'button',
          className:    'fs-utility-item fs-share-btn',
          onClick:      shareCurrentLink,
          title:        'Copy link to this view',
          'aria-label': 'Copy link to this view',
        },
          window.FieldSight.NavIcon && React.createElement(window.FieldSight.NavIcon, {
            name: 'share-2', size: 16,
          }),
        ),
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
            background: 'var(--surface-panel-muted)',
            border: '1px dashed var(--border-subtle)',
            borderRadius: '12px',
            color: 'var(--text-tertiary)',
          },
        },
          React.createElement('div', {
            style: {
              width: '48px', height: '48px', borderRadius: '50%',
              background: 'var(--surface-panel)',
              border: '1px solid var(--border-subtle)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            },
          },
            NavIcon ? React.createElement(NavIcon, {
              name: 'hammer', size: 22, color: 'var(--text-disabled)',
            }) : null,
          ),
          React.createElement('div', {
            style: {
              fontSize: t.typography.fontSize.base,
              fontWeight: t.typography.fontWeight.semibold,
              color: 'var(--text-secondary)',
            },
          }, title),
          React.createElement('div', {
            style: { fontSize: t.typography.fontSize.sm, lineHeight: 1.5 },
          }, 'This page isn’t wired up yet — coming in a later sprint.'),
        );
      })(),
    ),

    /* Drag handle on right edge — controlled by AppShell.
       Sprint 4.7: hidden on full-width pages (no neighbouring column
       to resize against). */
    !fullWidth && window.FieldSight.DragDivider ? React.createElement(
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

/* ---------- MobileBack — ← Back button shown inside right-detail on mobile */
function MobileBack({ onClose }) {
  var NavIcon = window.FieldSight && window.FieldSight.NavIcon;
  return React.createElement('button', {
    type:      'button',
    className: 'fs-mobile-back',
    onClick:   onClose,
    'aria-label': 'Back to list',
  },
    NavIcon ? React.createElement(NavIcon, { name: 'chevron-left', size: 16, color: 'var(--color-accent-500)' }) : null,
    'Back',
  );
}

/* ---------- RightDetail --------------------------------------------------- */
function RightDetail({ route, selectedItem, onClose }) {
  const t = window.FS.tokens;

  var page = window.FieldSight.getPageForRoute && window.FieldSight.getPageForRoute(route);
  if (page && page.Right) {
    return React.createElement('div', {
      className: 'right-detail',
      style: { background: 'var(--surface-app)', height: '100%', overflow: 'hidden', display: 'flex', flexDirection: 'column' },
    },
      /* Sprint 8.4.1 — back button visible only on mobile via CSS */
      React.createElement(MobileBack, { onClose: onClose }),
      React.createElement('div', { style: { flex: 1, overflow: 'hidden' } },
        React.createElement(page.Right, {
          selectedItem: selectedItem,
          onClose: onClose,
        }),
      ),
    );
  }

  /* Default empty state for unregistered routes */
  const iconWrapStyle = {
    width: '60px', height: '60px', borderRadius: '50%',
    background: 'var(--surface-panel)',
    border: '1px solid var(--border-subtle)',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    boxShadow: t.shadow.sm,
  };

  return React.createElement('div', {
    style: { background: 'var(--surface-app)', color: 'var(--text-tertiary)' },
    className: 'right-detail',
  },
    React.createElement('div', { style: iconWrapStyle },
      window.FieldSight && window.FieldSight.NavIcon
        ? React.createElement(window.FieldSight.NavIcon, {
            name: 'panel-right-open', size: 28, color: 'var(--text-disabled)',
          })
        : null,
    ),
    React.createElement('div', {
      style: {
        fontWeight: t.typography.fontWeight.semibold,
        fontSize: t.typography.fontSize.base,
        color: 'var(--text-secondary)',
      },
    }, 'Select an item'),
    React.createElement('div', {
      style: { fontSize: t.typography.fontSize.sm, color: 'var(--text-tertiary)' },
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

  /* Sprint 8.6 — global search palette */
  const [searchOpen, setSearchOpen] = React.useState(false);

  /* Sprint 8.11.2 — keyboard shortcut reference modal */
  const [shortcutsOpen, setShortcutsOpen] = React.useState(false);

  /* Sprint 8.11.1 — first-run onboarding overlay */
  const [onboardingOpen, setOnboardingOpen] = React.useState(function () {
    try {
      var p = new URLSearchParams(window.location.search);
      if (p.get('onboarding') === '1') return true;        /* dev override */
      return localStorage.getItem('fs.onboarded') !== '1';
    } catch (_) { return false; }
  });

  /* Sprint 8.9.2 — product tour */
  const [demoTourOpen, setDemoTourOpen] = React.useState(function () {
    return !!(window.FieldSight && window.FieldSight.shouldRunDemoTour
              && window.FieldSight.shouldRunDemoTour());
  });

  /* Sprint 8.7 — offline detection */
  const [isOnline, setIsOnline] = React.useState(function () {
    return typeof navigator !== 'undefined' ? navigator.onLine !== false : true;
  });

  /* Clear selection on route change — different page = fresh selection */
  React.useEffect(function() {
    setSelectedItem(null);
  }, [route]);

  /* Sprint 8.5.4 — announce page title to screen readers + update document.title.
     Sprint 8.10.1 — also expose printable date on #fs-main-content for @media print. */
  React.useEffect(function() {
    var label = (route || '/').replace(/^\//, '') || 'today';
    var title = label.split('-').map(function(w) { return w[0].toUpperCase() + w.slice(1); }).join(' ');
    document.title = title + ' · FieldSight';
    var region = document.getElementById('fs-live-region');
    if (region) region.textContent = 'Navigated to ' + title;
    var main = document.getElementById('fs-main-content');
    if (main) {
      var d = new Date();
      main.setAttribute('data-print-date',
        d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0')
        + '-' + String(d.getDate()).padStart(2, '0'));
    }
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

  /* Keyboard shortcuts — ignored when typing in text fields */
  React.useEffect(function() {
    function onKey(e) {
      var target = e.target;
      var tag = target && target.tagName;
      var inInput = tag === 'INPUT' || tag === 'TEXTAREA' || (target && target.isContentEditable);
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'b') {
        if (inInput) return;
        e.preventDefault();
        toggleCollapse();
      }
      /* Sprint 8.6 — ⌘K / Ctrl+K opens search palette */
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setSearchOpen(true);
      }
      /* Sprint 8.11.2 — single-key navigation + help (skipped in inputs) */
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (inInput) return;
      if (e.key === '?') {
        e.preventDefault();
        setShortcutsOpen(function (s) { return !s; });
      } else if (e.key.toLowerCase() === 't') {
        e.preventDefault();
        window.FS.Router.navigate('/today');
      } else if (e.key.toLowerCase() === 's') {
        e.preventDefault();
        window.FS.Router.navigate('/safety');
      } else if (e.key.toLowerCase() === 'p') {
        e.preventDefault();
        window.FS.Router.navigate('/programme');
      }
    }
    window.addEventListener('keydown', onKey);
    return function() { window.removeEventListener('keydown', onKey); };
  });

  /* Sprint 8.7 — online / offline detection */
  React.useEffect(function () {
    function onOnline()  { setIsOnline(true); }
    function onOffline() { setIsOnline(false); }
    window.addEventListener('online',  onOnline);
    window.addEventListener('offline', onOffline);
    return function () {
      window.removeEventListener('online',  onOnline);
      window.removeEventListener('offline', onOffline);
    };
  }, []);

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

  /* Sprint 3 P-07 — pages may declare a `Provider` slot in the page
     registry. AppShell wraps Middle + Right in it so the two columns
     share state through React Context (replaces the old window slot
     pattern). Provider's a Context.Provider under the hood — no DOM
     element — so the flex layout of LeftNav / Middle / Right is
     unchanged. Pages without a Provider get React.Fragment, which is
     equally transparent.

     Sprint 4.7 — pages may also declare `layout: 'full-width'`.
     When set, the static RightDetail pane is suppressed and the
     middle column flexes to fill the entire content area. A
     RightDrawer component slides in from the right edge whenever a
     selection is made; ESC / backdrop click / close button dismisses
     it. Currently used by /programme so the Gantt and kanban have
     room to breathe. */
  var pageEntry    = window.FieldSight.getPageForRoute && window.FieldSight.getPageForRoute(route);
  var PageProvider = (pageEntry && pageEntry.Provider) || React.Fragment;
  var fullWidth    = !!(pageEntry && pageEntry.layout === 'full-width');

  /* `has-selection` lets the mobile media query swap which pane is
     visible: middle when nothing is selected, right detail when something is.
     Suppressed on full-width pages — drawer handles selection there. */
  var shellClassName = 'app-shell'
    + ((selectedItem && !fullWidth) ? ' has-selection' : '')
    + (fullWidth ? ' app-shell--full-width' : '');

  /* Expose closeDetail globally so MobileBack button (and swipe handler)
     can close the right-detail panel without prop-drilling. */
  React.useEffect(function () {
    if (!window.FS) window.FS = {};
    window.FS.shell = { closeDetail: function () { setSelectedItem(null); } };
    return function () { delete window.FS.shell; };
  });

  return React.createElement('div', { style: shellStyle, className: shellClassName },

    /* Sprint 8.5.3 — skip navigation link (visually hidden until focused) */
    React.createElement('a', {
      href:      '#fs-main-content',
      className: 'fs-skip-nav',
    }, 'Skip to main content'),

    /* Sprint 8.5.4 — polite live region for route + action announcements */
    React.createElement('div', {
      id:           'fs-live-region',
      className:    'fs-live-region',
      'aria-live':  'polite',
      'aria-atomic': true,
    }),

    React.createElement(window.FieldSight.LeftNav, {
      user: user,
      currentRoute: route,
      isCollapsed: isCollapsed,
      onToggleCollapse: toggleCollapse,
      onNavigate: navigate,
    }),

    React.createElement(PageProvider, null,
      React.createElement(MiddleColumn, {
        route: route,
        width: middleWidth,
        onWidthChange: setMiddleWidth,
        onSelect: setSelectedItem,
        selectedItem: selectedItem,
        fullWidth: fullWidth,
        onSearchOpen: function () { setSearchOpen(true); },
      }),

      /* Static right pane only in normal (3-pane) mode. */
      !fullWidth ? React.createElement(RightDetail, {
        route: route,
        selectedItem: selectedItem,
        onClose: function() { setSelectedItem(null); },
      }) : null,

      /* Slide-in drawer for full-width pages. Always mounted so
         transitions can animate on open/close; opacity + transform
         driven by the `open` prop. */
      fullWidth && window.FieldSight.RightDrawer
        ? React.createElement(window.FieldSight.RightDrawer, {
            open:         !!selectedItem,
            route:        route,
            selectedItem: selectedItem,
            onClose:      function() { setSelectedItem(null); },
          })
        : null,
    ),

    /* Sprint 8.4.1 — bottom navigation bar (rendered outside PageProvider
       so it's always present regardless of page layout mode). */
    React.createElement(BottomNav, {
      user:         user,
      currentRoute: route,
      onNavigate:   navigate,
    }),

    showDevSwitcher && window.FieldSight.DevRoleSwitcher
      ? React.createElement(window.FieldSight.DevRoleSwitcher)
      : null,

    /* Sprint 8.7 — offline banner (fixed, above all content) */
    !isOnline
      ? React.createElement('div', {
          className:   'fs-offline-banner',
          role:        'status',
          'aria-live': 'polite',
        }, '⚠️ You’re offline — changes won’t sync')
      : null,

    /* Sprint 8.6 — global search palette */
    searchOpen && window.FieldSight.SearchPalette
      ? React.createElement(window.FieldSight.SearchPalette, {
          onClose: function () { setSearchOpen(false); },
        })
      : null,

    /* Sprint 8.11.2 — keyboard shortcut reference modal */
    shortcutsOpen && window.FieldSight.ModalOverlay
      ? React.createElement(window.FieldSight.ModalOverlay, {
          open:    true,
          onClose: function () { setShortcutsOpen(false); },
          title:   'Keyboard shortcuts',
          size:    'sm',
        },
          React.createElement('table', { className: 'fs-shortcuts' },
            React.createElement('tbody', null,
              [
                ['Cmd / Ctrl + K', 'Open search palette'],
                ['?',              'Show this shortcut reference'],
                ['Escape',         'Close modal / drawer / detail'],
                ['T',              'Go to Today'],
                ['S',              'Go to Safety'],
                ['P',              'Go to Programme'],
                ['Cmd / Ctrl + B', 'Toggle the navigation sidebar'],
                ['← / →',          'Shift Gantt task date (when bar focused)'],
              ].map(function (row, i) {
                return React.createElement('tr', { key: i },
                  React.createElement('th', { scope: 'row' },
                    React.createElement('kbd', { className: 'fs-shortcuts__key' }, row[0])),
                  React.createElement('td', null, row[1]),
                );
              }),
            ),
          ),
        )
      : null,

    /* Sprint 8.11.1 — first-run onboarding overlay */
    onboardingOpen && window.FieldSight.OnboardingOverlay
      ? React.createElement(window.FieldSight.OnboardingOverlay, {
          user:    user,
          onClose: function () { setOnboardingOpen(false); },
        })
      : null,

    /* Sprint 8.9.2 — product tour (?demo=1) */
    demoTourOpen && window.FieldSight.DemoTour
      ? React.createElement(window.FieldSight.DemoTour, {
          onClose: function () { setDemoTourOpen(false); },
        })
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

   Hooks are called unconditionally at the top so flipping useMocks
   at runtime never trips the Rules of Hooks (M-3 fix from the post-
   merge review). The early branch on useMocks just decides which
   tree to render after the hooks run. */
function SessionGate(opts) {
  const useMocks = window.FS && window.FS.api && window.FS.api.useMocks !== false;
  const session  = window.FS && window.FS.session;
  const Login    = window.FieldSight && window.FieldSight.LoginScreen;

  /* Always run the hooks — even in mock mode — so the hook order is
     stable across re-renders if useMocks ever changes at runtime. */
  const [signedIn, setSignedIn] = React.useState(function () {
    return session ? session.isSignedIn() : true;
  });

  React.useEffect(function () {
    if (!session) return undefined;
    return session.onChange(function () {
      setSignedIn(session.isSignedIn());
    });
  }, [session]);

  /* Mock mode (or no session module loaded): always render AppShell. */
  if (useMocks || !session) {
    return React.createElement(AppShell, opts);
  }

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
  if (window.FS && window.FS.theme)   window.FS.theme.init();
  if (window.FS && window.FS.density) window.FS.density.init();
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
