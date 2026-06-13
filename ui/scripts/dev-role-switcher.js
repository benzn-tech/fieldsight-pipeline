/* ==========================================================================
   FieldSight Dev Role Switcher — React/JSX
   Floating panel, shown when ?dev=1 or forced via showDevSwitcher prop.
   Sprint 3 P-05 replaced the native <select> with a custom popover so
   the dropdown chrome matches the dev panel and the selected option
   is visibly distinct (the native select rendered an opaque-black
   menu that hid the active row on macOS Chrome / Safari).
   Exported to window.FieldSight.DevRoleSwitcher
   ========================================================================== */

/* global React, window */

function DevRoleSwitcher() {
  const t = window.FS.tokens;

  const [minimized, setMinimized] = React.useState(false);
  const [selectedRole, setSelectedRole] = React.useState(
    () => window.AuthMock.currentUser.role
  );
  const [isAdmin, setIsAdmin] = React.useState(
    () => window.AuthMock.currentUser.isAdmin || false
  );
  const [currentUser, setCurrentUser] = React.useState(
    () => window.AuthMock.currentUser
  );

  /* Custom dropdown state (P-05). */
  const [dropOpen, setDropOpen] = React.useState(false);
  const triggerRef = React.useRef(null);
  const dropRef    = React.useRef(null);

  React.useEffect(() => {
    return window.AuthMock.onChange(u => {
      setCurrentUser({ ...u });
      setSelectedRole(u.role);
      setIsAdmin(!!u.isAdmin);
    });
  }, []);

  /* Close dropdown on outside click + Escape. */
  React.useEffect(() => {
    if (!dropOpen) return;
    function onDown(e) {
      if (!dropRef.current) return;
      if (dropRef.current.contains(e.target)) return;
      if (triggerRef.current && triggerRef.current.contains(e.target)) return;
      setDropOpen(false);
    }
    function onKey(e) { if (e.key === 'Escape') setDropOpen(false); }
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown',   onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown',   onKey);
    };
  }, [dropOpen]);

  function apply() {
    window.AuthMock.setRole(selectedRole);
    window.AuthMock.setAdmin(isAdmin);
  }

  const allRoles = Object.entries(window.FS.ROLES).map(([key, r]) => ({
    key,
    label: r.label,
    level: r.level,
    specialist: !!r.specialist,
  }));

  const hierarchy  = allRoles.filter(r => !r.specialist).sort((a, b) => a.level - b.level);
  const specialist = allRoles.filter(r =>  r.specialist);

  const visibleItems = isAdmin
    ? Object.values(window.FS.NAV_ITEMS).map(i => i.label)
    : window.FS.getVisibleNavItems(
        { role: selectedRole, isAdmin: false }
      ).map(i => i.label);

  const shownItems = visibleItems.slice(0, 8);
  const extraCount = visibleItems.length - shownItems.length;

  /* ---- styles ---- */
  const panelStyle = {
    position: 'fixed',
    bottom: '16px',
    right: '16px',
    width: '280px',
    background: 'rgba(15,22,35,0.96)',
    border: '1px solid rgba(255,255,255,0.10)',
    borderRadius: '10px',
    boxShadow: t.shadow.lg,
    fontFamily: t.typography.fontFamily.sans,
    fontSize: t.typography.fontSize.sm,
    color: 'rgba(255,255,255,0.85)',
    zIndex: t.zIndex.toast,
    overflow: 'visible',  /* allow dropdown to escape */
    backdropFilter: 'blur(8px)',
  };

  const headerStyle = {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '10px 12px',
    borderBottom: minimized ? 'none' : '1px solid rgba(255,255,255,0.08)',
    cursor: 'pointer',
  };

  const labelStyle = {
    fontSize: '11px',
    fontWeight: 600,
    letterSpacing: '0.06em',
    textTransform: 'uppercase',
    color: t.colors.accent[400],
  };

  const bodyStyle = {
    padding: '12px',
    display: 'flex',
    flexDirection: 'column',
    gap: '10px',
  };

  /* The dropdown trigger looks like the old <select> — translucent
     dark surface, light text, chevron on the right. */
  const triggerStyle = {
    width: '100%',
    background: 'rgba(255,255,255,0.07)',
    border: '1px solid ' + (dropOpen ? 'rgba(255,107,53,0.5)' : 'rgba(255,255,255,0.12)'),
    borderRadius: '6px',
    color: '#fff',
    padding: '7px 10px',
    fontSize: t.typography.fontSize.sm,
    fontFamily: t.typography.fontFamily.sans,
    cursor: 'pointer',
    textAlign: 'left',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: '8px',
  };

  /* Popover — opens BELOW the trigger, matching the panel's
     translucent dark blue (not a black browser native chrome). */
  const dropStyle = {
    position: 'absolute',
    top: '100%',
    left: 0,
    right: 0,
    marginTop: '4px',
    background: 'rgba(20,28,45,0.98)',
    border: '1px solid rgba(255,255,255,0.12)',
    borderRadius: '6px',
    boxShadow: t.shadow.lg,
    overflow: 'hidden',
    zIndex: t.zIndex.toast + 1,
    maxHeight: '280px',
    overflowY: 'auto',
  };

  const dropGroupLabelStyle = {
    padding: '8px 12px 4px',
    fontSize: '10px',
    fontWeight: 700,
    letterSpacing: '0.08em',
    textTransform: 'uppercase',
    color: 'rgba(255,255,255,0.40)',
  };

  function dropOptionStyle(active, hovered) {
    return {
      padding: '8px 12px',
      fontSize: t.typography.fontSize.sm,
      color: active ? t.colors.accent[300] : '#fff',
      background: active
        ? 'rgba(255,107,53,0.12)'
        : (hovered ? 'rgba(255,255,255,0.06)' : 'transparent'),
      cursor: 'pointer',
      display: 'flex',
      alignItems: 'center',
      gap: '8px',
      borderLeft: active ? '2px solid ' + t.colors.accent[500] : '2px solid transparent',
    };
  }

  const checkRowStyle = {
    display: 'flex',
    alignItems: 'center',
    gap: '8px',
    fontSize: t.typography.fontSize.sm,
    color: 'rgba(255,255,255,0.7)',
    cursor: 'pointer',
  };

  const metaBoxStyle = {
    background: 'rgba(255,255,255,0.05)',
    borderRadius: '6px',
    padding: '8px 10px',
    fontSize: '11px',
    color: 'rgba(255,255,255,0.5)',
    lineHeight: 1.6,
  };

  const applyBtnStyle = {
    width: '100%',
    padding: '8px',
    background: t.colors.accent[500],
    color: '#fff',
    border: 'none',
    borderRadius: '6px',
    fontWeight: 600,
    fontSize: t.typography.fontSize.sm,
    cursor: 'pointer',
    fontFamily: t.typography.fontFamily.sans,
    transition: 'background 100ms ease-out',
  };

  const metaLanding = window.FS.ROLES[selectedRole]?.defaultLanding || '/today';
  const selectedRoleObj = window.FS.ROLES[selectedRole];
  const triggerLabel = selectedRoleObj
    ? (selectedRoleObj.specialist
        ? selectedRoleObj.label
        : 'L' + selectedRoleObj.level + ' · ' + selectedRoleObj.label)
    : selectedRole;

  return React.createElement('div', { style: panelStyle, className: 'fs-dev-switcher' },

    /* Header — click to minimize */
    React.createElement('div', {
      style: headerStyle,
      onClick: () => setMinimized(m => !m),
    },
      React.createElement('span', { style: labelStyle }, '⚙ Dev · Role Switcher'),
      React.createElement('span', {
        style: {
          fontSize: '10px',
          fontWeight: '600',
          padding: '1px 5px',
          borderRadius: '3px',
          marginRight: '4px',
          background: (window.FS && window.FS.api && !window.FS.api.useMocks) ? '#22c55e' : 'rgba(255,255,255,0.15)',
          color: (window.FS && window.FS.api && !window.FS.api.useMocks) ? '#fff' : 'rgba(255,255,255,0.5)',
        },
      }, (window.FS && window.FS.api && !window.FS.api.useMocks) ? 'LIVE' : 'MOCK'),
      React.createElement('span', {
        style: { color: 'rgba(255,255,255,0.4)', fontSize: '11px' },
      }, minimized ? '▲' : '▼'),
    ),

    !minimized && React.createElement('div', { style: bodyStyle },

      /* Custom role dropdown */
      React.createElement('div', null,
        React.createElement('div', {
          style: { fontSize: '11px', color: 'rgba(255,255,255,0.4)', marginBottom: '5px' },
        }, 'Role'),
        React.createElement('div', { style: { position: 'relative' } },
          React.createElement('button', {
            ref:     triggerRef,
            type:    'button',
            style:   triggerStyle,
            onClick: function () { setDropOpen(o => !o); },
            'aria-haspopup': 'listbox',
            'aria-expanded': dropOpen,
          },
            React.createElement('span', null, triggerLabel),
            React.createElement('span', {
              style: { color: 'rgba(255,255,255,0.4)', fontSize: '10px' },
            }, '▾'),
          ),
          dropOpen ? React.createElement('div', {
            ref:   dropRef,
            style: dropStyle,
            role:  'listbox',
          },
            React.createElement('div', { style: dropGroupLabelStyle }, 'Hierarchy'),
            hierarchy.map(r => React.createElement(DropOption, {
              key:      r.key,
              label:    'L' + r.level + ' · ' + r.label,
              active:   r.key === selectedRole,
              onSelect: function () { setSelectedRole(r.key); setDropOpen(false); },
              styleFor: dropOptionStyle,
            })),
            React.createElement('div', { style: dropGroupLabelStyle }, 'Specialists'),
            specialist.map(r => React.createElement(DropOption, {
              key:      r.key,
              label:    r.label,
              active:   r.key === selectedRole,
              onSelect: function () { setSelectedRole(r.key); setDropOpen(false); },
              styleFor: dropOptionStyle,
            })),
          ) : null,
        ),
      ),

      /* Admin override checkbox */
      React.createElement('label', { style: checkRowStyle },
        React.createElement('input', {
          type: 'checkbox',
          checked: isAdmin,
          onChange: e => setIsAdmin(e.target.checked),
          style: { accentColor: t.colors.accent[500], cursor: 'pointer' },
        }),
        'Admin override (all permissions)',
      ),

      /* Meta box */
      React.createElement('div', { style: metaBoxStyle },
        React.createElement('div', null,
          React.createElement('span', { style: { color: 'rgba(255,255,255,0.35)' } }, 'Landing: '),
          metaLanding,
        ),
        React.createElement('div', null,
          React.createElement('span', { style: { color: 'rgba(255,255,255,0.35)' } }, 'Nav items: '),
          visibleItems.length,
        ),
        React.createElement('div', {
          style: { marginTop: '4px', display: 'flex', flexWrap: 'wrap', gap: '4px' },
        },
          shownItems.map(label =>
            React.createElement('span', {
              key: label,
              style: {
                padding: '1px 6px',
                borderRadius: '4px',
                background: 'rgba(255,107,53,0.15)',
                color: t.colors.accent[300],
                fontSize: '10px',
              },
            }, label)
          ),
          extraCount > 0 && React.createElement('span', {
            style: {
              padding: '1px 6px',
              borderRadius: '4px',
              background: 'rgba(255,255,255,0.06)',
              color: 'rgba(255,255,255,0.35)',
              fontSize: '10px',
            },
          }, `+${extraCount} more`),
        ),
      ),

      /* Apply button */
      React.createElement('button', {
        style: applyBtnStyle,
        onClick: apply,
        onMouseEnter: e => { e.target.style.background = t.colors.accent[600]; },
        onMouseLeave: e => { e.target.style.background = t.colors.accent[500]; },
      }, 'Apply Role'),

      /* Current active user indicator */
      React.createElement('div', {
        style: { fontSize: '10px', color: 'rgba(255,255,255,0.25)', textAlign: 'center' },
      }, `Active: ${currentUser.name} · ${currentUser.role}${currentUser.isAdmin ? ' (admin)' : ''}`),

    ),
  );
}

/* Single option row inside the custom dropdown — handles its own
   hover state so we don't have to lift it into DevRoleSwitcher. */
function DropOption({ label, active, onSelect, styleFor }) {
  const [hovered, setHovered] = React.useState(false);
  return React.createElement('div', {
    role:    'option',
    'aria-selected': active,
    style:   styleFor(active, hovered),
    onClick: onSelect,
    onMouseEnter: () => setHovered(true),
    onMouseLeave: () => setHovered(false),
  },
    active ? '✓' : React.createElement('span', { style: { display: 'inline-block', width: '10px' } }),
    label,
  );
}

if (!window.FieldSight) window.FieldSight = {};
window.FieldSight.DevRoleSwitcher = DevRoleSwitcher;
