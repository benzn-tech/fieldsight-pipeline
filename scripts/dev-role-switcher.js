/* ==========================================================================
   FieldSight Dev Role Switcher — React/JSX
   Floating panel, shown when ?dev=1 or forced via showDevSwitcher prop.
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

  React.useEffect(() => {
    return window.AuthMock.onChange(u => {
      setCurrentUser({ ...u });
      setSelectedRole(u.role);
      setIsAdmin(!!u.isAdmin);
    });
  }, []);

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
    overflow: 'hidden',
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

  const selectStyle = {
    width: '100%',
    background: 'rgba(255,255,255,0.07)',
    border: '1px solid rgba(255,255,255,0.12)',
    borderRadius: '6px',
    color: '#fff',
    padding: '7px 10px',
    fontSize: t.typography.fontSize.sm,
    fontFamily: t.typography.fontFamily.sans,
    cursor: 'pointer',
    appearance: 'none',
  };

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

  return React.createElement('div', { style: panelStyle, className: 'fs-dev-switcher' },

    /* Header — click to minimize */
    React.createElement('div', {
      style: headerStyle,
      onClick: () => setMinimized(m => !m),
    },
      React.createElement('span', { style: labelStyle }, '⚙ Dev · Role Switcher'),
      React.createElement('span', {
        style: { color: 'rgba(255,255,255,0.4)', fontSize: '11px' },
      }, minimized ? '▲' : '▼'),
    ),

    !minimized && React.createElement('div', { style: bodyStyle },

      /* Role selector */
      React.createElement('div', null,
        React.createElement('div', {
          style: { fontSize: '11px', color: 'rgba(255,255,255,0.4)', marginBottom: '5px' },
        }, 'Role'),
        React.createElement('div', { style: { position: 'relative' } },
          React.createElement('select', {
            style: selectStyle,
            value: selectedRole,
            onChange: e => setSelectedRole(e.target.value),
          },
            /* Hierarchy roles first */
            React.createElement('optgroup', { label: 'Hierarchy' },
              allRoles.filter(r => !r.specialist).sort((a, b) => a.level - b.level)
                .map(r => React.createElement('option', { key: r.key, value: r.key },
                  `L${r.level} · ${r.label}`
                ))
            ),
            /* Specialist roles */
            React.createElement('optgroup', { label: 'Specialists' },
              allRoles.filter(r => r.specialist)
                .map(r => React.createElement('option', { key: r.key, value: r.key },
                  r.label
                ))
            ),
          ),
          /* Chevron icon */
          React.createElement('span', {
            style: {
              position: 'absolute', right: '10px', top: '50%',
              transform: 'translateY(-50%)',
              pointerEvents: 'none', color: 'rgba(255,255,255,0.4)', fontSize: '10px',
            },
          }, '▾'),
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

if (!window.FieldSight) window.FieldSight = {};
window.FieldSight.DevRoleSwitcher = DevRoleSwitcher;
