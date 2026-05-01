/* ==========================================================================
   FieldSight Today Page — Sprint 2.4 (PLAN.md Phase D)
   --------------------------------------------------------------------------
   Today is now a DERIVED view over the latest DailyReport for the
   current user — no more bespoke mock-data shim. The same composites
   (TaskCard / UrgentCard / ActivityCard / MorningBriefCard / OnSiteCard)
   render unchanged; only the data source moved.

   Pipeline:
     FS.api.timeline.getTimeline (DailyReport)  ─┐
     FS.api.actions.getActions  (audit state)   ├─► todayAdapter.adapt
     fixtures.sites (for primary_site lookup)   ─┘            │
                                                              ▼
                                          { morningBrief, urgent, my/team
                                            tasks, activity, onSite }

   Sprint 2 task-check-off lands on REAL action items here:
     • TaskCard for an item Jarley owns gets a checkbox
     • Click → optimistic toggle through FS.api.actions.toggleAction
     • Animation: border pulse + line-through + fade-out (CSS, respects
       prefers-reduced-motion via tokens.css media query)
     • On animation end the row drops out of myTasks locally; full
       refresh on next mount picks up the persisted state

   Two exports: a Middle column component and a Right detail component.
   Both registered into window.FieldSight.PAGES under the '/today' key.
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  /* Today's date in NZDT — see BUG-19. We compute "today" via
     FS.api.todayNZDT() (Pacific/Auckland clock). If no report exists
     for that date, fall back to the latest available date from
     /api/dates so the prototype keeps rendering meaningfully when run
     on any calendar day. (P-06.) */

  /* ---------- SectionLabel (small uppercase heading) --------------------- */
  function SectionLabel(props) {
    var color = props.color || 'var(--text-tertiary)';
    return React.createElement('div', {
      style: {
        fontSize: '10px',
        fontWeight: 600,
        letterSpacing: '0.08em',
        textTransform: 'uppercase',
        color: color,
        margin: '20px 0 8px',
        padding: '0 4px',
      },
    }, props.children);
  }

  function SubsectionLabel(props) {
    return React.createElement('div', {
      style: {
        fontSize: '11px',
        fontWeight: 600,
        color: 'var(--text-secondary)',
        margin: '8px 0 4px',
        padding: '0 4px',
        letterSpacing: '0.02em',
      },
    }, props.children);
  }

  /* ---------- Helper: derive Today from a backend report --------------- */

  function buildTodayFromReport(report, actions, caller, date) {
    var sitesFx = (window.FieldSight && window.FieldSight.fixtures && window.FieldSight.fixtures.sites) || { users: [] };
    var match = (sitesFx.users || []).filter(function (u) { return u.name === (caller && caller.name); })[0];
    var primarySite = match ? match.primary_site : 'sb1108-ellesmere';

    return window.FS.api.todayAdapter.adapt(report, {
      currentUserName: caller && caller.name,
      primarySite:     primarySite,
      actionState:     actions || {},
      date:            date,
    });
  }

  /* Pick the most recent date with a report from /api/dates, or null. */
  function findLatestReportDate(datesMap) {
    var keys = Object.keys(datesMap || {}).filter(function (d) {
      return datesMap[d] && datesMap[d].hasReport;
    });
    if (keys.length === 0) return null;
    keys.sort(); /* YYYY-MM-DD sorts lexically */
    return keys[keys.length - 1];
  }

  /* ---------- TodayContext (Sprint 3, P-07) ---------------------------- */
  /* TodayMiddleColumn loads the report; TodayRightDetail needs the same
     snapshot to render `findItemById` lookups for the selected item.
     Phase D used a `window.FieldSight._todayCache` slot for this — fast
     to ship but invisible to React DevTools and broken under multiple
     instances. P-07 replaces it with a proper Context provided at the
     page level via the new `Provider` slot in the page registry; the
     AppShell wraps both Middle + Right in that Provider so they share
     state. */
  var TodayContext = React.createContext(null);

  /* ---------- TodayState hook ------------------------------------------ */
  /* Encapsulates the async fetch + optimistic-removal semantics for
     check-off. Returns { state, removeMyTask }. */
  function useTodayState(caller) {
    var refState = React.useState({ status: 'loading' });
    var state    = refState[0];
    var setState = refState[1];

    /* Re-key the effect on the caller name+role: dev role switcher
       changes role → teamTasks visibility flips; reload to recompute. */
    var depKey = (caller && caller.name) + '|' + (caller && caller.role) + '|' + (caller && caller.isAdmin);

    React.useEffect(function () {
      var cancelled = false;
      setState({ status: 'loading' });

      var today    = window.FS.api.todayNZDT();
      var folder   = window.FS.api.folderName(caller.name);

      /* Sprint 4.10.3 — Programme tasks for today run in parallel
         with the daily-report load. They live under data.programmeTasks
         so the My Tasks renderer can split them into a sub-group and
         render the ProgrammeTaskCard variant. Promise resolves with
         { rows: [...] } regardless of role / fixture state, so we
         can pass it straight through Promise.all without bailout
         logic. */
      var programmePromise = (window.FS.api.todayProgramme
        ? window.FS.api.todayProgramme.getTodayProgrammeTasks({ today: today, user: folder })
        : Promise.resolve({ rows: [] })
      ).then(function (r) { return (r && r.rows) || []; })
       .catch(function () { return []; });

      function loadFor(date, isFallback) {
        return Promise.all([
          window.FS.api.timeline.getTimeline({ date: date, user: folder }),
          window.FS.api.actions.getActions(date),
        ]).then(function (results) {
          if (cancelled) return null;
          var report  = results[0];
          var actions = results[1].actions || {};
          /* P-12: 403 from the timeline endpoint surfaces as a
             page-level access-denied state. Worker / site-manager
             querying another user's report hits this. */
          if (report && report._accessDenied) {
            return { accessDenied: true, message: report.error };
          }
          if (!report || report._notFound || report.available_users) {
            return { ok: false, report: report };
          }
          var data = buildTodayFromReport(report, actions, caller, date);
          return {
            ok:            true,
            data:          data,
            actions:       actions,
            effectiveDate: date,
            isFallback:    !!isFallback,
            today:         today,
          };
        });
      }

      /* Helper — merges the programme rows into the data envelope so
         every setState site below can compose the same shape without
         repeating the await. */
      function withProgramme(envelope, programmeRows) {
        if (!envelope || !envelope.data) return envelope;
        return Object.assign({}, envelope, {
          data: Object.assign({}, envelope.data, {
            programmeTasks: programmeRows || [],
          }),
        });
      }

      Promise.all([loadFor(today, false), programmePromise])
        .then(function (results) {
          if (cancelled) return;
          var first          = results[0];
          var programmeRows  = results[1];

          if (!first) return;
          if (first.accessDenied) {
            setState({ status: 'access_denied', message: first.message, today: today });
            return;
          }
          if (first.ok) {
            setState(Object.assign({ status: 'ok' }, withProgramme(first, programmeRows)));
            return;
          }
          /* No report for today — try the latest available. */
          return window.FS.api.dates.getDates({ months: 3 }).then(function (res) {
            if (cancelled) return;
            if (res && res._accessDenied) {
              setState({ status: 'access_denied', message: res.error, today: today });
              return;
            }
            var latest = findLatestReportDate(res.dates || {});
            if (!latest || latest === today) {
              /* Empty-state still shows today's programme tasks if any —
                 the programme is not gated on a daily report existing. */
              setState({
                status:         'empty',
                report:         first.report,
                today:          today,
                programmeTasks: programmeRows,
              });
              return;
            }
            return loadFor(latest, true).then(function (second) {
              if (cancelled || !second) return;
              if (second.accessDenied) {
                setState({ status: 'access_denied', message: second.message, today: today });
                return;
              }
              if (second.ok) {
                setState(Object.assign({ status: 'ok' }, withProgramme(second, programmeRows)));
              } else {
                setState({
                  status:         'empty',
                  report:         second.report,
                  today:          today,
                  programmeTasks: programmeRows,
                });
              }
            });
          });
        }).catch(function (err) {
          if (cancelled) return;
          setState({ status: 'error', error: err, today: today });
        });

      return function () { cancelled = true; };
    }, [depKey]);

    /* Local optimistic removal — used after the check-off animation
       finishes to drop a task out of the rendered list without a
       network round-trip. The next mount re-fetches the persisted
       audit state. */
    function removeMyTask(taskId) {
      setState(function (s) {
        if (s.status !== 'ok' || !s.data) return s;
        var nextMy = (s.data.myTasks || []).filter(function (t) { return t.id !== taskId; });
        return Object.assign({}, s, {
          data: Object.assign({}, s.data, { myTasks: nextMy }),
        });
      });
    }

    return { state: state, removeMyTask: removeMyTask };
  }

  /* ---------- In-page lookups (replace old MockData helpers) ----------- */

  function findItemById(data, id) {
    if (!id || !data) return null;
    var pools = [
      data.urgent || [], data.myTasks || [],
      data.teamTasks || [], data.activity || [],
    ];
    for (var i = 0; i < pools.length; i++) {
      for (var j = 0; j < pools[i].length; j++) {
        if (pools[i][j].id === id) return pools[i][j];
      }
    }
    return null;
  }

  function getRelated(data, item) {
    if (!item || !data) return [];

    if (item.kind === 'task') {
      var allTasks = (data.myTasks || []).concat(data.teamTasks || []);
      return allTasks
        .filter(function (t) { return t.id !== item.id && t.assignee === item.assignee; })
        .slice(0, 3)
        .map(function (t) {
          return { id: t.id, title: t.title,
                   subtitle: t.status + ' · due ' + t.dueTime };
        });
    }

    if (item.kind === 'activity') {
      return (data.activity || [])
        .filter(function (a) { return a.id !== item.id && a.speaker === item.speaker; })
        .slice(0, 3)
        .map(function (a) {
          return { id: a.id, title: a.snippet,
                   subtitle: a.timeAgo + ' · ' + a.channel };
        });
    }

    if (item.kind === 'urgent') {
      return (data.urgent || [])
        .filter(function (u) { return u.id !== item.id; })
        .slice(0, 3)
        .map(function (u) {
          return { id: u.id, title: u.title, subtitle: u.badgeLabel };
        });
    }

    return [];
  }

  function getTimeline(item) {
    if (!item) return [];

    if (item.kind === 'task') {
      return [
        { label: 'Captured in topic',          actor: 'AI · transcript',  time: 'Today' },
        { label: 'Assigned to ' + item.assignee, actor: 'Report generator', time: 'Today' },
        { label: 'Status: ' + item.status,     actor: item.assignee,      time: 'Today' },
      ];
    }
    if (item.kind === 'urgent') {
      return [
        { label: 'Flagged urgent',                                   actor: 'System', time: 'Today' },
        { label: 'Triggered by · ' + (item.triggeredBy || 'manual'), actor: 'System', time: 'Today' },
      ];
    }
    if (item.kind === 'activity') {
      return [
        { label: 'Captured',                            actor: item.speaker,      time: item.timeAgo },
        { label: 'Transcribed',                         actor: 'AWS Transcribe',  time: 'just after capture' },
        { label: 'Tagged · ' + (item.channel || 'General'), actor: 'AI',          time: 'just after capture' },
      ];
    }
    return [];
  }

  /* =====================================================================
     TodayProvider — owns the page state and exposes it via TodayContext.
     AppShell wraps Middle + Right in this so both columns see the same
     snapshot. (P-07)
     ===================================================================== */
  function TodayProvider(props) {
    var caller = (window.AuthMock && window.AuthMock.currentUser) || {};
    var ts     = useTodayState(caller);
    /* Stable-ish value object — not memoised because the TodayState
       hook already re-keys its effect on caller identity, and the
       consumers below read .state every render anyway. */
    var ctx = { state: ts.state, removeMyTask: ts.removeMyTask };
    return React.createElement(TodayContext.Provider, { value: ctx },
      props.children);
  }

  /* =====================================================================
     Today Middle Column
     ===================================================================== */
  function TodayMiddleColumn(props) {
    var fs       = window.FieldSight;
    var onSelect = props.onSelect || function () {};

    var ctx = React.useContext(TodayContext);
    if (!ctx) {
      console.warn('[TodayMiddleColumn] TodayContext missing — was the page Provider mounted?');
      return null;
    }
    var state    = ctx.state;
    var removeMy = ctx.removeMyTask;

    if (state.status === 'loading') {
      return React.createElement('div', { className: 'fs-page fs-page--today' },
        React.createElement('div', { style: { padding: '24px', color: 'var(--text-tertiary)', fontSize: '13px' } },
          'Loading today…'),
      );
    }

    if (state.status === 'error') {
      return React.createElement('div', { className: 'fs-page fs-page--today' },
        React.createElement('div', { style: { padding: '24px', color: 'var(--text-tertiary)', fontSize: '13px' } },
          'Could not load today. ' + (state.error && state.error.message || '')),
      );
    }

    /* P-12 — empathetic 403. The api/_fetch helper marks 403 responses
       with `_accessDenied: true`; today.js relays that to the
       AccessDenied composite (BACKEND-CONTEXT §8.4). */
    if (state.status === 'access_denied') {
      var AccessDenied = fs.AccessDenied;
      return React.createElement('div', { className: 'fs-page fs-page--today' },
        AccessDenied
          ? React.createElement(AccessDenied, {
              scope:   "today's report",
              message: state.message,
            })
          : React.createElement('div', null, 'Access denied.'),
      );
    }

    if (state.status === 'empty') {
      var report = state.report || {};
      var msg = (report.available_users && 'Pick a user from /timeline to view a daily report.')
              || report.message
              || 'No report yet for today.';
      return React.createElement('div', { className: 'fs-page fs-page--today' },
        React.createElement('div', { style: { padding: '24px', color: 'var(--text-tertiary)', fontSize: '13px' } },
          msg),
      );
    }

    var data          = state.data;
    var effectiveDate = state.effectiveDate;
    var isFallback    = !!state.isFallback;

    /* When the check-off anim finishes, drop the task locally. The
       optimistic toggle inside TaskCard already persisted via
       FS.api.actions.toggleAction. */
    function onCheckedOff(task) {
      removeMy(task.id);
    }

    /* Format the effective date for the fallback banner. */
    function fmtDate(yyyymmdd) {
      var p = (yyyymmdd || '').split('-').map(Number);
      if (p.length !== 3) return yyyymmdd || '';
      var d = new Date(Date.UTC(p[0], p[1] - 1, p[2]));
      var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
      return d.getUTCDate() + ' ' + months[d.getUTCMonth()] + ' ' + p[0];
    }

    return React.createElement('div', {
      style: { display: 'flex', flexDirection: 'column', gap: 0 },
      className: 'fs-page fs-page--today',
    },

      /* Fallback banner — shown when today has no report yet and we're
         displaying the latest available one instead. */
      isFallback ? React.createElement('div', { className: 'fs-today__fallback-banner' },
        React.createElement('span', { className: 'fs-today__fallback-label' },
          'Latest available'),
        React.createElement('span', { className: 'fs-today__fallback-date' },
          fmtDate(effectiveDate)),
        React.createElement('span', { className: 'fs-today__fallback-note' },
          '· no report yet for today (' + fmtDate(state.today) + ')'),
      ) : null,

      /* "View daily report" CTA — full-width banner above the brief.
         Lifted out of MorningBriefCard so the action stands on its
         own (post-merge review feedback). Navigates to the canonical
         /timeline view scoped to the brief's (date, user). The
         &from=today flag tells TimelineMiddleColumn to render a
         "← Back to today" link in its header (Sprint 4.5). */
      React.createElement('button', {
        type:      'button',
        className: 'fs-today__view-report-cta',
        onClick:   function () {
          var qs = '?date=' + encodeURIComponent(effectiveDate);
          var u  = data.morningBrief && data.morningBrief.userFolder;
          if (u) qs += '&user=' + encodeURIComponent(u);
          qs += '&from=today';
          window.FS.Router.navigate('/timeline' + qs);
        },
      },
        React.createElement('span', { className: 'fs-today__view-report-cta-text' },
          'View daily report'),
        React.createElement('span', { className: 'fs-today__view-report-cta-arrow' },
          '→'),
      ),

      /* MORNING BRIEF */
      React.createElement(fs.MorningBriefCard, { brief: data.morningBrief }),

      /* URGENT */
      data.urgent && data.urgent.length > 0
        ? React.createElement(React.Fragment, null,
            React.createElement(SectionLabel, { color: 'var(--color-danger-700)' }, 'Urgent now'),
            React.createElement('div', {
              style: { display: 'flex', flexDirection: 'column', gap: '6px' },
            },
              data.urgent.map(function (item) {
                return React.createElement(fs.UrgentCard, {
                  key: item.id, item: item, onSelect: onSelect,
                });
              })
            ),
          )
        : null,

      /* TASKS — Sprint 4.10: My-tasks list now mixes two sources:
         (1) programme tasks scheduled on today (from FS.api.todayProgramme),
         (2) action items derived from yesterday's daily report.
         Same parent SectionLabel, two visually distinct sub-groups so the
         user reads them as ONE list with provenance, not two lists. */
      React.createElement(SectionLabel, null, 'Tasks today'),

      /* Sub-group 1 — Programme tasks (rendered FIRST since the
         programme work is the structural context for the day; action
         items are reactive details inside it). */
      data.programmeTasks && data.programmeTasks.length > 0
        ? React.createElement(React.Fragment, null,
            React.createElement(SubsectionLabel, null,
              'From your programme · ' + data.programmeTasks.length),
            React.createElement('div', {
              style: { display: 'flex', flexDirection: 'column', gap: '6px' },
            },
              data.programmeTasks.map(function (row) {
                return React.createElement(fs.ProgrammeTaskCard, {
                  key:      row.task_id,
                  row:      row,
                  onSelect: function () {
                    /* 4.10.6 — navigate to /programme with deep-link
                       so the right drawer opens on the same task. */
                    window.FS.Router.navigate(
                      '/programme?task=' + encodeURIComponent(row.task_id)
                        + '&from=today');
                  },
                });
              })
            ),
          )
        : null,

      /* Sub-group 2 — Action items from the daily report. */
      data.myTasks && data.myTasks.length > 0
        ? React.createElement(React.Fragment, null,
            React.createElement(SubsectionLabel, null,
              'From recent reports · ' + data.myTasks.length),
            React.createElement('div', {
              style: { display: 'flex', flexDirection: 'column', gap: '6px' },
            },
              data.myTasks.map(function (task) {
                return React.createElement(fs.TaskCard, {
                  key:           task.id,
                  task:          task,
                  onSelect:      onSelect,
                  isMine:        true,
                  checkable:     task.topic_id != null && task.actionIndex != null,
                  date:          effectiveDate,
                  onCheckedOff:  onCheckedOff,
                });
              })
            ),
          )
        : null,

      data.teamTasks && data.teamTasks.length > 0 ? React.createElement(React.Fragment, null,
        React.createElement(SubsectionLabel, null,
          'Team · ' + data.teamTasks.length),
        React.createElement('div', {
          style: { display: 'flex', flexDirection: 'column', gap: '6px' },
        },
          data.teamTasks.map(function (task) {
            return React.createElement(fs.TaskCard, {
              key: task.id, task: task, onSelect: onSelect, isMine: false,
            });
          })
        ),
      ) : null,

      /* (Sprint 3, P-02) Recent activity removed — the same topics are
         now reachable on /timeline as the canonical surface. Today
         stays a quick dashboard: brief → urgent → tasks → on-site. */

      /* ON SITE */
      React.createElement(SectionLabel, null, 'On site now'),
      React.createElement(fs.OnSiteCard, { people: data.onSite }),

    );
  }

  /* =====================================================================
     Today Right Detail
     ===================================================================== */
  function TodayRightDetail(props) {
    var fs       = window.FieldSight;
    var Card     = fs.Card;
    var Badge    = fs.Badge;
    var Button   = fs.Button;
    var IconBtn  = fs.IconButton;
    var Timeline = fs.Timeline;

    var sel = props.selectedItem;

    /* Empty state */
    if (!sel) {
      return React.createElement('div', {
        style: {
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          height: '100%',
          padding: '32px',
          gap: '12px',
          color: 'var(--text-tertiary)',
        },
      },
        React.createElement('div', {
          style: { fontSize: '14px', fontWeight: 500, color: 'var(--text-secondary)' },
        }, 'Select an item'),
        React.createElement('div', { style: { fontSize: '13px' } },
          'Choose from the list to view details'),
      );
    }

    /* P-07 — the Middle column owns the snapshot via TodayProvider;
       we read it through TodayContext. If a row was check-off-removed
       between click and render, fall back to the selectedItem itself. */
    var ctx  = React.useContext(TodayContext);
    var data = ctx && ctx.state.status === 'ok' ? ctx.state.data : null;
    var item = findItemById(data, sel.id) || sel;

    /* M-4 — wire "Mark complete" to the same persistence path TaskCard
       uses: toggle the action via /api/actions, then drop the task
       optimistically from the page snapshot and close the right detail.
       Surface only when the item is a task that carries the action key. */
    var effectiveDate = (ctx && ctx.state && ctx.state.effectiveDate) || null;
    var canCheckOff   = item.kind === 'task'
                     && item.topic_id   != null
                     && item.actionIndex != null
                     && !!effectiveDate;

    function onMarkComplete() {
      if (!canCheckOff) return;
      var api = window.FS && window.FS.api && window.FS.api.actions;
      if (!api) return;
      api.toggleAction({
        date:         effectiveDate,
        topic_id:     item.topic_id,
        action_index: item.actionIndex,
        checked:      true,
        action_text:  item.title,
      }).then(function () {
        if (ctx && ctx.removeMyTask) ctx.removeMyTask(item.id);
        if (props.onClose) props.onClose();
      }).catch(function (err) {
        console.error('[Today right] markComplete failed', err);
      });
    }

    var rows = [];
    if (item.kind === 'task') {
      rows = [
        ['Assignee', item.assignee],
        ['Due',      item.dueTime],
        ['Status',   item.status],
        ['Priority', item.priority || 'Medium'],
      ];
    } else if (item.kind === 'urgent') {
      rows = [
        ['Severity',     item.badgeLabel],
        ['Triggered by', item.triggeredBy || 'Manual flag'],
        ['Detail',       item.body],
      ];
    } else if (item.kind === 'activity') {
      rows = [
        ['Speaker',  item.speaker],
        ['When',     item.timeAgo],
        ['Source',   'PTT transcript'],
        ['Channel',  item.channel || 'General'],
      ];
    }

    var related  = getRelated(data, item);
    var timeline = getTimeline(item);

    return React.createElement('div', {
      style: {
        padding: '24px',
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        gap: '20px',
        overflowY: 'auto',
        boxSizing: 'border-box',
      },
    },

      React.createElement('div', {
        style: { display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '12px' },
      },
        React.createElement('h2', {
          style: {
            margin: 0, fontSize: '18px', fontWeight: 600,
            color: 'var(--text-primary)', lineHeight: 1.3,
            flex: 1, minWidth: 0,
            display: '-webkit-box', WebkitBoxOrient: 'vertical', WebkitLineClamp: 3,
            overflow: 'hidden', wordBreak: 'break-word',
          },
        }, item.title || item.snippet || '(item)'),
        React.createElement(IconBtn, {
          icon: 'x', ariaLabel: 'Close detail', size: 'sm',
          onClick: function () { if (props.onClose) props.onClose(); },
        }),
      ),

      item.kind === 'urgent' ? React.createElement('div', {
        style: { display: 'flex', gap: '6px' },
      },
        React.createElement(Badge, {
          tone: item.badgeTone, size: 'sm', prefixDot: true,
        }, item.badgeLabel),
      ) : null,

      item.kind === 'task' ? React.createElement('div', {
        style: { display: 'flex', gap: '6px', flexWrap: 'wrap' },
      },
        React.createElement(Badge, { tone: item.statusTone, size: 'sm' }, item.status),
        item.priority ? React.createElement(Badge, {
          tone: item.priority === 'High' ? 'danger' : item.priority === 'Low' ? 'neutral' : 'warning',
          size: 'sm', variant: 'outline',
        }, item.priority) : null,
      ) : null,

      React.createElement('div', {
        style: { display: 'flex', flexDirection: 'column', gap: 0 },
      },
        rows.map(function (r, i) {
          return React.createElement('div', {
            key: i,
            style: {
              display: 'flex', gap: '12px', padding: '10px 0',
              borderBottom: i < rows.length - 1 ? '1px solid var(--border-subtle)' : 'none',
            },
          },
            React.createElement('div', {
              style: {
                fontSize: '11px', color: 'var(--text-tertiary)', fontWeight: 600,
                width: '88px', flexShrink: 0,
                textTransform: 'uppercase', letterSpacing: '0.06em', paddingTop: '2px',
              },
            }, r[0]),
            React.createElement('div', {
              style: { fontSize: '14px', color: 'var(--text-primary)', flex: 1, lineHeight: 1.45 },
            }, r[1]),
          );
        })
      ),

      related.length > 0 ? React.createElement(React.Fragment, null,
        React.createElement('div', {
          style: {
            fontSize: '11px', fontWeight: 600,
            color: 'var(--text-tertiary)',
            textTransform: 'uppercase', letterSpacing: '0.06em',
            marginTop: '4px',
          },
        }, 'Related'),
        React.createElement('div', {
          style: { display: 'flex', flexDirection: 'column', gap: '6px' },
        },
          /* M-4 — Related rows are informational; clicking did nothing
             but log a stub. Right detail can't currently change the
             middle's selectedItem (that lives in AppShell), so surface
             these as static cards rather than fake-interactive ones. */
          related.map(function (r, i) {
            return React.createElement(Card, {
              key: i, padding: 'sm', variant: 'ghost',
            },
              React.createElement(Card.Body, null,
                React.createElement('div', {
                  style: { fontSize: '13px', color: 'var(--text-primary)', fontWeight: 500 },
                }, r.title),
                React.createElement('div', {
                  style: { fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' },
                }, r.subtitle),
              ),
            );
          })
        ),
      ) : null,

      timeline.length > 0 ? React.createElement(React.Fragment, null,
        React.createElement('div', {
          style: {
            fontSize: '11px', fontWeight: 600,
            color: 'var(--text-tertiary)',
            textTransform: 'uppercase', letterSpacing: '0.06em',
            marginTop: '4px',
          },
        }, 'Timeline'),
        React.createElement(Timeline, { events: timeline }),
      ) : null,

      /* M-4 — only render the action footer when there's an action to
         take. Today the only persistable action from the right detail
         is marking a task complete (Reassign + Acknowledge had no API
         to back them and were stubs). */
      canCheckOff ? React.createElement('div', {
        style: {
          marginTop: 'auto', display: 'flex', gap: '8px',
          justifyContent: 'flex-end',
          paddingTop: '16px', borderTop: '1px solid var(--border-subtle)',
        },
      },
        React.createElement(Button, {
          size: 'sm', leftIcon: 'check',
          onClick: onMarkComplete,
        }, 'Mark complete'),
      ) : null,

    );
  }

  /* ---------- Register --------------------------------------------------- */
  if (!window.FieldSight) window.FieldSight = {};
  if (!window.FieldSight.PAGES) window.FieldSight.PAGES = {};
  window.FieldSight.PAGES['/today'] = {
    Middle:   TodayMiddleColumn,
    Right:    TodayRightDetail,
    /* P-07 — page-level Provider; AppShell wraps Middle + Right in this
       so they share TodayContext. Pages without page-level state simply
       omit this and AppShell falls back to React.Fragment. */
    Provider: TodayProvider,
  };

})();
