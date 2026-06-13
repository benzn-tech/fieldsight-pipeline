/* Builds the dynamic content of tokens-reference.html from the live CSS vars
   in styles/tokens.css. Single file, no framework. */

(function () {
  'use strict';

  const css = getComputedStyle(document.documentElement);
  const v = (name) => css.getPropertyValue(name).trim();

  /* ---------- helpers ----------------------------------------------------- */

  const el = (tag, attrs, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, val] of Object.entries(attrs || {})) {
      if (val == null) continue;
      if (k === 'style') n.setAttribute('style', val);
      else if (k === 'class') n.className = val;
      else if (k === 'html') n.innerHTML = val;
      else n.setAttribute(k, val);
    }
    for (const kid of kids) {
      if (kid == null) continue;
      n.append(kid && kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return n;
  };

  /* ---------- ramp definitions ------------------------------------------- */

  const RAMPS = {
    primary: {
      label: 'Primary Navy',
      prefix: '--color-primary-',
      shades: ['50','100','200','300','400','500','600','700','800','900','950'],
    },
    accent: {
      label: 'Safety Orange',
      prefix: '--color-accent-',
      shades: ['50','100','200','300','400','500','600','700','800','900'],
    },
    danger: {
      label: 'Danger',
      prefix: '--color-danger-',
      shades: ['50','100','200','300','400','500','600','700','800','900'],
    },
    warning: {
      label: 'Warning',
      prefix: '--color-warning-',
      shades: ['50','100','200','300','400','500','600','700','800','900'],
    },
    success: {
      label: 'Success',
      prefix: '--color-success-',
      shades: ['50','100','200','300','400','500','600','700','800','900'],
    },
    info: {
      label: 'Info',
      prefix: '--color-info-',
      shades: ['50','100','200','300','400','500','600','700','800','900'],
    },
    neutral: {
      label: 'Neutral',
      prefix: '--color-neutral-',
      shades: ['0','50','100','200','300','400','500','600','700','800','900','950'],
    },
  };

  function buildRamp(host, key, note) {
    const def = RAMPS[key];
    if (!def) return;

    const wrap = el('div');
    const labelRow = el('div', { class: 'ramp-label-row' },
      el('div', { class: 'ramp-name' }, def.label),
      note ? el('div', { class: 'ramp-note' }, note) : null,
    );

    const grid = el('div', { class: 'swatches' });
    grid.style.gridTemplateColumns = `repeat(${def.shades.length}, minmax(0, 1fr))`;

    def.shades.forEach((shade) => {
      const token = `${def.prefix}${shade}`;
      const hex = v(token) || '—';
      const sw = el('div', { class: 'swatch' },
        el('div', { class: 'chip', style: `background:${hex}` }),
        el('div', { class: 'meta' },
          el('div', { class: 'shade' }, shade),
          el('div', { class: 'hex' }, hex.toUpperCase()),
        ),
      );
      grid.append(sw);
    });

    wrap.append(labelRow, grid);
    host.replaceChildren(wrap);
  }

  document.querySelectorAll('[data-ramp]').forEach((row) => {
    buildRamp(row, row.dataset.ramp, row.dataset.note || '');
  });

  /* ---------- categories ------------------------------------------------- */

  const CATEGORIES = [
    ['Safety',     'safety',     'safety'],
    ['Public',     'public',     'public'],
    ['Quality',    'quality',    'quality'],
    ['Programme',  'programme',  'programme'],
    ['Commercial', 'commercial', 'commercial'],
    ['Weather',    'weather',    'weather'],
    ['General',    'general',    'general'],
  ];

  const catGrid = document.getElementById('cat-grid');
  if (catGrid) {
    CATEGORIES.forEach(([name, slug]) => {
      const fg = v(`--color-category-${slug}`);
      const bg = v(`--color-category-${slug}-bg`);
      catGrid.append(
        el('div', { class: 'cat-card' },
          el('div', { class: 'cat-row' },
            el('div', { class: 'cat-dot', style: `background:${fg}` }),
            el('div', null,
              el('div', { class: 'cat-name' }, name),
              el('div', { class: 'cat-hex' }, fg.toUpperCase()),
            ),
          ),
          el('div', { class: 'cat-bg-chip', style: `background:${bg};color:${fg}` }, name),
        ),
      );
    });
  }

  /* ---------- surfaces --------------------------------------------------- */

  const SURFACES = [
    ['App background',  '--surface-app'],
    ['Panel',           '--surface-panel'],
    ['Panel · elevated','--surface-panel-elevated'],
    ['Panel · muted',   '--surface-panel-muted'],
    ['Sidebar',         '--surface-sidebar'],
    ['Input',           '--surface-input'],
    ['Highlight',       '--surface-highlight'],
    ['Tooltip',         '--surface-tooltip'],
  ];

  const surfaceGrid = document.getElementById('surface-grid');
  if (surfaceGrid) {
    SURFACES.forEach(([name, token]) => {
      const bg = v(token);
      const isDarkSurface = ['--surface-sidebar', '--surface-tooltip'].includes(token);
      const fg = isDarkSurface ? 'var(--color-neutral-0)' : 'var(--text-primary)';
      surfaceGrid.append(
        el('div', { class: 'surface-card', style: `background:${bg};color:${fg}` },
          el('div', { class: 'surface-name' }, name),
          el('div', { class: 'token-name' }, token),
          el('div', { class: 'token-name' }, bg.toUpperCase()),
        ),
      );
    });
  }

  /* ---------- text roles ------------------------------------------------- */

  const TEXT_ROLES = [
    ['Primary',     '--text-primary',     'The most important text on the page.'],
    ['Secondary',   '--text-secondary',   'Supporting text, subtitles, descriptions.'],
    ['Tertiary',    '--text-tertiary',    'Timestamps, metadata, helper copy.'],
    ['Disabled',    '--text-disabled',    'Interactive but currently unavailable.'],
    ['Placeholder', '--text-placeholder', 'Empty form-field hint.'],
    ['Link',        '--text-link',        'Navigates somewhere else.'],
    ['Danger',      '--text-danger',      'Error messages, destructive copy.'],
    ['Success',     '--text-success',     'Confirmation, positive deltas.'],
    ['Warning',     '--text-warning',     'Caution and pending states.'],
  ];

  const textRoles = document.getElementById('text-roles');
  if (textRoles) {
    TEXT_ROLES.forEach(([name, token, sample]) => {
      textRoles.append(
        el('div', { class: 'row' },
          el('div', { style: `color:var(${token});font-size:var(--font-size-base);font-weight:var(--font-weight-medium)` }, `${name} — ${sample}`),
          el('div', { class: 'token' }, token),
        ),
      );
    });
  }

  /* ---------- typography specimens -------------------------------------- */

  const TYPE_PRESETS = [
    ['display',          'type-display',         'Display · stats hero'],
    ['h1',               'type-h1',              'Page heading H1'],
    ['h2',               'type-h2',              'Section heading H2'],
    ['h3',               'type-h3',              'Subsection H3'],
    ['h4',               'type-h4',              'Card title H4'],
    ['body-lg',          'type-body-lg',         'Body large — emphasized prose for primary copy.'],
    ['body',             'type-body',            'Body — the default text on the page, designed for sustained reading.'],
    ['body-sm',          'type-body-sm',         'Body small — secondary copy and dense metadata.'],
    ['label',            'type-label',           'Label · form field'],
    ['label-uppercase',  'type-label-uppercase', 'Eyebrow label'],
    ['caption',          'type-caption',         'Caption · timestamps + helper'],
    ['button',           'type-button',          'Button text'],
    ['stat',             'type-stat',            '1,284'],
    ['code',             'type-code',            'const status = "in_progress";'],
  ];

  const typeSpec = document.getElementById('type-specimens');
  if (typeSpec) {
    TYPE_PRESETS.forEach(([name, cls, sample]) => {
      // Probe a hidden node to pull computed metrics so the meta column
      // reflects whatever is in tokens.css today.
      const probe = el('span', { class: cls, style: 'position:absolute;visibility:hidden;left:-9999px;' }, 'A');
      document.body.append(probe);
      const cs = getComputedStyle(probe);
      const meta = `${cs.fontSize} / ${cs.lineHeight} · ${cs.fontWeight}`;
      probe.remove();

      typeSpec.append(
        el('div', { class: 'type-row' },
          el('div', { class: 'type-name' }, `.${cls}`),
          el('div', { class: cls, style: 'overflow:hidden;text-overflow:ellipsis;' }, sample),
          el('div', { class: 'type-meta' }, meta),
        ),
      );
    });
  }

  /* ---------- spacing ---------------------------------------------------- */

  const SPACING = [
    ['--space-0-5', 2], ['--space-1', 4], ['--space-1-5', 6], ['--space-2', 8],
    ['--space-2-5', 10], ['--space-3', 12], ['--space-4', 16], ['--space-5', 20],
    ['--space-6', 24], ['--space-8', 32], ['--space-10', 40], ['--space-12', 48],
    ['--space-16', 64], ['--space-20', 80], ['--space-24', 96], ['--space-32', 128],
  ];

  const MAX_PX = 128;
  const spacingHost = document.getElementById('spacing-rows');
  if (spacingHost) {
    SPACING.forEach(([token, px]) => {
      spacingHost.append(
        el('div', { class: 'space-row' },
          el('div', { class: 'space-token' }, token),
          el('div', { class: 'space-value' }, `${px}px`),
          el('div', null,
            el('div', { class: 'space-bar', style: `width:${(px / MAX_PX) * 100}%` }),
          ),
        ),
      );
    });
  }

  /* ---------- radius ----------------------------------------------------- */

  const RADII = [
    ['none',  '--radius-none', '0'],
    ['sm',    '--radius-sm',   '4px · tags, badges'],
    ['md',    '--radius-md',   '8px · buttons, inputs'],
    ['lg',    '--radius-lg',   '12px · cards'],
    ['xl',    '--radius-xl',   '16px · modals'],
    ['2xl',   '--radius-2xl',  '24px · prominent cards'],
    ['full',  '--radius-full', '9999px · pills'],
  ];

  const radiusGrid = document.getElementById('radius-grid');
  if (radiusGrid) {
    RADII.forEach(([name, token, note]) => {
      radiusGrid.append(
        el('div', { class: 'radius-card', style: `border-radius:var(${token})` },
          el('div', { class: 'r-name' }, name),
          el('div', { class: 'r-value' }, token),
          el('div', { class: 'r-value' }, note),
        ),
      );
    });
  }

  /* ---------- shadows ---------------------------------------------------- */

  const SHADOWS = [
    ['xs',   '--shadow-xs',  'subtle border lift'],
    ['sm',   '--shadow-sm',  'cards at rest'],
    ['md',   '--shadow-md',  'hover, dropdowns'],
    ['lg',   '--shadow-lg',  'popovers, menus'],
    ['xl',   '--shadow-xl',  'modals'],
    ['2xl',  '--shadow-2xl', 'deepest layer'],
  ];

  const shadowGrid = document.getElementById('shadow-grid');
  if (shadowGrid) {
    SHADOWS.forEach(([name, token, note]) => {
      shadowGrid.append(
        el('div', { class: 'shadow-card', style: `box-shadow:var(${token});border-radius:var(--radius-lg)` },
          el('div', { class: 's-name' }, name),
          el('div', { class: 's-meta' }, token),
          el('div', { class: 's-meta' }, note),
        ),
      );
    });
  }

  /* ---------- motion ----------------------------------------------------- */

  const MOTIONS = [
    { name: 'fast',   dur: '--duration-fast',   ease: '--ease-out',    label: 'Fast · ease-out',    note: 'micro-interactions, hover' },
    { name: 'base',   dur: '--duration-base',   ease: '--ease-out',    label: 'Base · ease-out',    note: 'default UI transitions' },
    { name: 'slow',   dur: '--duration-slow',   ease: '--ease-in-out', label: 'Slow · ease-in-out', note: 'panel slides, drawer' },
    { name: 'spring', dur: '--duration-slower', ease: '--ease-spring', label: 'Slower · spring',    note: 'celebratory bounces' },
  ];

  const motionGrid = document.getElementById('motion-grid');
  if (motionGrid) {
    MOTIONS.forEach((m) => {
      const dur = v(m.dur);
      const ease = v(m.ease);
      motionGrid.append(
        el('div', { class: 'motion-card' },
          el('div', { class: 'motion-label' },
            el('div', { class: 'motion-name' }, m.label),
            el('div', { class: 'motion-value' }, dur),
          ),
          el('div', { class: 'motion-track' },
            el('div', { class: 'motion-ball', style: `animation-duration:${dur};animation-timing-function:${ease}` }),
          ),
          el('div', { class: 'motion-value' }, m.note),
        ),
      );
    });
  }

  /* ---------- z-index table --------------------------------------------- */

  const Z_LAYERS = [
    ['--z-base',     'base content'],
    ['--z-raised',   'hovered cards'],
    ['--z-dropdown', 'select menus, autocomplete'],
    ['--z-sticky',   'sticky headers'],
    ['--z-fixed',    'fixed nav rails'],
    ['--z-backdrop', 'modal scrim'],
    ['--z-modal',    'dialogs'],
    ['--z-popover',  'popovers above modals'],
    ['--z-toast',    'transient notifications'],
    ['--z-tooltip',  'on top of everything UI'],
    ['--z-max',      'emergency override'],
  ];

  const zBody = document.getElementById('z-body');
  if (zBody) {
    Z_LAYERS.forEach(([token, use]) => {
      zBody.append(
        el('tr', null,
          el('td', { class: 'mono' }, token),
          el('td', { class: 'mono' }, v(token)),
          el('td', null, use),
        ),
      );
    });
  }

  /* ---------- breakpoints ----------------------------------------------- */

  const BPS = [
    ['--breakpoint-sm',   640,  'phone landscape'],
    ['--breakpoint-md',   768,  'tablet portrait'],
    ['--breakpoint-lg',  1024,  'tablet landscape, small laptop'],
    ['--breakpoint-xl',  1280,  'design target · default desktop'],
    ['--breakpoint-2xl', 1536,  'large desktop'],
  ];
  const BP_MAX = 1536;

  const bpHost = document.getElementById('bp-rows');
  if (bpHost) {
    BPS.forEach(([token, px, note]) => {
      bpHost.append(
        el('div', { class: 'bp-row' },
          el('div', { class: 'bp-token' }, token),
          el('div', { class: 'bp-value' }, `${px}px`),
          el('div', { class: 'bp-bar' },
            el('div', { class: 'fill', style: `width:${(px / BP_MAX) * 100}%` }),
          ),
        ),
      );
    });
  }

  /* ---------- pills (priority / status / source) ------------------------ */

  const buildPills = (host, items) => {
    if (!host) return;
    items.forEach(([name, token]) => {
      host.append(
        el('span', { class: 'pill' },
          el('span', { class: 'dot', style: `background:var(${token})` }),
          name,
        ),
      );
    });
  };

  buildPills(document.getElementById('priority-pills'), [
    ['Critical', '--priority-critical'],
    ['High',     '--priority-high'],
    ['Medium',   '--priority-medium'],
    ['Low',      '--priority-low'],
    ['None',     '--priority-none'],
  ]);

  // Status uses a custom builder so Blocked + Overdue can carry their
  // differentiating treatments (halt glyph vs. pulsing dot, filled
  // backgrounds) — color alone wasn't separating them clearly enough.
  const statusHost = document.getElementById('status-pills');
  if (statusHost) {
    const statusVariants = [
      { name: 'Open',        token: '--status-open'        },
      { name: 'In progress', token: '--status-in-progress' },
      { name: 'Blocked',     token: '--status-blocked',
        bg:    '--status-blocked-bg',
        glyph: '◼',                  // halt / stop block
        title: 'Paused — waiting on a dependency' },
      { name: 'Done',        token: '--status-done'        },
      { name: 'Cancelled',   token: '--status-cancelled'   },
      { name: 'Overdue',     token: '--status-overdue',
        bg:    '--status-overdue-bg',
        pulse: true,
        title: 'Past its deadline — actively bleeding' },
    ];

    statusVariants.forEach((s) => {
      const fg = `var(${s.token})`;
      const styles = [];
      // Overdue is the only "alarming" state — give it the filled
      // red treatment. Blocked uses the same neutral border as every
      // other status pill, distinguished only by its fuchsia dot/glyph.
      if (s.bg && s.name === 'Overdue') {
        styles.push(`background:var(${s.bg})`);
        styles.push(`color:${fg}`);
        styles.push(`border:1px solid color-mix(in srgb, ${fg} 55%, transparent)`);
      }
      const pill = el('span', {
        class: 'pill',
        style: styles.join(';') || null,
        title: s.title || null,
      });

      if (s.glyph) {
        pill.append(
          el('span', {
            class: 'glyph',
            style: `color:${fg};font-size:10px;line-height:1;display:inline-flex;width:8px;height:8px;align-items:center;justify-content:center;`,
          }, s.glyph),
        );
      } else {
        pill.append(
          el('span', {
            class: s.pulse ? 'dot pulse' : 'dot',
            style: `background:${fg}`,
          }),
        );
      }
      pill.append(s.name);
      statusHost.append(pill);
    });
  }

  buildPills(document.getElementById('source-pills'), [
    ['Programme',    '--source-programme'],
    ['Conversation', '--source-conversation'],
    ['Report',       '--source-report'],
    ['Manual',       '--source-manual'],
    ['AI suggested', '--source-ai-suggested'],
  ]);

  /* Category chips for the applied-examples section */
  const catPills = document.getElementById('cat-pills');
  if (catPills) {
    CATEGORIES.forEach(([name, slug]) => {
      const fg = v(`--color-category-${slug}`);
      const bg = v(`--color-category-${slug}-bg`);
      catPills.append(
        el('span', { class: 'pill', style: `background:${bg};color:${fg};border-color:transparent` },
          el('span', { class: 'dot', style: `background:${fg}` }),
          name,
        ),
      );
    });
  }

  /* ---------- theme toggle ---------------------------------------------- */

  const root = document.documentElement;
  const btnLight = document.getElementById('theme-light');
  const btnDark  = document.getElementById('theme-dark');

  function setTheme(theme) {
    root.setAttribute('data-theme', theme);
    btnLight.setAttribute('aria-pressed', String(theme === 'light'));
    btnDark.setAttribute('aria-pressed',  String(theme === 'dark'));
    try { localStorage.setItem('fs.theme', theme); } catch (_) {}

    // Re-derive any hex-meta cells whose values come from CSS vars
    document.querySelectorAll('[data-ramp]').forEach((row) => {
      buildRamp(row, row.dataset.ramp, row.dataset.note || '');
    });
  }

  btnLight.addEventListener('click', () => setTheme('light'));
  btnDark.addEventListener('click',  () => setTheme('dark'));

  let initial = 'light';
  try {
    const saved = localStorage.getItem('fs.theme');
    if (saved === 'light' || saved === 'dark') initial = saved;
  } catch (_) {}
  setTheme(initial);

  /* ---------- §10 · Role system v2.1 ----------------------------------- */
  // Mirror of scripts/roles.js — kept inline because this page uses a
  // classic <script> tag, not <script type="module">.
  // Includes FIX 1 (action hierarchy) and FIX 2 (separate nav resources).
  const SCOPES = {
    SELF:          'self',
    CREW:          'crew',
    SITE:          'site',
    PROJECT:       'project',
    MULTI_PROJECT: 'multi_project',
    REGION:        'region',
    ORG:           'org',
    ASSIGNED:      'assigned',
  };
  const P = (resource, action, scope) =>
    scope ? `${resource}:${action}:${scope}` : `${resource}:${action}`;

  // FIX 1 · Action implication hierarchy
  const ACTION_IMPLIES = {
    manage:  ['view','create','edit','delete','approve','assign','export','capture'],
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

  const ROLES = {
    worker: {
      level: 1, label: 'Worker', scope: SCOPES.SELF,
      description: 'Field worker capturing site events',
      defaultLanding: '/today',
      permissions: [
        P('task','view',SCOPES.SELF), P('event','capture',SCOPES.SELF),
        P('hazard','create',SCOPES.SELF), P('evidence','view',SCOPES.SELF),
        P('report','view',SCOPES.SELF), P('dashboard','view',SCOPES.SELF),
        P('settings','view',SCOPES.SELF),
      ],
    },
    foreman: {
      level: 2, label: 'Foreman', scope: SCOPES.CREW,
      description: 'Leads a crew within a site',
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
      description: 'Manages a single construction site',
      defaultLanding: '/today',
      permissions: [
        P('task','manage',SCOPES.SITE), P('report','create',SCOPES.SITE),
        P('report','approve',SCOPES.SITE), P('dashboard','view',SCOPES.SITE),
        P('pre_start','manage',SCOPES.SITE), P('evidence','view',SCOPES.SITE),
        P('safety','view',SCOPES.SITE), P('quality','view',SCOPES.SITE),
        P('weather','view',SCOPES.SITE), P('incident','capture',SCOPES.SITE),
        P('programme','view',SCOPES.SITE), P('settings','view',SCOPES.SELF),
      ],
    },
    project_manager: {
      level: 4, label: 'Project Manager', scope: SCOPES.PROJECT,
      description: 'Manages a project (typically 1 site, occasionally multi-stage)',
      defaultLanding: '/morning-brief',
      permissions: [
        P('dashboard','view',SCOPES.PROJECT), P('task','view',SCOPES.PROJECT),
        P('safety','view',SCOPES.PROJECT), P('quality','view',SCOPES.PROJECT),
        P('report','approve',SCOPES.PROJECT), P('programme','manage',SCOPES.PROJECT),
        P('variance','view',SCOPES.PROJECT), P('task','assign',SCOPES.PROJECT),
        P('pattern','view',SCOPES.PROJECT),
        P('pm_brief','view',SCOPES.PROJECT),   // FIX 2
        P('sites','view',SCOPES.PROJECT),       // FIX 2
        P('settings','view',SCOPES.SELF),       // FIX 2
      ],
    },
    construction_manager: {
      level: 5, label: 'Construction Manager', scope: SCOPES.MULTI_PROJECT,
      description: 'Oversees multiple projects strategically',
      defaultLanding: '/regional-dashboard',
      pending: true,
      permissions: [
        P('dashboard','view',SCOPES.MULTI_PROJECT), P('report','view',SCOPES.MULTI_PROJECT),
        P('escalation','view',SCOPES.MULTI_PROJECT), P('pattern','view',SCOPES.MULTI_PROJECT),
        P('commercial','view',SCOPES.MULTI_PROJECT), P('evidence','export',SCOPES.MULTI_PROJECT),
        P('portfolio','view',SCOPES.MULTI_PROJECT),  // FIX 2
        P('settings','view',SCOPES.SELF),             // FIX 2
      ],
    },
    gm: {
      level: 6, label: 'GM / Regional Manager', scope: SCOPES.REGION,
      description: 'Regional or general management',
      defaultLanding: '/regional-dashboard',
      permissions: [
        P('dashboard','view',SCOPES.REGION), P('kpi','view',SCOPES.REGION),
        P('client','view',SCOPES.REGION), P('risk','view',SCOPES.REGION),
        P('safety','view',SCOPES.REGION), P('user','manage',SCOPES.REGION),
        P('report','export',SCOPES.REGION),
        P('regional','view',SCOPES.REGION),   // FIX 2
        P('settings','view',SCOPES.SELF),      // FIX 2
      ],
    },
    director: {
      level: 7, label: 'Director / C-Suite', scope: SCOPES.ORG,
      description: 'Board / executive level — strategic and financial',
      defaultLanding: '/regional-dashboard',
      pending: true,
      permissions: [
        P('dashboard','view',SCOPES.ORG), P('pl','view',SCOPES.ORG),
        P('compliance','view',SCOPES.ORG), P('risk','view',SCOPES.ORG),
        P('audit','view',SCOPES.ORG), P('user','manage',SCOPES.ORG),
        P('report','export',SCOPES.ORG),
        P('executive','view',SCOPES.ORG),   // FIX 2
        P('settings','view',SCOPES.SELF),    // FIX 2
      ],
    },
    hse_manager: {
      level: 5, label: 'HSE Manager', scope: SCOPES.ORG, specialist: true,
      description: 'Cross-site H&S oversight (HSWA 2015 statutory role)',
      defaultLanding: '/safety',
      permissions: [
        P('safety','view',SCOPES.ORG), P('safety','manage',SCOPES.ORG),
        P('hazard','view',SCOPES.ORG), P('hazard','manage',SCOPES.ORG),
        P('incident','view',SCOPES.ORG), P('incident','manage',SCOPES.ORG),
        P('compliance','view',SCOPES.ORG), P('audit','view',SCOPES.ORG),
        P('report','view',SCOPES.ORG), P('evidence','view',SCOPES.ORG),
        P('evidence','export',SCOPES.ORG), P('dashboard','view',SCOPES.ORG),
        P('settings','view',SCOPES.SELF),
        // NOT pm_brief, portfolio, regional, executive, sites
      ],
    },
    quality_manager: {
      level: 5, label: 'Quality Manager', scope: SCOPES.ORG, specialist: true,
      description: 'Cross-site quality assurance',
      defaultLanding: '/quality',
      permissions: [
        P('quality','view',SCOPES.ORG), P('quality','manage',SCOPES.ORG),
        P('report','view',SCOPES.ORG), P('evidence','view',SCOPES.ORG),
        P('evidence','export',SCOPES.ORG), P('dashboard','view',SCOPES.ORG),
        P('settings','view',SCOPES.SELF),
        // NOT pm_brief, portfolio, regional, executive, sites
      ],
    },
    client_viewer: {
      level: 0, label: 'Client Viewer', scope: SCOPES.ASSIGNED, specialist: true,
      description: 'External client / owner read-only access to assigned projects',
      defaultLanding: '/today',
      permissions: [
        P('dashboard','view',SCOPES.ASSIGNED), P('report','view',SCOPES.ASSIGNED),
        P('evidence','view',SCOPES.ASSIGNED), P('safety','view',SCOPES.ASSIGNED),
        P('quality','view',SCOPES.ASSIGNED), P('programme','view',SCOPES.ASSIGNED),
        P('weather','view',SCOPES.ASSIGNED), P('settings','view',SCOPES.SELF),
      ],
    },
  };

  // FIX 2 · Separate resources for line-of-authority nav items so
  // HSE/QA (with dashboard:view:org) don't inherit them.
  const NAV_ITEMS = {
    today:         { permission: P('dashboard','view'),                      label: 'Today' },
    morning_brief: { permission: P('pm_brief', 'view'),                      label: 'Morning Brief' },
    portfolio:     { permission: P('portfolio','view'),                      label: 'Portfolio' },
    regional:      { permission: P('regional', 'view'),                      label: 'Regional' },
    executive:     { permission: P('executive','view'),                      label: 'Executive' },
    programme:     { permission: P('programme','view'),                      label: 'Programme' },
    live:          { permission: P('event',    'capture'),                   label: 'Live' },
    review:        { permission: P('report',   'view'),                      label: 'Review' },
    tasks:         { permission: P('task',     'view'),                      label: 'Tasks' },
    safety:        { permission: P('safety',   'view'),                      label: 'Safety' },
    quality:       { permission: P('quality',  'view'),                      label: 'Quality' },
    weather:       { permission: P('weather',  'view'),                      label: 'Weather' },
    evidence:      { permission: P('evidence', 'view'),                      label: 'Evidence' },
    reports:       { permission: P('report',   'view'),                      label: 'Reports' },
    sites:         { permission: P('sites',    'view'),                      label: 'Sites' },
    team:          { permission: P('user',     'manage'),                    label: 'Team' },
    settings:      { permission: P('settings', 'view'),                      label: 'Settings' },
  };

  const HIERARCHY_ROLES = ['worker','foreman','site_manager','project_manager',
                           'construction_manager','gm','director'];

  const SCOPE_HIERARCHY = {
    [SCOPES.SELF]:0, [SCOPES.CREW]:1, [SCOPES.SITE]:2, [SCOPES.PROJECT]:3,
    [SCOPES.MULTI_PROJECT]:4, [SCOPES.REGION]:5, [SCOPES.ORG]:6,
  };

  function getPermissionsForRole(roleName) {
    const role = ROLES[roleName];
    if (!role) return { own: [], inherited: [] };
    if (HIERARCHY_ROLES.includes(roleName)) {
      const inherited = [];
      HIERARCHY_ROLES
        .map((k) => ({ k, r: ROLES[k] }))
        .filter(({ r }) => r && r.level < role.level)
        .sort((a, b) => b.r.level - a.r.level)
        .forEach(({ r }) => r.permissions.forEach((p) => inherited.push(p)));
      return { own: role.permissions, inherited };
    }
    return { own: role.permissions, inherited: [] };
  }

  function parsePerm(p) {
    const [resource, action, scope] = p.split(':');
    return { resource, action, scope };
  }
  function permSatisfies(userPerm, requiredPerm) {
    const u = parsePerm(userPerm), r = parsePerm(requiredPerm);
    if (u.resource !== r.resource) return false;
    // FIX 1: use action hierarchy
    if (!actionSatisfies(u.action, r.action)) return false;
    if (u.scope === r.scope) return true;
    if (!r.scope) return true;
    if (u.scope === SCOPES.ASSIGNED || r.scope === SCOPES.ASSIGNED) {
      return u.scope === r.scope;
    }
    const uL = SCOPE_HIERARCHY[u.scope], rL = SCOPE_HIERARCHY[r.scope];
    if (uL === undefined || rL === undefined) return false;
    return uL >= rL;
  }
  function roleHasPerm(roleKey, requiredPerm) {
    const { own, inherited } = getPermissionsForRole(roleKey);
    const all = own.concat(inherited);
    return all.some((up) => permSatisfies(up, requiredPerm));
  }

  /* Render role cards */
  const stackHost = document.getElementById('roles-stack');
  if (stackHost) {
    // Order: hierarchy roles top-down (highest first), then specialists, then client
    const hierarchy = HIERARCHY_ROLES
      .map((k) => [k, ROLES[k]])
      .sort((a, b) => b[1].level - a[1].level);
    const specialists = [['hse_manager', ROLES.hse_manager], ['quality_manager', ROLES.quality_manager]];
    const external    = [['client_viewer', ROLES.client_viewer]];

    function appendCard([key, role]) {
      const { own, inherited } = getPermissionsForRole(key);
      const total = own.length + inherited.length;
      const tier = role.specialist ? 'mid' : (role.level <= 2 ? 'low' : role.level <= 4 ? 'mid' : 'high');

      const levelLabel = role.specialist
        ? (key === 'client_viewer' ? 'EXT' : 'SPC')
        : `L${role.level}`;

      const metaBits = [
        role.description, ' · ',
        el('code', null, `scope: ${role.scope}`), ' · ',
        el('code', null, role.defaultLanding),
      ];
      if (role.pending) {
        metaBits.push(' · ', el('code', { style: 'color: var(--color-warning-700);' }, 'no users yet'));
      }

      const card = el('div', { class: 'role-card', 'data-tier': tier, 'data-role': key });
      card.append(
        el('div', { class: 'level' }, levelLabel),
        el('div', null,
          el('div', { class: 'role-label' }, role.label),
          el('div', { class: 'role-meta' }, ...metaBits),
        ),
        el('div', { class: 'feature-count' },
          el('span', { class: 'num' }, String(total)),
          ' perms',
        ),
      );

      const list = el('ul', { class: 'feature-list' });
      own.forEach((p) => list.append(el('li', null, '+ ' + p)));
      inherited.forEach((p) => list.append(el('li', { class: 'inherited' }, '↳ ' + p)));
      card.append(list);

      card.addEventListener('click', () => card.classList.toggle('expanded'));
      return card;
    }

    // Hierarchy section
    hierarchy.forEach(([key, role], idx) => {
      stackHost.append(appendCard([key, role]));
      if (idx < hierarchy.length - 1) {
        stackHost.append(el('div', { class: 'role-arrow' }, '↓ inherits from'));
      }
    });

    // Specialists divider + cards
    stackHost.append(el('div', { class: 'role-divider' },
      el('span', null, 'Cross-cutting specialists'),
      el('div', { class: 'role-divider-note' }, 'Functional authority — do not inherit from the hierarchy.'),
    ));
    specialists.forEach(([key, role]) => stackHost.append(appendCard([key, role])));

    // External
    stackHost.append(el('div', { class: 'role-divider' },
      el('span', null, 'External'),
      el('div', { class: 'role-divider-note' }, 'Outside the org. Read-only, assignment-scoped.'),
    ));
    external.forEach(([key, role]) => stackHost.append(appendCard([key, role])));
  }

  /* Nav visibility matrix — derived from permissions */
  const matrixHost = document.getElementById('nav-matrix');
  if (matrixHost) {
    const roleCols = [
      { key: 'worker',               abbr: 'W'   },
      { key: 'foreman',              abbr: 'F'   },
      { key: 'site_manager',         abbr: 'SM'  },
      { key: 'project_manager',      abbr: 'PM'  },
      { key: 'construction_manager', abbr: 'CM'  },
      { key: 'gm',                   abbr: 'GM'  },
      { key: 'director',             abbr: 'Dir' },
      { key: 'hse_manager',          abbr: 'HSE' },
      { key: 'quality_manager',      abbr: 'QA'  },
      { key: 'client_viewer',        abbr: 'CL'  },
    ];

    const thead = el('thead');
    const headRow = el('tr', null, el('th', null, 'Nav item'));
    roleCols.forEach(({ key, abbr }) => headRow.append(el('th', { title: ROLES[key].label }, abbr)));
    thead.append(headRow);
    matrixHost.append(thead);

    const tbody = el('tbody');
    Object.entries(NAV_ITEMS).forEach(([navKey, item]) => {
      const row = el('tr',
        null,
        el('td', null,
          navKey,
          el('span', { class: 'nav-perm' }, item.permission),
        ),
      );
      roleCols.forEach(({ key }) => {
        const has = roleHasPerm(key, item.permission);
        row.append(el('td', { class: has ? 'check' : 'miss' }, has ? '✓' : '·'));
      });
      tbody.append(row);
    });
    matrixHost.append(tbody);
  }
})();
