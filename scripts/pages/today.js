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

  /* Today's date in NZDT — see BUG-19. The fixture's "today" is
     2026-04-29; for prototype purposes we anchor there. Real deploy
     will use addDaysISO(...) against an NZDT clock. */
  var FIXTURE_DATE = '2026-04-29';

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

  function buildTodayFromReport(report, actions, caller) {
    var sitesFx = (window.FieldSight && window.FieldSight.fixtures && window.FieldSight.fixtures.sites) || { users: [] };
    var match = (sitesFx.users || []).filter(function (u) { return u.name === (caller && caller.name); })[0];
    var primarySite = match ? match.primary_site : 'sb1108-ellesmere';

    return window.FS.api.todayAdapter.adapt(report, {
      currentUserName: caller && caller.name,
      primarySite:     primarySite,
      actionState:     actions || {},
      date:            FIXTURE_DATE,
    });
  }

  /* ---------- TodayState hook ------------------------------------------ */
  /* Encapsulates the async fetch + optimistic-removal semantics for
     check-off. Returns { status, data, removeMyTask, error }. */
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

      Promise.all([
        window.FS.api.timeline.getTimeline({ date: FIXTURE_DATE, user: window.FS.api.folderName(caller.name) }),
        window.FS.api.actions.getActions(FIXTURE_DATE),
      ]).then(function (results) {
        if (cancelled) return;
        var report  = results[0];
        var actions = results[1].actions || {};

        if (!report || report._notFound || report.available_users) {
          setState({ status: 'empty', report: report });
          return;
        }
        var data = buildTodayFromReport(report, actions, caller);
        setState({ status: 'ok', data: data, actions: actions });
      }).catch(function (err) {
        if (cancelled) return;
        setState({ status: 'error', error: err });
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
     Today Middle Column
     ===================================================================== */
  function TodayMiddleColumn(props) {
    var fs       = window.FieldSight;
    var caller   = (window.AuthMock && window.AuthMock.currentUser) || {};
    var onSelect = props.onSelect || function () {};

    var ts        = useTodayState(caller);
    var state     = ts.state;
    var removeMy  = ts.removeMyTask;

    /* Expose the loaded data to TodayRightDetail via a window slot —
       the simplest way to share state across the AppShell-managed
       split between Middle and Right components without restructuring
       the shell. Mounted Middle is the source of truth. */
    React.useEffect(function () {
      window.FieldSight._todayCache = state.status === 'ok' ? state.data : null;
    }, [state]);

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

    var data = state.data;

    /* When the check-off anim finishes, drop the task locally. The
       optimistic toggle inside TaskCard already persisted via
       FS.api.actions.toggleAction. */
    function onCheckedOff(task) {
      removeMy(task.id);
    }

    return React.createElement('div', {
      style: { display: 'flex', flexDirection: 'column', gap: 0 },
      className: 'fs-page fs-page--today',
    },

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

      /* TASKS — split into My + Team */
      React.createElement(SectionLabel, null, 'Tasks today'),

      data.myTasks && data.myTasks.length > 0 ? React.createElement(React.Fragment, null,
        React.createElement(SubsectionLabel, null,
          'My tasks · ' + data.myTasks.length),
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
              date:          FIXTURE_DATE,
              onCheckedOff:  onCheckedOff,
            });
          })
        ),
      ) : null,

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

    /* The Middle column publishes the latest TODAY snapshot to a window
       slot when it loads. We read it here for findItemById/getRelated.
       If the user clicked a row that's since been check-off-removed,
       the lookup falls back to the selectedItem itself. */
    var data = window.FieldSight._todayCache;
    var item = findItemById(data, sel.id) || sel;

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
          related.map(function (r, i) {
            return React.createElement(Card, {
              key: i, padding: 'sm', variant: 'ghost',
              onClick: function () { console.log('[Right] navigate to related:', r.id); },
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

      React.createElement('div', {
        style: {
          marginTop: 'auto', display: 'flex', gap: '8px',
          justifyContent: 'flex-end',
          paddingTop: '16px', borderTop: '1px solid var(--border-subtle)',
        },
      },
        React.createElement(Button, {
          variant: 'secondary', size: 'sm',
          onClick: function () { console.log('[Today] secondary action on', item.id); },
        }, 'Reassign'),
        React.createElement(Button, {
          size: 'sm', leftIcon: 'check',
          onClick: function () { console.log('[Today] primary action on', item.id); },
        }, item.kind === 'task' ? 'Mark complete' : 'Acknowledge'),
      ),

    );
  }

  /* ---------- Register --------------------------------------------------- */
  if (!window.FieldSight) window.FieldSight = {};
  if (!window.FieldSight.PAGES) window.FieldSight.PAGES = {};
  window.FieldSight.PAGES['/today'] = {
    Middle: TodayMiddleColumn,
    Right:  TodayRightDetail,
  };

})();
