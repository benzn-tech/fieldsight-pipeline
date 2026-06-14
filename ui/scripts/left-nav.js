/* ==========================================================================
   FieldSight Left Nav — React/createElement
   Exported to window.FieldSight.LeftNav, window.FieldSight.NavIcon
   ========================================================================== */

/* global React, window */

const NAV_ICONS = {
  today:      'calendar-check',
  activity:   'activity',
  portfolio:  'layout-dashboard',
  regional:   'map',
  executive:  'briefcase',
  programme:  'gantt-chart',
  tasks:      'check-square',
  safety:     'shield-alert',
  quality:    'badge-check',
  evidence:   'folder-open',
  reports:    'file-text',
  library:    'book-open',            /* Sprint 10 B.1 */
  insights:   'bar-chart-3',
  sites:      'map-pin',
  team:       'users',
  settings:   'settings',
};

const NAV_SECTIONS = [
  {
    key: 'DAILY',
    label: 'Daily',
    items: ['today', 'activity'],
  },
  {
    key: 'WORKSPACE',
    label: 'Workspace',
    items: ['tasks', 'programme'],
    /* `compliance` is a visual sub-grouping inside WORKSPACE.
       safety and quality remain independent routes. */
    subgroups: [
      { key: 'COMPLIANCE', label: 'Compliance', items: ['safety', 'quality'] },
    ],
    /* trailing items appear after the subgroup.
       Sprint 9 Track A — `insights` lives in WORKSPACE because PMs
       reach for it during the same morning-routine flow as
       safety/quality/reports. Permission-gated separately so
       workers don't see it. */
    trailingItems: ['evidence', 'reports', 'library', 'insights'],  /* Sprint 10 B.1 */
  },
  {
    key: 'MANAGEMENT',
    label: 'Management',
    items: ['sites', 'team'],
  },
  {
    key: 'STRATEGIC',
    label: 'Strategic',
    items: ['portfolio', 'regional', 'executive'],
  },
];

/* ---------- Icon component ------------------------------------------------ */
function NavIcon({ name, size, color, style: extraStyle }) {
  size = size || 16;
  color = color || 'currentColor';
  const ref = React.useRef(null);
  React.useEffect(function() {
    if (!ref.current || !window.lucide) return;
    const pascal = name.split('-').map(function(w) { return w[0].toUpperCase() + w.slice(1); }).join('');
    const iconData = window.lucide[pascal];
    if (!iconData || !Array.isArray(iconData)) return;
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', String(size));
    svg.setAttribute('height', String(size));
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    /* Set stroke via style (not attribute) so CSS var() values resolve. */
    svg.style.stroke = color;
    svg.setAttribute('stroke-width', '1.75');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    iconData.forEach(function(item) {
      const tag = item[0], attrs = item[1];
      const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
      Object.entries(attrs || {}).forEach(function(pair) { el.setAttribute(pair[0], String(pair[1])); });
      svg.appendChild(el);
    });
    ref.current.innerHTML = '';
    ref.current.appendChild(svg);
  }, [name, size, color]);
  return React.createElement('span', {
    ref: ref,
    style: Object.assign({ display: 'inline-flex', flexShrink: 0 }, extraStyle),
  });
}

/* ---------- NavItem ------------------------------------------------------- */
function NavItem({ navKey, label, isActive, isCollapsed, onClick, isSubItem }) {
  const [hovered, setHovered] = React.useState(false);
  const t = window.FS.tokens;

  const iconColor = isActive
    ? t.colors.accent[500]
    : (hovered ? t.colors.neutral[0] : t.colors.neutral[400]);

  /* Sprint 3 P-04: in collapsed mode (justifyContent:center, 48 px wide
     row), a 3 px borderLeft eats horizontal space inside the flex,
     pushing every icon ~1.5 px right of true centre. We keep the
     borderLeft in expanded mode (where it sits beside text and the
     visual offset is fine) and switch to a non-layout box-shadow
     inset stripe when collapsed. */
  const activeStripe   = isActive ? '3px solid ' + t.colors.accent[500] : '3px solid transparent';
  const collapsedStripe = isCollapsed && isActive
    ? 'inset 3px 0 0 ' + t.colors.accent[500]
    : 'none';

  const itemStyle = {
    display: 'flex',
    alignItems: 'center',
    gap: isCollapsed ? 0 : '10px',
    padding: isCollapsed ? '0' : '0 12px',
    height: '40px',
    cursor: 'pointer',
    borderRadius: '6px',
    /* Sub-items share the top-level icon column rather than indenting
       further — the SubgroupLabel above already establishes hierarchy
       (uppercase, dim, smaller font) so an extra indent here just
       pushed sub-item icons out of alignment with their siblings. */
    margin: '1px 8px',
    position: 'relative',
    justifyContent: isCollapsed ? 'center' : 'flex-start',
    background: isActive
      ? 'var(--surface-sidebar-active)'
      : hovered ? 'var(--surface-sidebar-hover)' : 'transparent',
    color: isActive || hovered ? t.colors.neutral[0] : t.colors.neutral[300],
    fontSize: t.typography.fontSize.sm,
    fontWeight: isActive ? t.typography.fontWeight.semibold : t.typography.fontWeight.medium,
    transition: 'background 100ms ease-out, color 100ms ease-out',
    userSelect: 'none',
    outline: 'none',
    textDecoration: 'none',
    borderLeft: isCollapsed ? '0' : activeStripe,
    boxShadow:  collapsedStripe,
    boxSizing: 'border-box',
  };

  return React.createElement('div',
    {
      style: itemStyle,
      onClick: onClick,
      onMouseEnter: function() { setHovered(true); },
      onMouseLeave: function() { setHovered(false); },
      role: 'menuitem',
      tabIndex: 0,
      title: isCollapsed ? label : undefined,
      onKeyDown: function(e) { if (e.key === 'Enter' || e.key === ' ') onClick(); },
      'data-nav-key': navKey,
    },
    React.createElement(NavIcon, { name: NAV_ICONS[navKey] || 'circle', size: 17, color: iconColor }),
    !isCollapsed ? React.createElement('span', {
      style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
    }, label) : null,
  );
}

/* ---------- SectionLabel -------------------------------------------------- */
function SectionLabel({ label, isCollapsed }) {
  if (isCollapsed) {
    return React.createElement('div', {
      style: { height: '1px', background: 'rgba(255,255,255,0.08)', margin: '8px 12px' },
    });
  }
  return React.createElement('div', {
    style: {
      padding: '16px 20px 4px',
      fontSize: '10px',
      fontWeight: 600,
      letterSpacing: '0.08em',
      textTransform: 'uppercase',
      /* Sprint 8.5.1 — bumped from 0.35 to 0.52 to meet WCAG 4.5:1 on sidebar */
      color: 'rgba(255,255,255,0.52)',
    },
  }, label);
}

/* ---------- SubgroupLabel (smaller, less prominent than SectionLabel) ----- */
function SubgroupLabel({ label, isCollapsed }) {
  if (isCollapsed) {
    return React.createElement('div', {
      style: {
        height: '1px',
        background: 'rgba(255,255,255,0.06)',
        margin: '4px 16px',
      },
    });
  }
  return React.createElement('div', {
    style: {
      /* Same horizontal as SectionLabel so it sits flush with WORKSPACE etc.
         Smaller font + uppercase + lower opacity make it visually
         subordinate; sub-items further indent via NavItem.isSubItem.
         Sprint 8.5.1 — bumped from 0.28 to 0.48 for WCAG 4.5:1 on sidebar */
      padding: '12px 20px 4px',
      fontSize: '9px',
      fontWeight: 600,
      letterSpacing: '0.10em',
      textTransform: 'uppercase',
      color: 'rgba(255,255,255,0.48)',
    },
  }, label);
}

/* ---------- UserArea ------------------------------------------------------ */
function UserArea({ user, isCollapsed }) {
  const [menuOpen, setMenuOpen] = React.useState(false);
  const t = window.FS.tokens;
  const role = window.FS.ROLES[user.role];

  const areaStyle = {
    borderTop: '1px solid rgba(255,255,255,0.08)',
    padding: isCollapsed ? '8px 0' : '8px',
    display: 'flex',
    alignItems: 'center',
    gap: '10px',
    cursor: 'pointer',
    borderRadius: '6px',
    margin: isCollapsed ? '8px 4px' : '8px',
    justifyContent: isCollapsed ? 'center' : 'flex-start',
    position: 'relative',
    flexShrink: 0,
  };

  const avatarStyle = {
    width: '32px', height: '32px', borderRadius: '50%',
    background: user.avatarColor || t.colors.accent[600],
    color: '#fff',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    fontSize: '12px', fontWeight: 700, flexShrink: 0,
  };

  const menuStyle = {
    position: 'absolute',
    bottom: isCollapsed ? 0 : '100%',
    left: isCollapsed ? '64px' : '0',
    right: isCollapsed ? 'auto' : '0',
    background: 'var(--surface-panel-elevated)',
    border: '1px solid var(--border-default)',
    borderRadius: '8px',
    boxShadow: t.shadow.lg,
    zIndex: t.zIndex.dropdown,
    overflow: 'hidden',
    minWidth: '180px',
  };

  const menuItemStyle = {
    padding: '10px 16px',
    fontSize: t.typography.fontSize.sm,
    color: 'var(--text-primary)',
    cursor: 'pointer',
    display: 'flex',
    alignItems: 'center',
    gap: '8px',
  };

  return React.createElement('div', { style: areaStyle, className: 'user-area' },
    React.createElement('div', {
      style: avatarStyle,
      onClick: function() { setMenuOpen(function(o) { return !o; }); },
    }, user.initials || '?'),

    !isCollapsed ? React.createElement('div', {
      style: { flex: 1, minWidth: 0, cursor: 'pointer' },
      onClick: function() { setMenuOpen(function(o) { return !o; }); },
    },
      React.createElement('div', {
        style: {
          fontSize: t.typography.fontSize.sm,
          fontWeight: t.typography.fontWeight.semibold,
          color: t.colors.neutral[0],
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        },
      }, user.name),
      React.createElement('div', {
        style: {
          fontSize: '11px',
          color: 'rgba(255,255,255,0.45)',
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        },
      }, role ? role.label : user.role),
    ) : null,

    menuOpen ? React.createElement('div', { style: menuStyle },
      React.createElement('div', { style: menuItemStyle }, 'Profile'),
      React.createElement('div', {
        style: Object.assign({}, menuItemStyle, {
          borderTop: '1px solid var(--border-subtle)',
          color: 'var(--text-danger)',
        }),
      }, 'Log out'),
    ) : null,

    menuOpen ? React.createElement('div', {
      style: { position: 'fixed', inset: 0, zIndex: t.zIndex.dropdown - 1 },
      onClick: function() { setMenuOpen(false); },
    }) : null,
  );
}

/* ---------- LeftNav ------------------------------------------------------- */
function LeftNav({ user, currentRoute, isCollapsed, onToggleCollapse, onNavigate }) {
  const t = window.FS.tokens;
  const visibleKeys = new Set(
    window.FS.getVisibleNavItems(user).map(function(i) { return i.key; })
  );

  // Dynamic width + sidebar background only — layout comes from .left-nav CSS
  const navStyle = {
    width: isCollapsed ? '64px' : '240px',
    background: 'var(--surface-sidebar)',
    transition: 'width 200ms cubic-bezier(0,0,0.2,1)',
  };

  const logoAreaStyle = {
    height: '56px',
    display: 'flex',
    alignItems: 'center',
    /* Sprint 3 P-03: when collapsed (64 px wide) the 28 px logo + 28 px
       chevron + 24 px padding = 80 px > 64 px and the two icons overlap.
       Drop the F mark in collapsed mode and centre the chevron. */
    justifyContent: isCollapsed ? 'center' : 'space-between',
    padding: isCollapsed ? '0' : '0 12px 0 16px',
    borderBottom: '1px solid rgba(255,255,255,0.08)',
    flexShrink: 0,
  };

  const logoMarkStyle = {
    width: '28px', height: '28px', borderRadius: '6px',
    background: t.colors.accent[500], color: '#fff',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    fontWeight: 800, fontSize: '14px', flexShrink: 0,
  };

  const toggleBtnStyle = {
    width: '28px', height: '28px', borderRadius: '6px',
    border: 'none', background: 'rgba(255,255,255,0.07)',
    color: t.colors.neutral[400],
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    cursor: 'pointer', flexShrink: 0, padding: 0,
  };

  const scrollAreaStyle = {
    flex: 1,
    overflowY: 'auto',
    overflowX: 'hidden',
    paddingBottom: '8px',
    scrollbarWidth: 'none',
  };

  const settingsAreaStyle = {
    borderTop: '1px solid rgba(255,255,255,0.08)',
    padding: '4px 0',
    flexShrink: 0,
  };

  return React.createElement('nav', {
    style: navStyle,
    className: 'left-nav' + (isCollapsed ? ' collapsed' : ''),
    role: 'navigation',
    'aria-label': 'Main navigation',
  },

    /* Logo + collapse toggle. Collapsed mode shows the chevron only —
       the F mark is dropped because it can't share the 64 px column
       with the chevron (P-03). */
    React.createElement('div', { style: logoAreaStyle },
      !isCollapsed ? React.createElement('div', {
        style: { display: 'flex', alignItems: 'center', gap: '10px', minWidth: 0 },
      },
        React.createElement('div', { style: logoMarkStyle }, 'F'),
        React.createElement('span', {
          style: {
            color: '#fff',
            fontWeight: t.typography.fontWeight.semibold,
            fontSize: t.typography.fontSize.base,
            letterSpacing: '-0.01em',
            overflow: 'hidden', whiteSpace: 'nowrap',
          },
        }, 'FieldSight'),
      ) : null,
      React.createElement('button', {
        style: toggleBtnStyle,
        onClick: onToggleCollapse,
        title: isCollapsed ? 'Expand sidebar (⌘B)' : 'Collapse sidebar (⌘B)',
        'aria-label': isCollapsed ? 'Expand sidebar' : 'Collapse sidebar',
      },
        React.createElement(NavIcon, {
          name: isCollapsed ? 'chevron-right' : 'chevron-left',
          size: 14,
          color: 'rgba(255,255,255,0.5)',
        }),
      ),
    ),

    /* Scrollable nav sections */
    React.createElement('div', { style: scrollAreaStyle },
      NAV_SECTIONS.map(function(section) {
        const allKeys = [
          ...(section.items || []),
          ...((section.subgroups || []).flatMap(function(g) { return g.items; })),
          ...(section.trailingItems || []),
        ];
        const anyVisible = allKeys.some(function(k) { return visibleKeys.has(k); });
        if (!anyVisible) return null;

        return React.createElement(React.Fragment, { key: section.key },

          /* Top-level section label (DAILY / WORKSPACE / ...) */
          React.createElement(SectionLabel, { label: section.label, isCollapsed: isCollapsed }),

          /* Direct items in this section */
          (section.items || []).filter(function(k) { return visibleKeys.has(k); }).map(function(key) {
            const item = window.FS.NAV_ITEMS[key];
            const isActive = currentRoute === item.path;
            return React.createElement(NavItem, {
              key: key,
              navKey: key,
              label: item.label,
              isActive: isActive,
              isCollapsed: isCollapsed,
              onClick: function() { onNavigate(item.path); },
            });
          }),

          /* Subgroups (e.g. Compliance) */
          (section.subgroups || []).map(function(group) {
            const visibleInGroup = group.items.filter(function(k) { return visibleKeys.has(k); });
            if (visibleInGroup.length === 0) return null;
            return React.createElement(React.Fragment, { key: group.key },
              React.createElement(SubgroupLabel, { label: group.label, isCollapsed: isCollapsed }),
              visibleInGroup.map(function(key) {
                const item = window.FS.NAV_ITEMS[key];
                const isActive = currentRoute === item.path;
                return React.createElement(NavItem, {
                  key: key,
                  navKey: key,
                  label: item.label,
                  isActive: isActive,
                  isCollapsed: isCollapsed,
                  isSubItem: true,
                  onClick: function() { onNavigate(item.path); },
                });
              }),
            );
          }),

          /* Trailing items (after subgroup) */
          (section.trailingItems || []).filter(function(k) { return visibleKeys.has(k); }).map(function(key) {
            const item = window.FS.NAV_ITEMS[key];
            const isActive = currentRoute === item.path;
            return React.createElement(NavItem, {
              key: key,
              navKey: key,
              label: item.label,
              isActive: isActive,
              isCollapsed: isCollapsed,
              onClick: function() { onNavigate(item.path); },
            });
          }),

        );
      }),
    ),

    /* Settings — sticky footer ABOVE UserArea, OUTSIDE scroll area */
    visibleKeys.has('settings') ? React.createElement('div', { style: settingsAreaStyle },
      React.createElement(NavItem, {
        navKey: 'settings',
        label: 'Settings',
        isActive: currentRoute === '/settings',
        isCollapsed: isCollapsed,
        onClick: function() { onNavigate('/settings'); },
      }),
    ) : null,

    /* User area sticky bottom */
    React.createElement(UserArea, { user: user, isCollapsed: isCollapsed }),
  );
}

if (!window.FieldSight) window.FieldSight = {};
window.FieldSight.LeftNav = LeftNav;
window.FieldSight.NavIcon = NavIcon;
