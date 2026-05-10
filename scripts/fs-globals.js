/* ==========================================================================
   FieldSight Globals — window.FS
   --------------------------------------------------------------------------
   Wraps tokens.js + roles.js values as a single window global (IIFE) so
   plain <script> tags can consume them without ES-module syntax.

   Sync this file whenever tokens.js or roles.js changes.
   ========================================================================== */

(function () {
  'use strict';

  /* ========================================================================
     TOKENS (mirrored from tokens.js)
     ===================================================================== */

  const tokens = {
    colors: {
      primary: {
        50: '#F0F4F8', 100: '#D9E2EC', 200: '#BCCCDC', 300: '#9FB3C8',
        400: '#829AB1', 500: '#627D98', 600: '#486581', 700: '#334E68',
        800: '#243B53', 900: '#102A43', 950: '#0A1A2E',
      },
      accent: {
        50: '#FFF4ED', 100: '#FFE6D5', 200: '#FFC8A5', 300: '#FFA06B',
        400: '#FF7A3C', 500: '#FF6B35', 600: '#E54B1E', 700: '#C23616',
        800: '#9A2A13', 900: '#7A2210',
      },
      danger: {
        50: '#FEF2F2', 100: '#FEE2E2', 200: '#FECACA', 300: '#FCA5A5',
        400: '#F87171', 500: '#EF4444', 600: '#DC2626', 700: '#B91C1C',
        800: '#991B1B', 900: '#7F1D1D',
      },
      warning: {
        50: '#FFFBEB', 100: '#FEF3C7', 200: '#FDE68A', 300: '#FCD34D',
        400: '#FBBF24', 500: '#F59E0B', 600: '#D97706', 700: '#B45309',
        800: '#92400E', 900: '#78350F',
      },
      success: {
        50: '#F0FDF4', 100: '#DCFCE7', 200: '#BBF7D0', 300: '#86EFAC',
        400: '#4ADE80', 500: '#22C55E', 600: '#16A34A', 700: '#15803D',
        800: '#166534', 900: '#14532D',
      },
      info: {
        50: '#EFF6FF', 100: '#DBEAFE', 200: '#BFDBFE', 300: '#93C5FD',
        400: '#60A5FA', 500: '#3B82F6', 600: '#2563EB', 700: '#1D4ED8',
        800: '#1E40AF', 900: '#1E3A8A',
      },
      neutral: {
        0: '#FFFFFF', 50: '#F9FAFB', 100: '#F3F4F6', 200: '#E5E7EB',
        300: '#D1D5DB', 400: '#9CA3AF', 500: '#6B7280', 600: '#4B5563',
        700: '#374151', 800: '#1F2937', 900: '#111827', 950: '#030712',
      },
      category: {
        safety: '#EF4444', public: '#D97706', quality: '#2563EB',
        programme: '#7C3AED', commercial: '#15803D',
        general: '#6B7280',
        safetyBg: '#FEE2E2', publicBg: '#FEF3C7', qualityBg: '#DBEAFE',
        programmeBg: '#EDE9FE', commercialBg: '#DCFCE7',
        generalBg: '#F3F4F6',
      },
    },
    surface: {
      app: '#F9FAFB', panel: '#FFFFFF', panelElevated: '#FFFFFF',
      panelMuted: '#F3F4F6', sidebar: '#102A43', sidebarHover: '#243B53',
      sidebarActive: '#334E68', input: '#FFFFFF', inputHover: '#F9FAFB',
      inputFocus: '#FFFFFF', overlay: 'rgba(17, 24, 39, 0.5)',
      tooltip: '#111827', highlight: '#FFF4ED',
    },
    border: {
      subtle: '#E5E7EB', default: '#D1D5DB', strong: '#9CA3AF',
      focus: '#FF6B35', danger: '#EF4444', success: '#22C55E',
    },
    text: {
      primary: '#111827', secondary: '#4B5563', tertiary: '#6B7280',
      /* Sprint 11 A.2 — bumped to neutral-500 for AA 4.5:1 (was -400 = 2.85:1) */
      disabled: '#6B7280', placeholder: '#6B7280', link: '#2563EB',
      linkHover: '#1D4ED8', inverse: '#FFFFFF', inverseMuted: '#D1D5DB',
      danger: '#B91C1C', success: '#15803D', warning: '#B45309',
    },
    typography: {
      fontFamily: {
        sans: "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
        mono: "'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace",
      },
      fontSize: {
        xs: '0.75rem', sm: '0.875rem', base: '1rem', lg: '1.125rem',
        xl: '1.25rem', '2xl': '1.5rem', '3xl': '1.875rem', '4xl': '2.25rem',
        '5xl': '3rem',
      },
      fontWeight: { regular: 400, medium: 500, semibold: 600, bold: 700 },
      lineHeight: {
        tight: 1.25, snug: 1.375, normal: 1.5, relaxed: 1.625, loose: 2,
      },
      letterSpacing: {
        tight: '-0.025em', normal: '0', wide: '0.025em', wider: '0.05em',
      },
    },
    space: {
      0:   '0',
      0.5: '0.125rem',
      1:   '0.25rem',
      1.5: '0.375rem',
      2:   '0.5rem',
      2.5: '0.625rem',
      3:   '0.75rem',
      4:   '1rem',
      5:   '1.25rem',
      6:   '1.5rem',
      8:   '2rem',
      10:  '2.5rem',
      12:  '3rem',
      16:  '4rem',
      20:  '5rem',
      24:  '6rem',
      32:  '8rem',
    },
    radius: {
      none: '0', sm: '0.25rem', md: '0.5rem', lg: '0.75rem',
      xl: '1rem', '2xl': '1.5rem', full: '9999px',
    },
    shadow: {
      xs:    '0 1px 2px 0 rgba(16,42,67,0.05)',
      sm:    '0 1px 3px 0 rgba(16,42,67,0.10), 0 1px 2px 0 rgba(16,42,67,0.06)',
      md:    '0 4px 6px -1px rgba(16,42,67,0.10), 0 2px 4px -1px rgba(16,42,67,0.06)',
      lg:    '0 10px 15px -3px rgba(16,42,67,0.10), 0 4px 6px -2px rgba(16,42,67,0.05)',
      xl:    '0 20px 25px -5px rgba(16,42,67,0.10), 0 10px 10px -5px rgba(16,42,67,0.04)',
      '2xl': '0 25px 50px -12px rgba(16,42,67,0.25)',
      inner: 'inset 0 2px 4px 0 rgba(16,42,67,0.06)',
    },
    duration: {
      instant: '0ms', fast: '100ms', base: '200ms', slow: '300ms', slower: '500ms',
    },
    easing: {
      linear: 'linear',
      in:     'cubic-bezier(0.4, 0, 1, 1)',
      out:    'cubic-bezier(0, 0, 0.2, 1)',
      inOut:  'cubic-bezier(0.4, 0, 0.2, 1)',
      spring: 'cubic-bezier(0.34, 1.56, 0.64, 1)',
      sharp:  'cubic-bezier(0.4, 0, 0.6, 1)',
    },
    zIndex: {
      base: 0, raised: 10, dropdown: 100, sticky: 200,
      fixed: 300, backdrop: 400, modal: 500, toast: 700,
      popover: 600, tooltip: 800, max: 9999,
    },
    breakpoint: {
      sm: '40rem', md: '48rem', lg: '64rem', xl: '80rem', '2xl': '96rem',
    },
    touchTarget: { min: '2.75rem', default: '3rem', large: '3.5rem' },
    priority: {
      critical: '#DC2626', high: '#F87171', medium: '#F59E0B',
      low: '#3B82F6', none: '#9CA3AF',
    },
    status: {
      open: '#3B82F6', inProgress: '#F59E0B', blocked: '#C026D3',
      done: '#22C55E', cancelled: '#9CA3AF', overdue: '#DC2626',
      openBg: '#DBEAFE', inProgressBg: '#FEF3C7', blockedBg: '#FAE8FF',
      doneBg: '#DCFCE7', cancelledBg: '#F3F4F6', overdueBg: '#FEE2E2',
    },
    source: {
      programme:    '#7C3AED',
      conversation: '#3B82F6',
      report:       '#15803D',
      manual:       '#6B7280',
      aiSuggested:  '#FF6B35',
    },
  };

  /* Dark mode token overrides */
  const darkTokens = {
    surface: {
      app: '#0A1018', panel: '#111827', panelElevated: '#1F2937',
      panelMuted: '#0F1623', sidebar: '#000814', sidebarHover: '#0A1018',
      sidebarActive: '#334E68', input: '#111827', inputHover: '#1F2937',
      inputFocus: '#111827', overlay: 'rgba(0,0,0,0.75)',
      tooltip: '#1F2937', highlight: 'rgba(255,107,53,0.12)',
    },
    border: { subtle: '#1F2937', default: '#374151', strong: '#4B5563' },
    text: {
      primary: '#F9FAFB', secondary: '#D1D5DB', tertiary: '#9CA3AF',
      /* Sprint 11 A.2 — bumped to #9CA3AF for AA 4.5:1 on dark surface */
      disabled: '#9CA3AF', placeholder: '#9CA3AF', inverse: '#111827',
      inverseMuted: '#4B5563', danger: '#F87171', success: '#4ADE80',
      warning: '#FBBF24', link: '#60A5FA', linkHover: '#93C5FD',
    },
  };

  function getTokens(mode) {
    if (mode !== 'dark') return tokens;
    function deepMerge(a, b) {
      if (!b || typeof b !== 'object') return b !== undefined ? b : a;
      const out = Object.assign({}, a);
      for (const k of Object.keys(b)) {
        out[k] = (k in a && typeof a[k] === 'object') ? deepMerge(a[k], b[k]) : b[k];
      }
      return out;
    }
    return deepMerge(tokens, darkTokens);
  }


  /* ========================================================================
     ROLES (mirrored from roles.js v2.1)
     ===================================================================== */

  const SCOPES = {
    SELF: 'self', CREW: 'crew', SITE: 'site', PROJECT: 'project',
    MULTI_PROJECT: 'multi_project', REGION: 'region', ORG: 'org',
    ASSIGNED: 'assigned',
  };

  function P(resource, action, scope) {
    return scope ? `${resource}:${action}:${scope}` : `${resource}:${action}`;
  }

  const ACTION_IMPLIES = {
    manage:  ['view','create','edit','delete','approve','assign','export','capture'],
    edit:    ['view'], approve: ['view'], assign: ['view'],
    create:  ['view'], export:  ['view'], capture: ['view'],
    view: [], delete: [],
  };

  function actionSatisfies(userAction, requiredAction) {
    if (userAction === requiredAction) return true;
    return ACTION_IMPLIES[userAction]?.includes(requiredAction) || false;
  }

  const ROLES = {
    worker: {
      level: 1, label: 'Worker', scope: SCOPES.SELF,
      defaultLanding: '/today',
      permissions: [
        P('task','view',SCOPES.SELF), P('event','capture',SCOPES.SELF),
        P('hazard','create',SCOPES.SELF), P('evidence','view',SCOPES.SELF),
        P('report','view',SCOPES.SELF), P('dashboard','view',SCOPES.SELF),
        P('settings','view',SCOPES.SELF),
        P('template','manage',SCOPES.SELF),  /* Sprint 10 B.0 — personal library */
        P('template','view',SCOPES.ORG),     /* Sprint 10 B.0 — view org library */
      ],
    },
    foreman: {
      level: 2, label: 'Foreman', scope: SCOPES.CREW,
      defaultLanding: '/today',
      permissions: [
        P('task','view',SCOPES.CREW), P('task','assign',SCOPES.CREW),
        P('event','capture',SCOPES.CREW), P('evidence','view',SCOPES.CREW),
        P('report','view',SCOPES.CREW), P('report','create',SCOPES.CREW),
        P('dashboard','view',SCOPES.CREW), P('settings','view',SCOPES.SELF),
      ],
    },
    site_manager: {
      level: 3, label: 'Site Manager', scope: SCOPES.SITE,
      defaultLanding: '/today',
      permissions: [
        P('task','manage',SCOPES.SITE), P('report','create',SCOPES.SITE),
        P('report','approve',SCOPES.SITE), P('dashboard','view',SCOPES.SITE),
        P('pre_start','manage',SCOPES.SITE), P('evidence','view',SCOPES.SITE),
        P('safety','view',SCOPES.SITE), P('quality','view',SCOPES.SITE),
        P('incident','capture',SCOPES.SITE),
        P('programme','view',SCOPES.SITE),
        P('insights','view',SCOPES.SITE),  /* Sprint 9 Track A */
        P('settings','view',SCOPES.SELF),
      ],
    },
    project_manager: {
      level: 4, label: 'Project Manager', scope: SCOPES.PROJECT,
      defaultLanding: '/today',
      permissions: [
        P('dashboard','view',SCOPES.PROJECT), P('task','view',SCOPES.PROJECT),
        P('safety','view',SCOPES.PROJECT), P('quality','view',SCOPES.PROJECT),
        P('report','approve',SCOPES.PROJECT), P('programme','manage',SCOPES.PROJECT),
        P('variance','view',SCOPES.PROJECT), P('task','assign',SCOPES.PROJECT),
        P('pattern','view',SCOPES.PROJECT), P('pm_brief','view',SCOPES.PROJECT),
        P('sites','view',SCOPES.PROJECT),
        P('insights','view',SCOPES.PROJECT),  /* Sprint 9 Track A */
        P('user','manage',SCOPES.PROJECT),    /* Sprint 9 Track B */
        P('settings','view',SCOPES.SELF),
      ],
    },
    construction_manager: {
      level: 5, label: 'Construction Manager', scope: SCOPES.MULTI_PROJECT,
      defaultLanding: '/regional-dashboard',
      permissions: [
        P('dashboard','view',SCOPES.MULTI_PROJECT), P('report','view',SCOPES.MULTI_PROJECT),
        P('escalation','view',SCOPES.MULTI_PROJECT), P('pattern','view',SCOPES.MULTI_PROJECT),
        P('commercial','view',SCOPES.MULTI_PROJECT), P('evidence','export',SCOPES.MULTI_PROJECT),
        P('portfolio','view',SCOPES.MULTI_PROJECT),
        P('insights','view',SCOPES.MULTI_PROJECT),  /* Sprint 9 Track A */
        P('settings','view',SCOPES.SELF),
      ],
    },
    gm: {
      level: 6, label: 'GM / Regional Manager', scope: SCOPES.REGION,
      defaultLanding: '/regional-dashboard',
      permissions: [
        P('dashboard','view',SCOPES.REGION), P('kpi','view',SCOPES.REGION),
        P('client','view',SCOPES.REGION), P('risk','view',SCOPES.REGION),
        P('safety','view',SCOPES.REGION), P('user','manage',SCOPES.REGION),
        P('report','export',SCOPES.REGION), P('regional','view',SCOPES.REGION),
        P('insights','view',SCOPES.REGION),  /* Sprint 9 Track A */
        P('template','manage',SCOPES.ORG),   /* Sprint 10 B.0 — org library management */
        P('settings','view',SCOPES.SELF),
      ],
    },
    director: {
      level: 7, label: 'Director / C-Suite', scope: SCOPES.ORG,
      defaultLanding: '/regional-dashboard',
      permissions: [
        P('dashboard','view',SCOPES.ORG), P('pl','view',SCOPES.ORG),
        P('compliance','view',SCOPES.ORG), P('risk','view',SCOPES.ORG),
        P('audit','view',SCOPES.ORG), P('user','manage',SCOPES.ORG),
        P('report','export',SCOPES.ORG), P('executive','view',SCOPES.ORG),
        P('insights','view',SCOPES.ORG),  /* Sprint 9 Track A */
        P('settings','view',SCOPES.SELF),
      ],
    },
    hse_manager: {
      level: 5, label: 'HSE Manager', scope: SCOPES.ORG, specialist: true,
      defaultLanding: '/safety',
      permissions: [
        P('safety','view',SCOPES.ORG), P('safety','manage',SCOPES.ORG),
        P('hazard','view',SCOPES.ORG), P('hazard','manage',SCOPES.ORG),
        P('incident','view',SCOPES.ORG), P('incident','manage',SCOPES.ORG),
        P('compliance','view',SCOPES.ORG), P('audit','view',SCOPES.ORG),
        P('report','view',SCOPES.ORG), P('evidence','view',SCOPES.ORG),
        P('evidence','export',SCOPES.ORG), P('dashboard','view',SCOPES.ORG),
        P('insights','view',SCOPES.ORG),  /* Sprint 9 Track A */
        P('settings','view',SCOPES.SELF),
      ],
    },
    quality_manager: {
      level: 5, label: 'Quality Manager', scope: SCOPES.ORG, specialist: true,
      defaultLanding: '/quality',
      permissions: [
        P('quality','view',SCOPES.ORG), P('quality','manage',SCOPES.ORG),
        P('report','view',SCOPES.ORG), P('evidence','view',SCOPES.ORG),
        P('evidence','export',SCOPES.ORG), P('dashboard','view',SCOPES.ORG),
        P('insights','view',SCOPES.ORG),  /* Sprint 9 Track A */
        P('settings','view',SCOPES.SELF),
      ],
    },
    client_viewer: {
      level: 0, label: 'Client Viewer', scope: SCOPES.ASSIGNED, specialist: true,
      defaultLanding: '/today',
      permissions: [
        P('dashboard','view',SCOPES.ASSIGNED), P('report','view',SCOPES.ASSIGNED),
        P('evidence','view',SCOPES.ASSIGNED), P('safety','view',SCOPES.ASSIGNED),
        P('quality','view',SCOPES.ASSIGNED), P('programme','view',SCOPES.ASSIGNED),
        P('settings','view',SCOPES.SELF),
      ],
    },
  };

  const NAV_ITEMS = {
    /* DAILY */
    today:      { permission: P('dashboard','view'),  label: 'Today',      path: '/today' },
    timeline:   { permission: P('report','view'),     label: 'Timeline',   path: '/timeline' },
    activity:   { permission: P('report','view'),     label: 'Activity',   path: '/activity' },

    /* WORKSPACE */
    tasks:      { permission: P('task','view'),       label: 'Tasks',      path: '/tasks' },
    programme:  { permission: P('programme','view'),  label: 'Programme',  path: '/programme' },
    safety:     { permission: P('safety','view'),     label: 'Safety',     path: '/safety' },
    quality:    { permission: P('quality','view'),    label: 'Quality',    path: '/quality' },
    evidence:   { permission: P('evidence','view'),   label: 'Evidence',   path: '/evidence' },
    reports:    { permission: P('report','view'),     label: 'Reports',    path: '/reports' },
    library:    { permission: P('template','manage',SCOPES.SELF), label: 'Library', path: '/library' },  /* Sprint 10 B.0 */

    /* INSIGHTS — Sprint 9 Track A */
    insights:   { permission: P('insights','view'),   label: 'Insights',   path: '/insights' },

    /* MANAGEMENT */
    sites:      { permission: P('sites','view'),      label: 'Sites',      path: '/sites' },
    team:       { permission: P('user','manage'),     label: 'Team',       path: '/team' },

    /* STRATEGIC */
    portfolio:  { permission: P('portfolio','view'),  label: 'Portfolio',  path: '/portfolio' },
    regional:   { permission: P('regional','view'),   label: 'Regional',   path: '/regional' },
    executive:  { permission: P('executive','view'),  label: 'Executive',  path: '/executive' },

    /* FOOTER */
    settings:   { permission: P('settings','view'),   label: 'Settings',   path: '/settings' },
  };

  const HIERARCHY_ROLES = ['worker','foreman','site_manager','project_manager',
                           'construction_manager','gm','director'];

  const SCOPE_HIERARCHY = {
    [SCOPES.SELF]: 0, [SCOPES.CREW]: 1, [SCOPES.SITE]: 2,
    [SCOPES.PROJECT]: 3, [SCOPES.MULTI_PROJECT]: 4,
    [SCOPES.REGION]: 5, [SCOPES.ORG]: 6,
  };

  function getPermissionsForRole(roleName) {
    const role = ROLES[roleName];
    if (!role) return [];
    const all = new Set();
    if (HIERARCHY_ROLES.includes(roleName)) {
      HIERARCHY_ROLES
        .map(n => ROLES[n])
        .filter(r => r && r.level <= role.level)
        .forEach(r => r.permissions.forEach(p => all.add(p)));
    } else {
      role.permissions.forEach(p => all.add(p));
    }
    return Array.from(all);
  }

  function parsePermission(perm) {
    const [resource, action, scope] = perm.split(':');
    return { resource, action, scope };
  }

  function permissionSatisfies(userPerm, requiredPerm) {
    const u = parsePermission(userPerm);
    const r = parsePermission(requiredPerm);
    if (u.resource !== r.resource) return false;
    if (!actionSatisfies(u.action, r.action)) return false;
    if (u.scope === r.scope) return true;
    if (!r.scope) return true;
    if (u.scope === SCOPES.ASSIGNED || r.scope === SCOPES.ASSIGNED) return u.scope === r.scope;
    const uL = SCOPE_HIERARCHY[u.scope], rL = SCOPE_HIERARCHY[r.scope];
    if (uL === undefined || rL === undefined) return false;
    return uL >= rL;
  }

  function can(user, requiredPerm) {
    const role = typeof user === 'string' ? user : user?.role;
    const isAdmin = typeof user === 'object' && user?.isAdmin === true;
    if (isAdmin) return true;
    const userPerms = getPermissionsForRole(role);
    return userPerms.some(up => permissionSatisfies(up, requiredPerm));
  }

  function canSeeNav(navKey, user) {
    const item = NAV_ITEMS[navKey];
    if (!item) return false;
    return can(user, item.permission);
  }

  function getVisibleNavItems(user) {
    return Object.entries(NAV_ITEMS)
      .filter(([key]) => canSeeNav(key, user))
      .map(([key, item]) => ({ key, ...item }));
  }

  function getDefaultLanding(user) {
    const role = typeof user === 'string' ? user : user?.role;
    return ROLES[role]?.defaultLanding || '/today';
  }


  /* ========================================================================
     Export to window.FS
     ===================================================================== */

  window.FS = {
    tokens,
    darkTokens,
    getTokens,
    ROLES,
    SCOPES,
    NAV_ITEMS,
    HIERARCHY_ROLES,
    P,
    can,
    canSeeNav,
    getPermissionsForRole,
    getVisibleNavItems,
    getDefaultLanding,
  };

})();
