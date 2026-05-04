/* ==========================================================================
   FieldSight Team Page — Sprint 7.1 + 7.2
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

   Permission gate: Provider checks FS.can(caller, 'user:manage').
   Nav already gates /team, but a direct URL hit should also render
   AccessDenied rather than leaking data.

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
      gm:           'General Manager',
      admin:        'Admin',
      regional_manager: 'Regional Manager',
      exec:         'Executive',
    };
    return labels[role] || (role ? role.replace(/_/g, ' ') : 'Unknown');
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
        var groups = groupUsersBySite(users);
        setState({
          status:  'ok',
          users:   users,
          groups:  groups,
          totals: {
            users:    users.length,
            sites:    groups.filter(function (g) { return g.site_id !== '__none__'; }).length,
            roles:    countDistinctRoles(users),
          },
        });
      }).catch(function (err) {
        if (cancelled) return;
        setState({ status: 'error', error: { code: (err && err.status) || 0, message: (err && err.message) || 'Could not load team', retryable: true }, retry: function () { setRetry(function (n) { return n + 1; }); } });
      });

      return function () { cancelled = true; };
    }, [depKey, retryCount]);

    var ctx = { state: state };
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

    return React.createElement('div', { className: 'fs-team' },

      React.createElement('div', { className: 'fs-team__header' },
        React.createElement('h2', { className: 'fs-team__title' }, 'Team'),
        React.createElement('div', { className: 'fs-team__subtitle' }, metaLine),
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
        groups.map(function (group) {
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

  /* ---------- TeamRightDetail — Sprint 7.2 ------------------------------ */

  function TeamRightDetail(props) {
    var fs      = window.FieldSight;
    var Avatar  = fs.Avatar;
    var Badge   = fs.Badge;
    var IconBtn = fs.IconButton;

    var sel = props.selectedItem;

    if (!sel || sel.kind !== 'user') {
      return React.createElement('div', { className: 'fs-team-detail__placeholder' },
        React.createElement('div', { className: 'fs-team-detail__placeholder-title' },
          'Select a person'),
        React.createElement('div', { className: 'fs-team-detail__placeholder-body' },
          'Pick anyone from the list to view their profile.'),
      );
    }

    var u           = sel.user;
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
      ),
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
