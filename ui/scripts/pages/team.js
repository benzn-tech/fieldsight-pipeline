/* ==========================================================================
   FieldSight Team Page — Sprint 7.1 + 7.2 + 9.B
   --------------------------------------------------------------------------
   /team — read-only people directory, grouped by site.

   Middle column:
     • Header (title + N users · M sites meta)
     • KPI strip: total users · active sites · distinct roles
     • Body: groups ordered descending by user count; each group has a
       site header + user rows (Avatar + name + role badge + secondary
       sites pill)

   Right detail (7.2):
     • Large Avatar + name + role badge + scope pill
     • Field rows: Primary site · All sites · Device ID
     • Footer: "View their reports" → /timeline, "View their tasks" → /tasks?user=

   Sprint 9 Track B additions:
     • Site-scoping for PM callers — `getCallerManagedSites()` reads
       `caller.managed_sites` if present, else looks the user up in
       fixtures by name / folder_name. PM sees only people on their
       managed sites; admin / gm / director / specialist roles still
       see the full directory.
     • PM-only "Reassign to another site" right-detail action — opens
       an inline modal with the PM's managed_sites as radio options.
       Mock-only mutation gated on `useMocks` (PLAN §3 trap pattern).

   Permission gate: Provider checks FS.can(caller, 'user:manage').
   Sprint 9 grants project_manager `user:manage:project`, so PM now
   passes the gate (was gm + director + admin only).

   Registers as window.FieldSight.PAGES['/team']
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  /* ---------- Helpers --------------------------------------------------- */

  function roleLabel(role) {
    var labels = {
      worker:       'Worker',
      site_manager: 'Site Manager',
      pm:           'Project Manager',
      project_manager: 'Project Manager',
      gm:           'General Manager',
      admin:        'Admin',
      regional_manager: 'Regional Manager',
      exec:         'Executive',
    };
    return labels[role] || (role ? role.replace(/_/g, ' ') : 'Unknown');
  }

  /* Sprint 9 B.2 — given the AuthMock current user, return the list
     of site_ids the caller is permitted to see members of. Behaviour:
       • admin / gm / director → null  (no filter; full directory)
       • PM with caller.managed_sites already populated → that list
       • PM whose name matches a fixture user with managed_sites or
         sites → use that
       • Anyone else (worker / site_manager) → fall back to their own
         primary_site (single-site scope)
     A null return = "do NOT filter". */
  function getCallerManagedSites(caller) {
    if (!caller || caller.isAdmin) return null;
    var role = caller.role;
    if (role === 'admin' || role === 'gm' || role === 'director'
        || role === 'hse_manager' || role === 'quality_manager'
        || role === 'construction_manager') {
      return null;
    }
    if (caller.managed_sites && caller.managed_sites.length > 0) {
      return caller.managed_sites.slice();
    }
    /* Fall back to fixture lookup so the dev role switcher still shows
       sensible scope when admin flips to project_manager mid-session
       without a real auth payload. */
    var fx = (window.FieldSight && window.FieldSight.fixtures
      && window.FieldSight.fixtures.sites) || {};
    var match = (fx.users || []).filter(function (u) {
      return (u.name && u.name === caller.name)
        || (u.folder_name && u.folder_name === caller.name);
    })[0];
    if (match) {
      if (match.managed_sites && match.managed_sites.length > 0) {
        return match.managed_sites.slice();
      }
      if (match.sites && match.sites.length > 0) {
        return match.sites.slice();
      }
      if (match.primary_site) return [match.primary_site];
    }
    /* Last resort — for project_manager role we must scope to
       SOMETHING; default to the first PM's managed_sites in fixtures
       to keep the dev experience meaningful. Real auth never falls
       through here. */
    if (role === 'pm' || role === 'project_manager') {
      var anyPm = (fx.users || []).filter(function (u) {
        return (u.role === 'pm' || u.role === 'project_manager')
          && u.managed_sites && u.managed_sites.length > 0;
      })[0];
      if (anyPm) return anyPm.managed_sites.slice();
    }
    return null;
  }

  /* Returns true if a user record overlaps any of the given site_ids. */
  function userOnSites(user, siteIds) {
    if (!siteIds || siteIds.length === 0) return true;
    if (user.primary_site && siteIds.indexOf(user.primary_site) >= 0) return true;
    return (user.sites || []).some(function (s) {
      return siteIds.indexOf(s) >= 0;
    });
  }

  /* Deterministic sort key: descending by user count, then site name. */
  function siteGroupSortKey(group) {
    return -group.users.length;
  }

  function groupUsersBySite(users) {
    var map = {};
    (users || []).forEach(function (u) {
      var key = u.primary_site || '__none__';
      if (!map[key]) map[key] = { site_id: key, users: [] };
      map[key].users.push(u);
    });
    return Object.values(map).sort(function (a, b) {
      var diff = siteGroupSortKey(a) - siteGroupSortKey(b);
      if (diff !== 0) return diff;
      return (a.site_id || '').localeCompare(b.site_id || '');
    });
  }

  function siteDisplayName(siteId) {
    var fix = (window.FieldSight && window.FieldSight.fixtures && window.FieldSight.fixtures.sites) || {};
    var match = (fix.sites || []).filter(function (s) { return s.site_id === siteId; })[0];
    return match ? match.name : siteId || 'Unknown site';
  }

  function countDistinctRoles(users) {
    var seen = {};
    (users || []).forEach(function (u) { if (u.role) seen[u.role] = true; });
    return Object.keys(seen).length;
  }

  function readRouteParams() {
    var r = window.FS && window.FS.Router && window.FS.Router.getCurrentRoute();
    return (r && r.params) || {};
  }

  /* ---------- TeamContext ------------------------------------------------ */

  var TeamContext = React.createContext(null);

  function TeamProvider(props) {
    var caller = (window.AuthMock && window.AuthMock.currentUser) || {};
    var depKey = (caller.name || '') + '|' + (caller.role || '') + '|' + (caller.isAdmin ? 'admin' : '');

    var refState = React.useState({ status: 'loading' });
    var state    = refState[0];
    var setState = refState[1];

    var retryRef   = React.useState(0);
    var retryCount = retryRef[0];
    var setRetry   = retryRef[1];

    React.useEffect(function () {
      /* Permission gate */
      if (!window.FS.can(caller, 'user:manage')) {
        setState({ status: 'access_denied', message: 'You don\'t have permission to view the team directory.' });
        return undefined;
      }

      var cancelled = false;
      setState({ status: 'loading' });

      window.FS.api.sites.getUsers().then(function (res) {
        if (cancelled) return;
        if (res && res._accessDenied) {
          setState({ status: 'access_denied', message: res.error });
          return;
        }
        var users = (res && res.users) || [];

        /* Sprint 9 B.2 — apply scope filter. PM sees only people on
           managed sites. Admin / gm / director / specialists pass
           through (getCallerManagedSites returns null). */
        var managedSites = getCallerManagedSites(caller);
        var scoped = (managedSites === null)
          ? users
          : users.filter(function (u) { return userOnSites(u, managedSites); });

        var groups = groupUsersBySite(scoped);
        setState({
          status:       'ok',
          users:        scoped,
          groups:       groups,
          managedSites: managedSites,                 /* null on full-directory paths */
          callerScoped: managedSites !== null,
          totals: {
            users:    scoped.length,
            sites:    groups.filter(function (g) { return g.site_id !== '__none__'; }).length,
            roles:    countDistinctRoles(scoped),
          },
        });
      }).catch(function (err) {
        if (cancelled) return;
        setState({ status: 'error', error: { code: (err && err.status) || 0, message: (err && err.message) || 'Could not load team', retryable: true }, retry: function () { setRetry(function (n) { return n + 1; }); } });
      });

      return function () { cancelled = true; };
    }, [depKey, retryCount]);

    /* Sprint 9 B.3 — local override for reassign-to-site. Map of
       device_id → { primary_site }. Merged into render output so the
       UI reflects optimistic mutations without re-fetching. */
    var refOverrides = React.useState({});
    var overrides    = refOverrides[0];
    var setOverrides = refOverrides[1];

    function applyReassign(deviceId, newSiteId) {
      setOverrides(function (prev) {
        var next = Object.assign({}, prev);
        next[deviceId] = Object.assign({}, next[deviceId] || {}, {
          primary_site: newSiteId,
        });
        return next;
      });
      /* Re-derive groups from current users + new override. */
      setState(function (s) {
        if (s.status !== 'ok') return s;
        var patched = (s.users || []).map(function (u) {
          if (u.device_id !== deviceId) return u;
          return Object.assign({}, u, { primary_site: newSiteId,
            sites: u.sites && u.sites.indexOf(newSiteId) >= 0 ? u.sites
              : (u.sites || []).concat([newSiteId]) });
        });
        return Object.assign({}, s, {
          users:  patched,
          groups: groupUsersBySite(patched),
        });
      });
      /* Toast confirmation. */
      if (window.FS && window.FS.toast) {
        window.FS.toast.show({
          message: 'Reassigned to ' + siteDisplayName(newSiteId),
          tone:    'success',
        });
      }
    }

    var ctx = {
      state:       state,
      caller:      caller,
      overrides:   overrides,
      applyReassign: applyReassign,
    };
    return React.createElement(TeamContext.Provider, { value: ctx }, props.children);
  }

  /* ---------- TeamMiddleColumn ------------------------------------------ */

  function TeamMiddleColumn(props) {
    var fs        = window.FieldSight;
    var Avatar    = fs.Avatar;
    var Badge     = fs.Badge;
    var onSelect  = props.onSelect || function () {};

    var ctx = React.useContext(TeamContext);
    if (!ctx) {
      console.warn('[TeamMiddleColumn] TeamContext missing');
      return null;
    }
    var state = ctx.state;

    if (state.status === 'loading') {
      return React.createElement('div', { className: 'fs-team' },
        React.createElement('div', { className: 'fs-team__loading' }, 'Loading team…'));
    }

    if (state.status === 'error') {
      var ErrorBanner = window.FieldSight.ErrorBanner;
      return React.createElement('div', { className: 'fs-team' },
        ErrorBanner
          ? React.createElement(ErrorBanner, {
              message:   (state.error && state.error.message) || 'Could not load team',
              retryable: true,
              onRetry:   state.retry,
            })
          : React.createElement('div', { className: 'fs-team__empty' },
              (state.error && state.error.message) || 'Could not load team'));
    }

    if (state.status === 'access_denied') {
      var AccessDenied = fs.AccessDenied;
      return React.createElement('div', { className: 'fs-team' },
        AccessDenied
          ? React.createElement(AccessDenied, {
              scope:   'the team directory',
              message: state.message,
            })
          : React.createElement('div', null, 'Access denied.'));
    }

    var totals   = state.totals  || {};
    var groups   = state.groups  || [];
    var selectedId = props.selectedItem && props.selectedItem.kind === 'user'
      ? props.selectedItem.device_id
      : null;

    var metaLine = totals.users + ' ' + (totals.users === 1 ? 'person' : 'people')
      + ' · ' + totals.sites + ' ' + (totals.sites === 1 ? 'site' : 'sites');

    /* Sprint 9 B.2 — scope caption when caller is a PM. */
    var scopeCaption = null;
    if (state.callerScoped && state.managedSites && state.managedSites.length > 0) {
      var siteList = state.managedSites.map(siteDisplayName).join(', ');
      scopeCaption = 'Scoped to your managed sites · ' + siteList;
    }

    return React.createElement('div', { className: 'fs-team' },

      React.createElement('div', { className: 'fs-team__header' },
        React.createElement('h2', { className: 'fs-team__title' }, 'Team'),
        React.createElement('div', { className: 'fs-team__subtitle' }, metaLine),
        scopeCaption
          ? React.createElement('div', { className: 'fs-team__scope-caption' }, scopeCaption)
          : null,
      ),

      /* KPI strip */
      React.createElement('div', { className: 'fs-team__kpi-strip' },
        React.createElement('div', { className: 'fs-team__kpi' },
          React.createElement('div', { className: 'fs-team__kpi-value' }, totals.users),
          React.createElement('div', { className: 'fs-team__kpi-label' }, 'People')),
        React.createElement('div', { className: 'fs-team__kpi' },
          React.createElement('div', { className: 'fs-team__kpi-value' }, totals.sites),
          React.createElement('div', { className: 'fs-team__kpi-label' }, 'Active sites')),
        React.createElement('div', { className: 'fs-team__kpi' },
          React.createElement('div', { className: 'fs-team__kpi-value' }, totals.roles),
          React.createElement('div', { className: 'fs-team__kpi-label' }, 'Roles')),
      ),

      /* Site groups */
      React.createElement('div', { className: 'fs-team__groups' },
        groups.length === 0
          ? React.createElement('div', { className: 'fs-team__empty' },
              'No team members on your managed sites yet.')
          : groups.map(function (group) {
              return React.createElement('div', { key: group.site_id, className: 'fs-team__group' },

                React.createElement('div', { className: 'fs-team__group-header' },
                  siteDisplayName(group.site_id),
                  React.createElement('span', { className: 'fs-team__group-count' },
                    group.users.length),
                ),

                React.createElement('div', { className: 'fs-team__user-list' },
                  group.users.map(function (u) {
                    var isSelected = selectedId === u.device_id;
                    var extraSites = (u.sites || []).filter(function (s) { return s !== u.primary_site; });
                    return React.createElement('button', {
                      key:       u.device_id,
                      type:      'button',
                      className: 'fs-team__user-row' + (isSelected ? ' fs-team__user-row--selected' : ''),
                      onClick:   function () {
                        onSelect({ kind: 'user', id: 'user_' + u.device_id, device_id: u.device_id, user: u });
                      },
                    },
                      Avatar ? React.createElement(Avatar, { name: u.name, size: 'sm' }) : null,
                      React.createElement('div', { className: 'fs-team__user-info' },
                        React.createElement('div', { className: 'fs-team__user-name' }, u.name),
                        React.createElement('div', { className: 'fs-team__user-meta' },
                          Badge ? React.createElement(Badge, {
                            tone: 'neutral', size: 'xs', variant: 'subtle',
                          }, roleLabel(u.role)) : roleLabel(u.role),
                          extraSites.length > 0
                            ? React.createElement('span', { className: 'fs-team__extra-sites' },
                                '+' + extraSites.length + ' site' + (extraSites.length > 1 ? 's' : ''))
                            : null,
                        ),
                      ),
                    );
                  }),
                ),
              );
            }),
      ),
    );
  }

  /* ---------- ReassignModal — Sprint 9 B.3 ------------------------------ */

  function ReassignModal(props) {
    var Modal     = window.FieldSight && window.FieldSight.ModalOverlay;
    var u         = props.user;
    var onClose   = props.onClose;
    var onSubmit  = props.onSubmit;
    var managedSites = props.managedSites || [];

    var refSel = React.useState(u.primary_site || managedSites[0] || '');
    var picked = refSel[0];
    var setPicked = refSel[1];

    var refStatus = React.useState('idle');  /* 'idle' | 'submitting' */
    var status = refStatus[0];
    var setStatus = refStatus[1];

    function handleSubmit(e) {
      if (e) e.preventDefault();
      if (!picked || picked === u.primary_site) {
        if (onClose) onClose();
        return;
      }
      setStatus('submitting');
      /* Mock-only mutation in Sprint 9 (no /api/users PATCH yet).
         Live path would: PATCH /api/users/{device_id} { primary_site:
         picked }; on 200 → call onSubmit; on failure → toast +
         setStatus('idle'). */
      setTimeout(function () {
        if (onSubmit) onSubmit(picked);
      }, 200);
    }

    var content = React.createElement('form', {
      className: 'fs-team-reassign__form',
      onSubmit:  handleSubmit,
    },
      React.createElement('p', { className: 'fs-team-reassign__lead' },
        'Move ', React.createElement('strong', null, u.name),
        ' to a different primary site within your managed projects.'),

      React.createElement('div', { className: 'fs-team-reassign__options', role: 'radiogroup', 'aria-label': 'Pick a primary site' },
        managedSites.map(function (siteId) {
          var checked = picked === siteId;
          return React.createElement('label', {
            key:       siteId,
            className: 'fs-team-reassign__option' + (checked ? ' fs-team-reassign__option--checked' : ''),
          },
            React.createElement('input', {
              type:    'radio',
              name:    'fs-team-reassign-site',
              value:   siteId,
              checked: checked,
              onChange: function () { setPicked(siteId); },
            }),
            React.createElement('span', { className: 'fs-team-reassign__option-text' },
              siteDisplayName(siteId)),
            siteId === u.primary_site
              ? React.createElement('span', { className: 'fs-team-reassign__option-badge' }, 'current')
              : null,
          );
        }),
      ),

      React.createElement('div', { className: 'fs-team-reassign__actions' },
        React.createElement('button', {
          type:      'button',
          className: 'fs-team-reassign__btn fs-team-reassign__btn--ghost',
          onClick:   onClose,
          disabled:  status === 'submitting',
        }, 'Cancel'),
        React.createElement('button', {
          type:      'submit',
          className: 'fs-team-reassign__btn fs-team-reassign__btn--primary',
          disabled:  status === 'submitting' || !picked || picked === u.primary_site,
        }, status === 'submitting' ? 'Saving…' : 'Reassign'),
      ),
    );

    if (Modal) {
      return React.createElement(Modal, {
        title:   'Reassign to another site',
        onClose: onClose,
      }, content);
    }
    /* Bare-fallback when ModalOverlay isn't loaded — render an inline
       card so the action still works in components-preview etc. */
    return React.createElement('div', { className: 'fs-team-reassign__inline' },
      React.createElement('div', { className: 'fs-team-reassign__title' }, 'Reassign to another site'),
      content,
    );
  }

  /* ---------- TeamRightDetail — Sprint 7.2 + 9.B ------------------------ */

  function TeamRightDetail(props) {
    var fs      = window.FieldSight;
    var Avatar  = fs.Avatar;
    var Badge   = fs.Badge;
    var IconBtn = fs.IconButton;

    var ctx = React.useContext(TeamContext);
    var sel = props.selectedItem;

    var refModalOpen = React.useState(false);
    var modalOpen    = refModalOpen[0];
    var setModalOpen = refModalOpen[1];

    if (!sel || sel.kind !== 'user') {
      return React.createElement('div', { className: 'fs-team-detail__placeholder' },
        React.createElement('div', { className: 'fs-team-detail__placeholder-title' },
          'Select a person'),
        React.createElement('div', { className: 'fs-team-detail__placeholder-body' },
          'Pick anyone from the list to view their profile.'),
      );
    }

    /* Use the LIVE user from context (post-override) so reassign
       propagates here even though `sel.user` was captured at click. */
    var liveUser = sel.user;
    if (ctx && ctx.state && ctx.state.users) {
      var hit = ctx.state.users.filter(function (u) { return u.device_id === sel.device_id; })[0];
      if (hit) liveUser = hit;
    }
    var u = liveUser;

    var allSiteNames = (u.sites || []).map(siteDisplayName).join(', ') || '—';
    var scopePrimary = siteDisplayName(u.primary_site);
    var today        = window.FS.api && window.FS.api.todayNZDT ? window.FS.api.todayNZDT() : '';

    function navReports() {
      var qs = '?date=' + encodeURIComponent(today) + '&user=' + encodeURIComponent(u.folder_name || '');
      window.FS.Router.navigate('/timeline' + qs);
    }

    function navTasks() {
      window.FS.Router.navigate('/tasks?user=' + encodeURIComponent(u.folder_name || ''));
    }

    /* Sprint 9 B.3 — show "Reassign to another site" only when the
       caller is a PM (or admin) AND the target user is on one of the
       caller's managed sites. */
    var caller       = ctx ? ctx.caller : ((window.AuthMock && window.AuthMock.currentUser) || {});
    var managedSites = (ctx && ctx.state && ctx.state.managedSites) || null;
    var canReassign  = false;
    if (caller && (caller.isAdmin || caller.role === 'project_manager' || caller.role === 'pm')
        && managedSites && managedSites.length > 1) {
      canReassign = userOnSites(u, managedSites);
    }

    return React.createElement('div', { className: 'fs-team-detail' },

      React.createElement('div', { className: 'fs-team-detail__header' },
        React.createElement('div', { className: 'fs-team-detail__header-main' },
          Avatar ? React.createElement(Avatar, { name: u.name, size: 'lg' }) : null,
          React.createElement('div', { className: 'fs-team-detail__header-text' },
            React.createElement('h2', { className: 'fs-team-detail__name' }, u.name || '—'),
            React.createElement('div', { className: 'fs-team-detail__badges' },
              Badge ? React.createElement(Badge, { tone: 'neutral', size: 'sm', variant: 'subtle' },
                roleLabel(u.role)) : roleLabel(u.role),
              React.createElement('span', { className: 'fs-team-detail__scope' }, scopePrimary),
            ),
          ),
        ),
        IconBtn ? React.createElement(IconBtn, {
          icon: 'x', ariaLabel: 'Close detail', size: 'sm',
          onClick: function () { if (props.onClose) props.onClose(); },
        }) : null,
      ),

      /* Field rows */
      React.createElement('div', { className: 'fs-team-detail__fields' },
        React.createElement('div', { className: 'fs-team-detail__field' },
          React.createElement('div', { className: 'fs-team-detail__field-label' }, 'Primary site'),
          React.createElement('div', { className: 'fs-team-detail__field-value' }, scopePrimary),
        ),
        React.createElement('div', { className: 'fs-team-detail__field' },
          React.createElement('div', { className: 'fs-team-detail__field-label' }, 'All sites'),
          React.createElement('div', { className: 'fs-team-detail__field-value' }, allSiteNames),
        ),
        React.createElement('div', { className: 'fs-team-detail__field' },
          React.createElement('div', { className: 'fs-team-detail__field-label' }, 'Device ID'),
          React.createElement('div', { className: 'fs-team-detail__field-value fs-team-detail__field-value--mono' },
            u.device_id || '—'),
        ),
      ),

      /* Action footer */
      React.createElement('div', { className: 'fs-team-detail__actions' },
        React.createElement('button', {
          type:      'button',
          className: 'fs-team-detail__action-btn',
          onClick:   navReports,
          disabled:  !u.folder_name,
        }, 'View their reports'),
        React.createElement('button', {
          type:      'button',
          className: 'fs-team-detail__action-btn',
          onClick:   navTasks,
          disabled:  !u.folder_name,
        }, 'View their tasks'),
        canReassign ? React.createElement('button', {
          type:      'button',
          className: 'fs-team-detail__action-btn fs-team-detail__action-btn--primary',
          onClick:   function () { setModalOpen(true); },
        }, 'Reassign to another site') : null,
      ),

      /* Reassign modal — only mounts when open + caller is a PM */
      modalOpen ? React.createElement(ReassignModal, {
        user:         u,
        managedSites: managedSites || [],
        onClose:      function () { setModalOpen(false); },
        onSubmit:     function (newSite) {
          if (ctx && ctx.applyReassign) ctx.applyReassign(u.device_id, newSite);
          setModalOpen(false);
        },
      }) : null,
    );
  }

  /* ---------- Register --------------------------------------------------- */

  if (!window.FieldSight) window.FieldSight = {};
  if (!window.FieldSight.PAGES) window.FieldSight.PAGES = {};
  window.FieldSight.PAGES['/team'] = {
    Middle:   TeamMiddleColumn,
    Right:    TeamRightDetail,
    Provider: TeamProvider,
  };

})();
