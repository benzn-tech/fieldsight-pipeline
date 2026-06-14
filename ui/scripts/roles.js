/* ==========================================================================
   FieldSight Role System v2
   --------------------------------------------------------------------------
   Changes from v1:
     - Removed `admin` from hierarchy → now an orthogonal `isAdmin` flag
     - Added `foreman` between worker and site_manager
     - Added `client_viewer` (read-only client portal access)
     - Added `hse_manager` and `quality_manager` (cross-cutting specialists)
     - Renamed feature constants to `resource:action:scope` format
     - Removed NAV_VISIBILITY (derived from features instead)
     - PM scope corrected for NZ context (typically 1 site)
     - SM can approve own reports
     - GM can manage users (small org reality)
   Changes in v2.1:
     - FIX 1: Added ACTION_IMPLIES hierarchy — manage implies view, etc.
     - FIX 2: Separated dashboard nav permissions by purpose (pm_brief,
       portfolio, regional, executive, sites, settings resources) so
       HSE/QA don't incorrectly see line-of-authority dashboards.
   ========================================================================== */


/* ==========================================================================
   Resources, Actions, Scopes
   --------------------------------------------------------------------------
   Permission keys follow: `resource:action:scope`
   ========================================================================== */

export const RESOURCES = {
  TASK:        'task',
  EVENT:       'event',
  HAZARD:      'hazard',
  EVIDENCE:    'evidence',
  REPORT:      'report',
  PRE_START:   'pre_start',
  INCIDENT:    'incident',
  PROGRAMME:   'programme',
  VARIANCE:    'variance',
  DASHBOARD:   'dashboard',
  PM_BRIEF:    'pm_brief',
  PORTFOLIO:   'portfolio',
  REGIONAL:    'regional',
  EXECUTIVE:   'executive',
  SITES:       'sites',
  SETTINGS:    'settings',
  SAFETY:      'safety',
  QUALITY:     'quality',
  COMMERCIAL:  'commercial',
  PL:          'pl',
  RISK:        'risk',
  COMPLIANCE:  'compliance',
  AUDIT:       'audit',
  USER:        'user',
  CLIENT:      'client',
  ESCALATION:  'escalation',
  PATTERN:     'pattern',
  KPI:         'kpi',
  TEMPLATE:    'template',  /* Sprint 10 B.0 — report template library */
};

export const ACTIONS = {
  VIEW:    'view',
  CAPTURE: 'capture',
  CREATE:  'create',
  EDIT:    'edit',
  APPROVE: 'approve',
  ASSIGN:  'assign',
  MANAGE:  'manage',
  EXPORT:  'export',
  DELETE:  'delete',
};

export const SCOPES = {
  SELF:          'self',
  CREW:          'crew',
  SITE:          'site',
  PROJECT:       'project',
  MULTI_PROJECT: 'multi_project',
  REGION:        'region',
  ORG:           'org',
  ASSIGNED:      'assigned',
};


/* ==========================================================================
   Permission helper — string format
   ========================================================================== */

/** Build a permission key: P('safety', 'view', 'site') → 'safety:view:site' */
export function P(resource, action, scope) {
  return scope ? `${resource}:${action}:${scope}` : `${resource}:${action}`;
}


/* ==========================================================================
   FIX 1 · Action hierarchy
   --------------------------------------------------------------------------
   Higher-power actions imply lower-power actions.
   e.g. manage implies view, so PM with programme:manage:project satisfies
   a programme:view check.
   ========================================================================== */

const ACTION_IMPLIES = {
  manage:  ['view', 'create', 'edit', 'delete', 'approve', 'assign', 'export', 'capture'],
  edit:    ['view'],
  approve: ['view'],
  assign:  ['view'],
  create:  ['view'],
  export:  ['view'],
  capture: ['view'],
  view:    [],
  delete:  [],
};

function actionSatisfies(userAction, requiredAction) {
  if (userAction === requiredAction) return true;
  return ACTION_IMPLIES[userAction]?.includes(requiredAction) || false;
}


/* ==========================================================================
   Roles
   ========================================================================== */

export const ROLES = {
  // ── Tier 1 ────────────────────────────────────────────────────────────
  worker: {
    level: 1,
    label: 'Worker',
    scope: SCOPES.SELF,
    description: 'Field worker capturing site events',
    defaultLanding: '/today',
    permissions: [
      P('task',      'view',    SCOPES.SELF),
      P('event',     'capture', SCOPES.SELF),
      P('hazard',    'create',  SCOPES.SELF),
      P('evidence',  'view',    SCOPES.SELF),
      P('report',    'view',    SCOPES.SELF),
      P('dashboard', 'view',    SCOPES.SELF),
      P('settings',  'view',    SCOPES.SELF),
      P('template',  'manage',  SCOPES.SELF),   /* Sprint 10 B.0 — personal library */
      P('template',  'view',    SCOPES.ORG),    /* Sprint 10 B.0 — can view org library */
    ],
  },

  // ── Tier 1.5 ──────────────────────────────────────────────────────────
  foreman: {
    level: 2,
    label: 'Foreman',
    scope: SCOPES.CREW,
    description: 'Leads a crew within a site',
    defaultLanding: '/today',
    permissions: [
      P('task',      'view',    SCOPES.CREW),
      P('task',      'assign',  SCOPES.CREW),
      P('event',     'capture', SCOPES.CREW),
      P('evidence',  'view',    SCOPES.CREW),
      P('report',    'view',    SCOPES.CREW),
      P('report',    'create',  SCOPES.CREW),
      P('dashboard', 'view',    SCOPES.CREW),
      P('settings',  'view',    SCOPES.SELF),
    ],
  },

  // ── Tier 2 ────────────────────────────────────────────────────────────
  site_manager: {
    level: 3,
    label: 'Site Manager',
    scope: SCOPES.SITE,
    description: 'Manages a single construction site',
    defaultLanding: '/today',
    permissions: [
      P('task',      'manage',  SCOPES.SITE),
      P('report',    'create',  SCOPES.SITE),
      P('report',    'approve', SCOPES.SITE),
      P('dashboard', 'view',    SCOPES.SITE),
      P('pre_start', 'manage',  SCOPES.SITE),
      P('evidence',  'view',    SCOPES.SITE),
      P('safety',    'view',    SCOPES.SITE),
      P('quality',   'view',    SCOPES.SITE),
      P('incident',  'capture', SCOPES.SITE),
      P('programme', 'view',    SCOPES.SITE),  // SM views but doesn't manage programme
      P('insights',  'view',    SCOPES.SITE),  // Sprint 9 Track A — site-scoped insights dashboard
      P('settings',  'view',    SCOPES.SELF),
    ],
  },

  // ── Tier 3 ────────────────────────────────────────────────────────────
  project_manager: {
    level: 4,
    label: 'Project Manager',
    scope: SCOPES.PROJECT,
    description: 'Manages a project (typically 1 site, occasionally multi-stage)',
    defaultLanding: '/today',
    permissions: [
      P('dashboard', 'view',    SCOPES.PROJECT),
      P('task',      'view',    SCOPES.PROJECT),
      P('safety',    'view',    SCOPES.PROJECT),
      P('quality',   'view',    SCOPES.PROJECT),
      P('report',    'approve', SCOPES.PROJECT),
      P('programme', 'manage',  SCOPES.PROJECT),
      P('variance',  'view',    SCOPES.PROJECT),
      P('task',      'assign',  SCOPES.PROJECT),
      P('pattern',   'view',    SCOPES.PROJECT),
      P('pm_brief',  'view',    SCOPES.PROJECT),
      P('sites',     'view',    SCOPES.PROJECT),
      P('insights',  'view',    SCOPES.PROJECT),  // Sprint 9 Track A — project-scoped insights
      P('user',      'manage',  SCOPES.PROJECT),  // Sprint 9 Track B — PM can see /team for managed sites
      P('settings',  'view',    SCOPES.SELF),
    ],
  },

  // ── Tier 4 ────────────────────────────────────────────────────────────
  // NOTE: No real users at this tier yet.
  construction_manager: {
    level: 5,
    label: 'Construction Manager',
    scope: SCOPES.MULTI_PROJECT,
    description: 'Oversees multiple projects strategically',
    defaultLanding: '/regional-dashboard',
    permissions: [
      P('dashboard',  'view',  SCOPES.MULTI_PROJECT),
      P('report',     'view',  SCOPES.MULTI_PROJECT),
      P('escalation', 'view',  SCOPES.MULTI_PROJECT),
      P('pattern',    'view',  SCOPES.MULTI_PROJECT),
      P('commercial', 'view',  SCOPES.MULTI_PROJECT),
      P('evidence',   'export',SCOPES.MULTI_PROJECT),
      P('portfolio',  'view',  SCOPES.MULTI_PROJECT),
      P('insights',   'view',  SCOPES.MULTI_PROJECT),  // Sprint 9 Track A
      P('settings',   'view',  SCOPES.SELF),
    ],
  },

  // ── Tier 5 ────────────────────────────────────────────────────────────
  gm: {
    level: 6,
    label: 'GM / Regional Manager',
    scope: SCOPES.REGION,
    description: 'Regional or general management',
    defaultLanding: '/regional-dashboard',
    permissions: [
      P('dashboard', 'view',   SCOPES.REGION),
      P('kpi',       'view',   SCOPES.REGION),
      P('client',    'view',   SCOPES.REGION),
      P('risk',      'view',   SCOPES.REGION),
      P('safety',    'view',   SCOPES.REGION),
      P('user',      'manage', SCOPES.REGION),
      P('report',    'export', SCOPES.REGION),
      P('regional',  'view',   SCOPES.REGION),
      P('insights',  'view',   SCOPES.REGION),  // Sprint 9 Track A
      P('template',  'manage', SCOPES.ORG),     // Sprint 10 B.0 — org library management
      P('settings',  'view',   SCOPES.SELF),
    ],
  },

  // ── Tier 6 ────────────────────────────────────────────────────────────
  // NOTE: No real users at this tier yet.
  director: {
    level: 7,
    label: 'Director / C-Suite',
    scope: SCOPES.ORG,
    description: 'Board / executive level — strategic and financial',
    defaultLanding: '/regional-dashboard',
    permissions: [
      P('dashboard',  'view',   SCOPES.ORG),
      P('pl',         'view',   SCOPES.ORG),
      P('compliance', 'view',   SCOPES.ORG),
      P('risk',       'view',   SCOPES.ORG),
      P('audit',      'view',   SCOPES.ORG),
      P('user',       'manage', SCOPES.ORG),
      P('report',     'export', SCOPES.ORG),
      P('executive',  'view',   SCOPES.ORG),
      P('insights',   'view',   SCOPES.ORG),    // Sprint 9 Track A
      P('settings',   'view',   SCOPES.SELF),
    ],
  },

  // ── Cross-cutting specialists (orthogonal to hierarchy) ───────────────

  hse_manager: {
    level: 5,
    label: 'HSE Manager',
    scope: SCOPES.ORG,
    description: 'Cross-site health & safety oversight (HSWA 2015 statutory role)',
    defaultLanding: '/safety',
    permissions: [
      P('safety',    'view',    SCOPES.ORG),
      P('safety',    'manage',  SCOPES.ORG),
      P('hazard',    'view',    SCOPES.ORG),
      P('hazard',    'manage',  SCOPES.ORG),
      P('incident',  'view',    SCOPES.ORG),
      P('incident',  'manage',  SCOPES.ORG),
      P('compliance','view',    SCOPES.ORG),
      P('audit',     'view',    SCOPES.ORG),
      P('report',    'view',    SCOPES.ORG),
      P('evidence',  'view',    SCOPES.ORG),
      P('evidence',  'export',  SCOPES.ORG),
      P('dashboard', 'view',    SCOPES.ORG),
      P('insights',  'view',    SCOPES.ORG),    // Sprint 9 Track A — HSE eats their own dog food
      P('settings',  'view',    SCOPES.SELF),
      // NOT pm_brief, portfolio, regional, executive, sites
    ],
  },

  quality_manager: {
    level: 5,
    label: 'Quality Manager',
    scope: SCOPES.ORG,
    description: 'Cross-site quality assurance',
    defaultLanding: '/quality',
    permissions: [
      P('quality',  'view',   SCOPES.ORG),
      P('quality',  'manage', SCOPES.ORG),
      P('report',   'view',   SCOPES.ORG),
      P('evidence', 'view',   SCOPES.ORG),
      P('evidence', 'export', SCOPES.ORG),
      P('dashboard','view',   SCOPES.ORG),
      P('insights', 'view',   SCOPES.ORG),     // Sprint 9 Track A
      P('settings', 'view',   SCOPES.SELF),
      // NOT pm_brief, portfolio, regional, executive, sites
    ],
  },

  client_viewer: {
    level: 0,
    label: 'Client Viewer',
    scope: SCOPES.ASSIGNED,
    description: 'External client / owner read-only access to assigned projects',
    defaultLanding: '/today',
    permissions: [
      P('dashboard', 'view',   SCOPES.ASSIGNED),
      P('report',    'view',   SCOPES.ASSIGNED),
      P('evidence',  'view',   SCOPES.ASSIGNED),
      P('safety',    'view',   SCOPES.ASSIGNED),
      P('quality',   'view',   SCOPES.ASSIGNED),
      P('programme', 'view',   SCOPES.ASSIGNED),
      P('settings',  'view',   SCOPES.SELF),
      // No capture, no manage, no export
    ],
  },
};


/* ==========================================================================
   Top-level navigation registry
   --------------------------------------------------------------------------
   FIX 2: portfolio, regional, executive, sites, settings use distinct
   resources so HSE/QA specialists (with dashboard:view:org) don't
   incorrectly inherit line-of-authority dashboard nav items.
   ========================================================================== */

export const NAV_ITEMS = {
  /* DAILY — Today subsumes Morning Brief; Activity subsumes Live + Review */
  today:      { permission: P('dashboard', 'view'),                   label: 'Today' },
  activity:   { permission: P('report',    'view'),                   label: 'Activity' },

  /* WORKSPACE */
  tasks:      { permission: P('task',      'view'),                   label: 'Tasks' },
  programme:  { permission: P('programme', 'view'),                   label: 'Programme' },
  safety:     { permission: P('safety',    'view'),                   label: 'Safety' },
  quality:    { permission: P('quality',   'view'),                   label: 'Quality' },
  evidence:   { permission: P('evidence',  'view'),                   label: 'Evidence' },
  reports:    { permission: P('report',    'view'),                   label: 'Reports' },
  library:    { permission: P('template',  'manage', SCOPES.SELF),   label: 'Library' },  /* Sprint 10 B.0 */

  /* MANAGEMENT */
  sites:      { permission: P('sites',     'view'),                   label: 'Sites' },
  team:       { permission: P('user',      'manage'),                 label: 'Team' },

  /* STRATEGIC */
  portfolio:  { permission: P('portfolio', 'view'),                   label: 'Portfolio' },
  regional:   { permission: P('regional',  'view'),                   label: 'Regional' },
  executive:  { permission: P('executive', 'view'),                   label: 'Executive' },

  /* FOOTER */
  settings:   { permission: P('settings',  'view'),                   label: 'Settings' },
};


/* ==========================================================================
   Permission resolution
   ========================================================================== */

const HIERARCHY_ROLES = ['worker', 'foreman', 'site_manager', 'project_manager',
                         'construction_manager', 'gm', 'director'];

export function getPermissionsForRole(roleName) {
  const role = ROLES[roleName];
  if (!role) return [];

  const all = new Set();

  if (HIERARCHY_ROLES.includes(roleName)) {
    HIERARCHY_ROLES
      .map(name => ROLES[name])
      .filter(r => r && r.level <= role.level)
      .forEach(r => r.permissions.forEach(p => all.add(p)));
  } else {
    role.permissions.forEach(p => all.add(p));
  }

  return Array.from(all);
}


/* ==========================================================================
   Permission check
   ========================================================================== */

const SCOPE_HIERARCHY = {
  [SCOPES.SELF]:          0,
  [SCOPES.CREW]:          1,
  [SCOPES.SITE]:          2,
  [SCOPES.PROJECT]:       3,
  [SCOPES.MULTI_PROJECT]: 4,
  [SCOPES.REGION]:        5,
  [SCOPES.ORG]:           6,
};

function parsePermission(perm) {
  const [resource, action, scope] = perm.split(':');
  return { resource, action, scope };
}

function permissionSatisfies(userPerm, requiredPerm) {
  const u = parsePermission(userPerm);
  const r = parsePermission(requiredPerm);

  if (u.resource !== r.resource) return false;
  // FIX 1: use action hierarchy instead of strict equality
  if (!actionSatisfies(u.action, r.action)) return false;
  if (u.scope === r.scope) return true;
  if (!r.scope) return true;

  if (u.scope === SCOPES.ASSIGNED || r.scope === SCOPES.ASSIGNED) {
    return u.scope === r.scope;
  }

  const uLevel = SCOPE_HIERARCHY[u.scope];
  const rLevel = SCOPE_HIERARCHY[r.scope];
  if (uLevel === undefined || rLevel === undefined) return false;
  return uLevel >= rLevel;
}

export function can(user, requiredPerm) {
  const role = typeof user === 'string' ? user : user?.role;
  const isAdmin = typeof user === 'object' && user?.isAdmin === true;

  if (isAdmin) return true;

  const userPerms = getPermissionsForRole(role);
  return userPerms.some(up => permissionSatisfies(up, requiredPerm));
}


/* ==========================================================================
   Other helpers
   ========================================================================== */

export function hasMinimumRole(userRole, requiredRole) {
  if (!HIERARCHY_ROLES.includes(userRole) || !HIERARCHY_ROLES.includes(requiredRole)) {
    return false;
  }
  return ROLES[userRole]?.level >= ROLES[requiredRole]?.level;
}

export function getDefaultLanding(user) {
  const role = typeof user === 'string' ? user : user?.role;
  return ROLES[role]?.defaultLanding || '/today';
}

export function getRolesAtOrAbove(level) {
  return HIERARCHY_ROLES
    .map(name => ({ key: name, ...ROLES[name] }))
    .filter(r => r.level >= level);
}

export function canSeeNav(navKey, user) {
  const item = NAV_ITEMS[navKey];
  if (!item) return false;
  return can(user, item.permission);
}

export function getVisibleNavItems(user) {
  return Object.entries(NAV_ITEMS)
    .filter(([key]) => canSeeNav(key, user))
    .map(([key, item]) => ({ key, ...item }));
}

export default ROLES;
