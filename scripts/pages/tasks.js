/* ==========================================================================
   FieldSight Tasks Page — Sprint 4.2 (PLAN.md Q-1 = yes)
   --------------------------------------------------------------------------
   /tasks — cross-day action-item dashboard.

   PLAN.md Q-1 commitment: frontend fan-out via the new
   `FS.api.tasks.getActionsResolvedRange` aggregator (Sprint 4.2).
   Backend endpoint `/api/actions/all?from=&to=&user=` is a
   nice-to-have; the aggregator is designed so a future drop-in
   replacement preserves the same row contract.

   Middle column:
     • Header: "Tasks" + perf caveat ("Aggregating last 14 days —
       slow at scale until backend aggregator ships")
     • TasksFilterChips: All / Mine / Open / Overdue / Done with counts
     • Filtered list (TaskCard rows). Worker role: "Mine" pre-selected.

   Right detail:
     • Action detail (text, owner, deadline, priority, source topic)
     • Audit history (checked_by, checked_at)
     • Mark complete CTA — wires through FS.api.actions.toggleAction
       (reuses Sprint 2.4 P-04 optimistic flow; on success removes
       the row from the page snapshot via TasksContext.removeRow)

   Architecture:
     • TasksProvider owns state via TasksContext (Sprint 3 P-07
       page-Provider pattern, mirrors Today/Sites/Activity).
     • Default range: last 14 days (matches PLAN.md Sprint 4.2 plan).

   Registers as window.FieldSight.PAGES['/tasks']
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  var DEFAULT_DAYS = 14;
  var PAGE_SIZE    = 25;

  /* ---------- Helpers --------------------------------------------------- */

  function readRouteParams() {
    var r = window.FS && window.FS.Router && window.FS.Router.getCurrentRoute();
    return (r && r.params) || {};
  }

  function parseHHMM(text) {
    if (!text) return null;
    var m = String(text).match(/(\d{1,2}):(\d{2})/);
    if (!m) return null;
    return (m[1].length === 1 ? '0' + m[1] : m[1]) + ':' + m[2];
  }

  function fmtDate(yyyymmdd) {
    if (!yyyymmdd) return '';
    var p = String(yyyymmdd).split('-').map(Number);
    if (p.length !== 3) return yyyymmdd;
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return p[2] + ' ' + months[p[1] - 1] + ' ' + p[0];
  }

  function fmtTimestamp(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toISOString().replace('T', ' ').slice(0, 16) + ' UTC';
  }

  /* Heuristic deadline → ISO date. The deadline field is free text per
     BACKEND-CONTEXT §5.1 ("Tomorrow 08:00", "Today 14:00", "By Friday",
     "Friday 03 May", null). We try a few obvious shapes; unparseable
     deadlines never count as overdue. */
  function deadlineToISO(deadline, dateOfReport) {
    if (!deadline) return null;
    var d = String(deadline).trim();
    /* Today / Tomorrow keywords resolve relative to the report's date. */
    if (/^today\b/i.test(d) && dateOfReport)    return dateOfReport;
    if (/^tomorrow\b/i.test(d) && dateOfReport) return window.FS.api.addDaysISO(dateOfReport, 1);
    /* "DD MMM" or "DD MMM YYYY" */
    var m = d.match(/(\d{1,2})\s+(\w+)\s*(\d{4})?/);
    if (m) {
      var months = { Jan:1,Feb:2,Mar:3,Apr:4,May:5,Jun:6,Jul:7,Aug:8,Sep:9,Oct:10,Nov:11,Dec:12 };
      var mon = months[m[2].slice(0, 3).replace(/^\w/, function (c) { return c.toUpperCase(); })];
      if (mon) {
        var y = m[3] ? parseInt(m[3], 10)
                     : (dateOfReport ? parseInt(dateOfReport.slice(0, 4), 10) : new Date().getUTCFullYear());
        var day = parseInt(m[1], 10);
        var pad = function (n) { return n < 10 ? '0' + n : '' + n; };
        return y + '-' + pad(mon) + '-' + pad(day);
      }
    }
    return null;
  }

  function isOverdue(row, today) {
    if (!row || row.audit.checked) return false;
    var iso = deadlineToISO(row.deadline, row.date);
    if (!iso) return false;
    return iso < today;
  }

  /* ---------- TasksContext --------------------------------------------- */

  var TasksContext = React.createContext(null);

  function TasksProvider(props) {
    var caller = (window.AuthMock && window.AuthMock.currentUser) || {};

    var refParams = React.useState(function () { return readRouteParams(); });
    var routeParams = refParams[0];
    var setRouteParams = refParams[1];

    React.useEffect(function () {
      return window.FS.Router.subscribe(function () {
        setRouteParams(readRouteParams());
      });
    }, []);

    var targetUser = routeParams.user || null;
    var depKey = (caller.name || '') + '|' + (caller.role || '') + '|' + (caller.isAdmin ? 'admin' : '')
      + '|' + (targetUser || '');

    var refState = React.useState({ status: 'loading' });
    var state    = refState[0];
    var setState = refState[1];

    var retryRef   = React.useState(0);
    var retryCount = retryRef[0];
    var setRetry   = retryRef[1];

    React.useEffect(function () {
      var cancelled = false;
      setState({ status: 'loading' });

      var today = window.FS.api.todayNZDT();
      var from  = window.FS.api.addDaysISO(today, -(DEFAULT_DAYS - 1));

      var fetchOpts = { from: from, to: today };
      if (targetUser) fetchOpts.user = targetUser;

      window.FS.api.tasks.getActionsResolvedRange(fetchOpts).then(function (res) {
        if (cancelled) return;
        if (res && res._accessDenied) {
          setState({ status: 'access_denied', message: res.error });
          return;
        }
        var rows = (res && res.rows) || [];
        setState({
          status:     'ok',
          rows:       rows,
          from:       from,
          to:         today,
          today:      today,
          user:       res.user || null,
          filterUser: targetUser || null,
        });
      }).catch(function (err) {
        if (cancelled) return;
        setState({ status: 'error', error: { code: (err && err.status) || 0, message: (err && err.message) || 'Could not load tasks', retryable: true }, retry: function () { setRetry(function (n) { return n + 1; }); } });
      });

      return function () { cancelled = true; };
    }, [depKey, retryCount]);

    function removeRow(rowId) {
      setState(function (s) {
        if (s.status !== 'ok') return s;
        var next = (s.rows || []).filter(function (r) { return r.id !== rowId; });
        return Object.assign({}, s, { rows: next });
      });
    }

    var ctx = { state: state, removeRow: removeRow };
    return React.createElement(TasksContext.Provider, { value: ctx },
      props.children);
  }

  /* ---------- TasksMiddleColumn ---------------------------------------- */

  function TasksMiddleColumn(props) {
    var fs                = window.FieldSight;
    var TasksFilterChips  = fs.TasksFilterChips;
    var TaskCard          = fs.TaskCard;
    var onSelect          = props.onSelect || function () {};

    var caller = (window.AuthMock && window.AuthMock.currentUser) || {};
    var myName = caller.name || '';

    /* Default filter: workers and named users see "Mine" first;
       admin/gm sees "All" so they get the full picture. */
    var defaultFilter = (caller.role === 'admin' || caller.role === 'gm' || caller.isAdmin)
      ? 'all' : 'mine';

    var refFilter = React.useState(defaultFilter);
    var filter    = refFilter[0];
    var setFilter = refFilter[1];

    /* 8.8.1 — visible count; reset to PAGE_SIZE when filter changes */
    var refVisible  = React.useState(PAGE_SIZE);
    var visibleCount = refVisible[0];
    var setVisible   = refVisible[1];

    React.useEffect(function () { setVisible(PAGE_SIZE); }, [filter]);

    var ctx = React.useContext(TasksContext);
    if (!ctx) {
      console.warn('[TasksMiddleColumn] TasksContext missing');
      return null;
    }
    var state = ctx.state;

    if (state.status === 'loading') {
      /* Sprint 8.7.3 — skeleton rows while aggregating */
      var skeletonWidths = ['75%', '55%', '90%', '65%', '80%'];
      return React.createElement('div', { className: 'fs-tasks' },
        React.createElement('div', { className: 'fs-tasks__skeleton' },
          skeletonWidths.map(function (w, i) {
            return React.createElement('div', {
              key: i, className: 'fs-skeleton-row',
            },
              React.createElement('span', {
                className: 'fs-skeleton fs-skeleton-row__check',
              }),
              React.createElement('div', { className: 'fs-skeleton-row__body' },
                React.createElement('span', {
                  className: 'fs-skeleton fs-skeleton-row__title',
                  style:     { width: w },
                }),
                React.createElement('span', {
                  className: 'fs-skeleton fs-skeleton-row__sub',
                }),
              ),
              React.createElement('span', {
                className: 'fs-skeleton fs-skeleton-row__badge',
              }),
            );
          }),
        ),
      );
    }

    if (state.status === 'error') {
      var ErrorBanner = window.FieldSight.ErrorBanner;
      return React.createElement('div', { className: 'fs-tasks' },
        ErrorBanner
          ? React.createElement(ErrorBanner, {
              message:   (state.error && state.error.message) || 'Could not load tasks',
              retryable: true,
              onRetry:   state.retry,
            })
          : React.createElement('div', { className: 'fs-tasks__empty' },
              (state.error && state.error.message) || 'Could not load tasks'),
      );
    }

    if (state.status === 'access_denied') {
      var AccessDenied = fs.AccessDenied;
      return React.createElement('div', { className: 'fs-tasks' },
        AccessDenied
          ? React.createElement(AccessDenied, {
              scope:   'these tasks',
              message: state.message,
            })
          : React.createElement('div', null, 'Access denied.'),
      );
    }

    var rows  = state.rows  || [];
    var today = state.today || window.FS.api.todayNZDT();

    /* Pre-compute filter buckets — used both for chip counts and the
       active-filter render below. */
    var buckets = {
      all:     rows,
      mine:    rows.filter(function (r) { return r.responsible === myName; }),
      open:    rows.filter(function (r) { return !r.audit.checked; }),
      overdue: rows.filter(function (r) { return isOverdue(r, today); }),
      done:    rows.filter(function (r) { return  r.audit.checked; }),
    };
    var counts = {
      all:     buckets.all.length,
      mine:    buckets.mine.length,
      open:    buckets.open.length,
      overdue: buckets.overdue.length,
      done:    buckets.done.length,
    };

    var visible = buckets[filter] || buckets.all;

    /* Sort: open first by (overdue desc, deadline asc, date desc),
       then done at the bottom. */
    visible = visible.slice().sort(function (a, b) {
      if (a.audit.checked !== b.audit.checked) return a.audit.checked ? 1 : -1;
      if (a.date !== b.date) return a.date < b.date ? 1 : -1;
      return (a.action || '').localeCompare(b.action || '');
    });

    /* Sprint 8.8.1 — client-side pagination */
    var totalVisible = visible.length;
    var hasMore      = visibleCount < totalVisible;
    visible          = visible.slice(0, visibleCount);

    var selectedId = props.selectedItem && props.selectedItem.kind === 'task_row'
      ? props.selectedItem.id
      : null;

    return React.createElement('div', { className: 'fs-tasks' },

      /* Header */
      React.createElement('div', { className: 'fs-tasks__header' },
        React.createElement('h2', { className: 'fs-tasks__title' }, 'Tasks'),
        React.createElement('div', { className: 'fs-tasks__subtitle' },
          'Action items assigned across reports — yours, your team’s, by status'),
        React.createElement('div', { className: 'fs-tasks__meta' },
          rows.length + ' actions · last ' + DEFAULT_DAYS + ' days'
            + ' (' + fmtDate(state.from) + ' → ' + fmtDate(state.to) + ')',
          React.createElement('span', {
            className: 'fs-tasks__meta-info',
            title:     'Aggregated client-side from /api/timeline + /api/actions per day. A backend /api/actions/all aggregator would speed this up at scale.',
          }, 'ⓘ'),
        ),
      ),

      /* Filter chips */
      React.createElement(TasksFilterChips, {
        value:    filter,
        counts:   counts,
        onChange: function (next) { setFilter(next); },
      }),

      /* List */
      visible.length === 0
        ? React.createElement('div', { className: 'fs-tasks__empty' },
            'No ' + (filter === 'all' ? '' : filter + ' ') + 'tasks in this window.')
        : React.createElement('div', { className: 'fs-tasks__list' },
            visible.map(function (row) {
              var task = {
                id:          row.id,
                topic_id:    row.topic_id,
                actionIndex: row.action_index,
                title:       row.action,
                assignee:    row.responsible || '—',
                status:      row.audit.checked ? 'Done' : (isOverdue(row, today) ? 'Overdue' : 'Open'),
                statusTone:  row.audit.checked ? 'success'
                            : (isOverdue(row, today) ? 'danger' : 'info'),
                priority:    row.priority
                              ? row.priority.charAt(0).toUpperCase() + row.priority.slice(1)
                              : 'Medium',
                dueTime:     parseHHMM(row.deadline) || (row.deadline ? row.deadline.slice(0, 14) : '—'),
              };
              return React.createElement(TaskCard, {
                key:      row.id,
                task:     task,
                isMine:   row.responsible === myName,
                selected: selectedId === row.id,
                onSelect: function () {
                  onSelect({
                    kind:        'task_row',
                    id:          row.id,
                    row:         row,
                  });
                },
              });
            }),
          ),

      /* Sprint 8.8.1 — load more */
      hasMore
        ? React.createElement('div', { className: 'fs-tasks__load-more' },
            React.createElement('button', {
              type:      'button',
              className: 'fs-tasks__load-more-btn',
              onClick:   function () { setVisible(function (n) { return n + PAGE_SIZE; }); },
            }, 'Load more (' + (totalVisible - visibleCount) + ' remaining)'),
          )
        : null,
    );
  }

  /* ---------- TasksRightDetail ---------------------------------------- */

  function TasksRightDetail(props) {
    var fs        = window.FieldSight;
    var Card      = fs.Card;
    var Badge     = fs.Badge;
    var Button    = fs.Button;
    var IconBtn   = fs.IconButton;

    var ctx = React.useContext(TasksContext);
    var sel = props.selectedItem;

    var refBusy = React.useState(false);
    var busy    = refBusy[0];
    var setBusy = refBusy[1];

    if (!sel || sel.kind !== 'task_row') {
      return React.createElement('div', { className: 'fs-tasks-detail__placeholder' },
        React.createElement('div', { className: 'fs-tasks-detail__placeholder-title' },
          'Select an action'),
        React.createElement('div', { className: 'fs-tasks-detail__placeholder-body' },
          'Pick any row in the list to see its full detail and audit history.'),
      );
    }

    var row = sel.row;
    var today = ctx && ctx.state && ctx.state.today;
    var overdue = isOverdue(row, today);

    function onMarkComplete() {
      if (busy || row.audit.checked) return;
      setBusy(true);
      window.FS.api.actions.toggleAction({
        date:         row.date,
        topic_id:     row.topic_id,
        action_index: row.action_index,
        checked:      true,
        action_text:  row.action,
      }).then(function () {
        if (ctx && ctx.removeRow) ctx.removeRow(row.id);
        if (props.onClose) props.onClose();
      }).catch(function (err) {
        console.error('[Tasks right] markComplete failed', err);
        setBusy(false);
      });
    }

    function onOpenInTimeline() {
      var qs = '?date=' + encodeURIComponent(row.date);
      if (row.user_folder) qs += '&user=' + encodeURIComponent(row.user_folder);
      window.FS.Router.navigate('/timeline' + qs);
    }

    var statusBadge;
    if (row.audit.checked) {
      statusBadge = React.createElement(Badge, { tone: 'success', size: 'sm', prefixDot: true }, 'Done');
    } else if (overdue) {
      statusBadge = React.createElement(Badge, { tone: 'danger', size: 'sm', prefixDot: true }, 'Overdue');
    } else {
      statusBadge = React.createElement(Badge, { tone: 'info', size: 'sm', prefixDot: true }, 'Open');
    }

    var priorityBadge = row.priority
      ? React.createElement(Badge, {
          tone: row.priority === 'high' ? 'danger'
              : row.priority === 'low'  ? 'neutral' : 'warning',
          size: 'sm', variant: 'outline',
        }, row.priority.charAt(0).toUpperCase() + row.priority.slice(1))
      : null;

    return React.createElement('div', { className: 'fs-tasks-detail' },

      /* Header */
      React.createElement('div', { className: 'fs-tasks-detail__header' },
        React.createElement('div', { className: 'fs-tasks-detail__header-main' },
          React.createElement('h2', { className: 'fs-tasks-detail__title' },
            row.action),
          React.createElement('div', { className: 'fs-tasks-detail__metaline' },
            statusBadge, priorityBadge,
          ),
        ),
        IconBtn ? React.createElement(IconBtn, {
          icon: 'x', ariaLabel: 'Close detail', size: 'sm',
          onClick: function () { if (props.onClose) props.onClose(); },
        }) : null,
      ),

      /* Field rows */
      React.createElement('div', { className: 'fs-tasks-detail__rows' },
        React.createElement(DetailRow, {
          label: 'Owner',     value: row.responsible || '—',
        }),
        React.createElement(DetailRow, {
          label: 'Deadline',  value: row.deadline || '—',
        }),
        React.createElement(DetailRow, {
          label: 'Date',      value: fmtDate(row.date),
        }),
        React.createElement(DetailRow, {
          label: 'From topic',value: row.topic_title,
        }),
        React.createElement(DetailRow, {
          label: 'Category',  value: row.topic_category,
        }),
        React.createElement(DetailRow, {
          label: 'Reporter',  value: row.user_name,
        }),
      ),

      /* Audit history (only when checked) */
      row.audit.checked
        ? React.createElement('div', { className: 'fs-tasks-detail__audit' },
            React.createElement('div', { className: 'fs-tasks-detail__audit-label' },
              'Audit'),
            React.createElement('div', { className: 'fs-tasks-detail__audit-body' },
              'Checked by ' + (row.audit.checked_by || '—')
                + ' · ' + (fmtTimestamp(row.audit.checked_at) || '—')),
          )
        : null,

      /* Actions */
      React.createElement('div', { className: 'fs-tasks-detail__actions' },
        !row.audit.checked
          ? React.createElement(Button, {
              size: 'sm', leftIcon: 'check',
              onClick: onMarkComplete, disabled: busy,
            }, busy ? 'Saving…' : 'Mark complete')
          : null,
        React.createElement(Button, {
          variant: 'secondary', size: 'sm', rightIcon: 'arrow-right',
          onClick: onOpenInTimeline,
        }, 'Open in timeline'),
      ),
    );
  }

  function DetailRow(props) {
    return React.createElement('div', { className: 'fs-tasks-detail__row' },
      React.createElement('div', { className: 'fs-tasks-detail__row-label' },
        props.label),
      React.createElement('div', { className: 'fs-tasks-detail__row-value' },
        props.value),
    );
  }

  /* ---------- Register --------------------------------------------------- */

  if (!window.FieldSight) window.FieldSight = {};
  if (!window.FieldSight.PAGES) window.FieldSight.PAGES = {};
  window.FieldSight.PAGES['/tasks'] = {
    Middle:   TasksMiddleColumn,
    Right:    TasksRightDetail,
    Provider: TasksProvider,
  };

})();
