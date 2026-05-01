/* ==========================================================================
   FieldSight Programme Page — Sprint 4.4 (Programme MVP)
   --------------------------------------------------------------------------
   /programme — read-only Gantt view + Jira-style TO-DO toggle +
   linked-action navigation. The single biggest sub-sprint of
   Sprint 4 — see the Sprint 4 plan in /root/.claude/plans/ for full
   scope and the deliberately-deferred Sprint 5 follow-ups (imports,
   native edit, cascade engine).

   Middle column:
     • Header: programme name + range + view toggle (Gantt / TO-DO)
     • Tier toggle (Day / Week / Month) when on Gantt
     • Gantt: left WBS tree (sticky) + scrollable timeline
     • TO-DO: ProgrammeTodoList (This Week / Next Week / Later)

   Right detail:
     • Selected task: WBS, name, status, dates, baseline, progress,
       assignees, resource pool, dependencies, tags
     • "Linked actions" section — clickable rows that navigate to
       /timeline?date=…&user=…

   Architecture:
     • ProgrammeProvider owns programme + selection state + view +
       tier + collapsed-groups set
     • Worker rule applied in scripts/api/programme.js
     • Critical-path rendering = task is in programme.critical_path[]

   Registers as window.FieldSight.PAGES['/programme']
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  var DEFAULT_PROGRAMME_ID = 'sb1108-2026-q2';

  var TIER_PIXELS = { day: 24, week: 6, month: 2 };

  /* ---------- Helpers --------------------------------------------------- */

  function fmtDate(yyyymmdd) {
    if (!yyyymmdd) return '';
    var p = String(yyyymmdd).split('-').map(Number);
    if (p.length !== 3) return yyyymmdd;
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return p[2] + ' ' + months[p[1] - 1] + ' ' + p[0];
  }

  function diffDays(a, b) {
    var ms = new Date(b + 'T00:00:00Z').getTime() - new Date(a + 'T00:00:00Z').getTime();
    return Math.round(ms / 86400000);
  }

  /* Roll up child date range + progress for a group row. Used to
     render summary bars in the Gantt without backend support. */
  function rollupGroup(parent, leaves) {
    var children = leaves.filter(function (t) { return t.parent_id === parent.task_id; });
    if (!children.length) return { start: null, end: null, progress: 0 };
    var start = children.reduce(function (m, t) { return !m || t.start < m ? t.start : m; }, null);
    var end   = children.reduce(function (m, t) { return !m || t.end   > m ? t.end   : m; }, null);
    var totalDays = children.reduce(function (s, t) { return s + (t.duration_days || 0); }, 0);
    var doneDays  = children.reduce(function (s, t) {
      return s + ((t.duration_days || 0) * (t.progress_pct || 0) / 100);
    }, 0);
    var progress = totalDays > 0 ? Math.round(doneDays / totalDays * 100) : 0;
    return { start: start, end: end, progress: progress };
  }

  /* ---------- ProgrammeContext ---------------------------------------- */

  var ProgrammeContext = React.createContext(null);

  function ProgrammeProvider(props) {
    var caller = (window.AuthMock && window.AuthMock.currentUser) || {};
    var depKey = (caller.name || '') + '|' + (caller.role || '') + '|' + (caller.isAdmin ? 'admin' : '');

    var refState  = React.useState({ status: 'loading' });
    var state     = refState[0];
    var setState  = refState[1];

    var refView   = React.useState('gantt');
    var view      = refView[0];
    var setView   = refView[1];

    var refTier   = React.useState('day');
    var tier      = refTier[0];
    var setTier   = refTier[1];

    /* Set of task_ids whose group is collapsed (children hidden). */
    var refCollapsed = React.useState(new Set());
    var collapsed    = refCollapsed[0];
    var setCollapsed = refCollapsed[1];

    React.useEffect(function () {
      var cancelled = false;
      setState({ status: 'loading' });

      window.FS.api.programme.getProgramme(DEFAULT_PROGRAMME_ID).then(function (p) {
        if (cancelled) return;
        if (p && p._accessDenied) {
          setState({ status: 'access_denied', message: p.error });
          return;
        }
        if (!p || p._notFound) {
          setState({ status: 'error', error: { message: 'Programme not found.' } });
          return;
        }
        var leaves   = (p.tasks || []).filter(function (t) { return t.status !== 'group'; });
        var parents  = (p.tasks || []).filter(function (t) { return t.status === 'group'; });
        var critical = new Set(p.critical_path || []);
        setState({
          status:   'ok',
          programme: p,
          leaves:   leaves,
          parents:  parents,
          critical: critical,
          today:    window.FS.api.todayNZDT(),
        });
      }).catch(function (err) {
        if (cancelled) return;
        setState({ status: 'error', error: err });
      });

      return function () { cancelled = true; };
    }, [depKey]);

    function toggleGroup(groupId) {
      setCollapsed(function (prev) {
        var next = new Set(prev);
        if (next.has(groupId)) next.delete(groupId);
        else next.add(groupId);
        return next;
      });
    }

    var ctx = {
      state:        state,
      view:         view,    setView:    setView,
      tier:         tier,    setTier:    setTier,
      collapsed:    collapsed,
      toggleGroup:  toggleGroup,
    };
    return React.createElement(ProgrammeContext.Provider, { value: ctx },
      props.children);
  }

  /* ---------- Gantt sub-view ------------------------------------------ */

  function GanttView(props) {
    var fs           = window.FieldSight;
    var GanttStrip   = fs.GanttStrip;
    var GanttRow     = fs.GanttRow;
    var TaskTreeCell = fs.TaskTreeCell;

    var ctx = React.useContext(ProgrammeContext);
    var s   = ctx.state;
    var prog = s.programme;

    /* Build the visible rows in WBS order: each parent followed by its
       leaves (unless collapsed). */
    var rows = [];
    s.parents.forEach(function (parent) {
      var roll = rollupGroup(parent, s.leaves);
      var groupTask = Object.assign({}, parent, {
        start: roll.start, end: roll.end, duration_days: 0,
        progress_pct: roll.progress, status: 'group',
      });
      rows.push({ kind: 'group', task: groupTask, parent: parent, indent: 0 });
      if (!ctx.collapsed.has(parent.task_id)) {
        s.leaves
          .filter(function (t) { return t.parent_id === parent.task_id; })
          .forEach(function (leaf) {
            rows.push({ kind: 'leaf', task: leaf, indent: 1 });
          });
      }
    });

    var ppd        = TIER_PIXELS[ctx.tier] || 24;
    var totalDays  = diffDays(prog.start_date, prog.end_date) + 1;
    var totalWidth = totalDays * ppd;

    /* Today marker offset within the timeline. */
    var todayOffset = null;
    if (s.today >= prog.start_date && s.today <= prog.end_date) {
      todayOffset = diffDays(prog.start_date, s.today) * ppd;
    }

    var selectedId = props.selectedItem && props.selectedItem.kind === 'programme_task'
      ? props.selectedItem.task_id
      : null;

    return React.createElement('div', { className: 'fs-gantt' },

      /* Sticky left tree */
      React.createElement('div', { className: 'fs-gantt__tree' },
        React.createElement('div', { className: 'fs-gantt__tree-head' }, 'WBS · Task'),
        rows.map(function (r) {
          return React.createElement(TaskTreeCell, {
            key:        r.task.task_id,
            task:       r.task,
            isGroup:    r.kind === 'group',
            expanded:   r.kind === 'group' && !ctx.collapsed.has(r.task.task_id),
            indent:     r.indent,
            critical:   r.kind === 'leaf' && s.critical.has(r.task.task_id),
            selected:   selectedId === r.task.task_id,
            onToggle:   function () { ctx.toggleGroup(r.task.task_id); },
            onSelect:   function () {
              if (r.kind === 'group') return;
              props.onSelect({
                kind:     'programme_task',
                id:       'task_' + r.task.task_id,
                task_id:  r.task.task_id,
                task:     r.task,
              });
            },
          });
        }),
      ),

      /* Right scrollable timeline */
      React.createElement('div', { className: 'fs-gantt__timeline' },
        React.createElement('div', {
          className: 'fs-gantt__timeline-inner',
          style:     { width: totalWidth + 'px' },
        },
          React.createElement(GanttStrip, {
            from: prog.start_date, to: prog.end_date,
            pixelsPerDay: ppd, tier: ctx.tier,
          }),

          rows.map(function (r) {
            return React.createElement(GanttRow, {
              key:           r.task.task_id,
              task:          r.task,
              programmeStart: prog.start_date,
              pixelsPerDay:  ppd,
              critical:      r.kind === 'leaf' && s.critical.has(r.task.task_id),
              selected:      selectedId === r.task.task_id,
              onSelect:      function () {
                if (r.kind === 'group') return;
                props.onSelect({
                  kind:     'programme_task',
                  id:       'task_' + r.task.task_id,
                  task_id:  r.task.task_id,
                  task:     r.task,
                });
              },
            });
          }),

          todayOffset != null
            ? React.createElement('div', {
                className: 'fs-gantt__today',
                style:     { left: todayOffset + 'px' },
                title:     'Today: ' + s.today,
              })
            : null,
        ),
      ),
    );
  }

  /* ---------- ProgrammeMiddleColumn ----------------------------------- */

  function ProgrammeMiddleColumn(props) {
    var fs                = window.FieldSight;
    var ProgrammeTodoList = fs.ProgrammeTodoList;
    var Button            = fs.Button;
    var onSelect          = props.onSelect || function () {};

    var ctx = React.useContext(ProgrammeContext);
    if (!ctx) {
      console.warn('[ProgrammeMiddleColumn] ProgrammeContext missing');
      return null;
    }
    var s = ctx.state;

    if (s.status === 'loading') {
      return React.createElement('div', { className: 'fs-programme' },
        React.createElement('div', { className: 'fs-programme__loading' },
          'Loading programme…'),
      );
    }
    if (s.status === 'error') {
      return React.createElement('div', { className: 'fs-programme' },
        React.createElement('div', { className: 'fs-programme__empty' },
          'Could not load programme. ' + (s.error && s.error.message || '')),
      );
    }
    if (s.status === 'access_denied') {
      var AccessDenied = fs.AccessDenied;
      return React.createElement('div', { className: 'fs-programme' },
        AccessDenied
          ? React.createElement(AccessDenied, {
              scope:   'this programme',
              message: s.message,
            })
          : React.createElement('div', null, 'Access denied.'),
      );
    }

    var p           = s.programme;
    var selectedId  = props.selectedItem && props.selectedItem.kind === 'programme_task'
      ? props.selectedItem.task_id
      : null;

    return React.createElement('div', { className: 'fs-programme' },

      /* Header */
      React.createElement('div', { className: 'fs-programme__header' },
        React.createElement('h2', { className: 'fs-programme__title' }, p.name),
        React.createElement('div', { className: 'fs-programme__subtitle' },
          fmtDate(p.start_date) + ' → ' + fmtDate(p.end_date)
            + ' · ' + s.leaves.length + ' tasks'
            + ' · ' + (s.critical && s.critical.size) + ' on critical path'),

        /* View + tier toggles */
        React.createElement('div', { className: 'fs-programme__toolbar' },
          React.createElement('div', { className: 'fs-programme__view-toggle' },
            React.createElement('button', {
              type:      'button',
              className: 'fs-programme__toggle' + (ctx.view === 'gantt' ? ' fs-programme__toggle--active' : ''),
              onClick:   function () { ctx.setView('gantt'); },
            }, 'Gantt'),
            React.createElement('button', {
              type:      'button',
              className: 'fs-programme__toggle' + (ctx.view === 'todo' ? ' fs-programme__toggle--active' : ''),
              onClick:   function () { ctx.setView('todo'); },
            }, 'TO-DO'),
          ),

          ctx.view === 'gantt'
            ? React.createElement('div', { className: 'fs-programme__tier-toggle' },
                ['day', 'week', 'month'].map(function (k) {
                  return React.createElement('button', {
                    key:       k,
                    type:      'button',
                    className: 'fs-programme__toggle fs-programme__toggle--small'
                                + (ctx.tier === k ? ' fs-programme__toggle--active' : ''),
                    onClick:   function () { ctx.setTier(k); },
                  }, k.charAt(0).toUpperCase() + k.slice(1));
                }),
              )
            : null,
        ),
      ),

      /* Body */
      ctx.view === 'gantt'
        ? React.createElement(GanttView, {
            selectedItem: props.selectedItem,
            onSelect:     onSelect,
          })
        : React.createElement(ProgrammeTodoList, {
            tasks:       s.leaves,
            today:       s.today,
            criticalSet: s.critical,
            selectedId:  selectedId,
            onSelect:    function (t) {
              onSelect({
                kind:    'programme_task',
                id:      'task_' + t.task_id,
                task_id: t.task_id,
                task:    t,
              });
            },
          }),
    );
  }

  /* ---------- ProgrammeRightDetail ------------------------------------ */

  function ProgrammeRightDetail(props) {
    var fs       = window.FieldSight;
    var Card     = fs.Card;
    var Badge    = fs.Badge;
    var Button   = fs.Button;
    var IconBtn  = fs.IconButton;

    var ctx = React.useContext(ProgrammeContext);
    var sel = props.selectedItem;

    /* Linked-action lazy fetch — only when a task is selected and it
       carries linked_action_items. We pull each action's text from
       /api/timeline so the right pane shows the real action wording
       (not just the indices). */
    var refLinks = React.useState({ status: 'idle', items: [] });
    var linksS   = refLinks[0];
    var setLinks = refLinks[1];

    React.useEffect(function () {
      if (!sel || sel.kind !== 'programme_task') {
        setLinks({ status: 'idle', items: [] });
        return undefined;
      }
      var linked = (sel.task && sel.task.linked_action_items) || [];
      if (linked.length === 0) {
        setLinks({ status: 'ok', items: [] });
        return undefined;
      }
      var cancelled = false;
      setLinks({ status: 'loading', items: [] });

      /* Group by date so we issue one getTimeline per unique date. */
      var byDate = {};
      linked.forEach(function (l) {
        (byDate[l.date] = byDate[l.date] || []).push(l);
      });
      var dates = Object.keys(byDate);

      Promise.all(dates.map(function (d) {
        return window.FS.api.timeline.getTimeline({ date: d, user: null })
          .then(function (r) { return { date: d, report: r }; });
      })).then(function (perDate) {
        if (cancelled) return;
        var items = [];
        perDate.forEach(function (x) {
          if (!x.report || x.report._notFound) return;
          /* When user is null and there's only one user, getTimeline
             may return the single report directly OR an
             available_users disambiguation. Try to handle either. */
          var report = x.report.available_users && x.report.available_users.length === 1
            ? null  /* multi-user disambiguation — defer to user picking on /timeline */
            : x.report;

          (byDate[x.date] || []).forEach(function (link) {
            var topic = report && (report.topics || []).filter(function (t) {
              return t.topic_id === link.topic_id;
            })[0];
            if (!topic) {
              items.push({
                date: link.date, topic_id: link.topic_id,
                action_index: link.action_index,
                text: '(action not found in fixture)',
                user_folder: null,
              });
              return;
            }
            var action = (topic.action_items || [])[link.action_index];
            items.push({
              date:         link.date,
              topic_id:     link.topic_id,
              action_index: link.action_index,
              text:         action ? action.action : '(action_index out of range)',
              responsible:  action ? action.responsible : null,
              topic_title:  topic.topic_title,
              user_folder:  report && report.user_name
                              ? window.FS.api.folderName(report.user_name)
                              : null,
            });
          });
        });
        setLinks({ status: 'ok', items: items });
      }).catch(function () {
        if (!cancelled) setLinks({ status: 'error', items: [] });
      });

      return function () { cancelled = true; };
    }, [sel && sel.task && sel.task.task_id]);

    if (!sel || sel.kind !== 'programme_task') {
      return React.createElement('div', { className: 'fs-programme-detail__placeholder' },
        React.createElement('div', { className: 'fs-programme-detail__placeholder-title' },
          'Select a task'),
        React.createElement('div', { className: 'fs-programme-detail__placeholder-body' },
          'Pick any task from the Gantt or the TO-DO list to see its full detail and linked report actions.'),
      );
    }

    var t = sel.task;
    var s = ctx && ctx.state;
    var critical = s && s.critical && s.critical.has(t.task_id);

    var statusTone = ({
      not_started: 'neutral',
      in_progress: 'info',
      completed:   'success',
      blocked:     'danger',
      delayed:     'warning',
    })[t.status] || 'neutral';

    var statusLabel = ({
      not_started: 'Not started',
      in_progress: 'In progress',
      completed:   'Done',
      blocked:     'Blocked',
      delayed:     'Delayed',
    })[t.status] || t.status;

    function openLink(link) {
      if (!link.user_folder) {
        /* Fall back to date-only navigation when reporter ambiguous. */
        window.FS.Router.navigate('/timeline?date=' + encodeURIComponent(link.date));
        return;
      }
      window.FS.Router.navigate(
        '/timeline?date=' + encodeURIComponent(link.date)
          + '&user=' + encodeURIComponent(link.user_folder));
    }

    return React.createElement('div', { className: 'fs-programme-detail' },

      /* Header */
      React.createElement('div', { className: 'fs-programme-detail__header' },
        React.createElement('div', { className: 'fs-programme-detail__header-main' },
          React.createElement('div', { className: 'fs-programme-detail__wbs' },
            'WBS ' + t.wbs),
          React.createElement('h2', { className: 'fs-programme-detail__title' },
            t.name),
          React.createElement('div', { className: 'fs-programme-detail__metaline' },
            React.createElement(Badge, { tone: statusTone, size: 'sm', prefixDot: true },
              statusLabel),
            critical ? React.createElement(Badge, {
              tone: 'danger', size: 'sm', variant: 'subtle',
            }, 'Critical path') : null,
            (t.tags || []).map(function (tag) {
              return React.createElement(Badge, {
                key: tag, tone: 'warning', size: 'sm', variant: 'outline',
              }, tag.replace(/_/g, ' '));
            }),
          ),
        ),
        IconBtn ? React.createElement(IconBtn, {
          icon: 'x', ariaLabel: 'Close detail', size: 'sm',
          onClick: function () { if (props.onClose) props.onClose(); },
        }) : null,
      ),

      /* Detail rows */
      React.createElement('div', { className: 'fs-programme-detail__rows' },
        React.createElement(DetailRow, {
          label: 'Dates',
          value: fmtDate(t.start) + ' → ' + fmtDate(t.end)
                  + '   (' + (t.duration_days || 0) + ' days)',
        }),
        React.createElement(DetailRow, {
          label: 'Baseline',
          value: fmtDate(t.baseline_start) + ' → ' + fmtDate(t.baseline_end),
        }),
        React.createElement(DetailRow, {
          label: 'Progress',
          value: (t.progress_pct || 0) + '%',
        }),
        React.createElement(DetailRow, {
          label: 'Assignees',
          value: (t.assignees || []).map(function (a) { return a.replace(/_/g, ' '); }).join(', ') || '—',
        }),
        React.createElement(DetailRow, {
          label: 'Resource pool',
          value: (t.resource_pool || []).join(', ') || '—',
        }),
        React.createElement(DetailRow, {
          label: 'Depends on',
          value: (t.depends_on || []).join(', ') || '—',
        }),
      ),

      /* Linked actions */
      React.createElement('div', { className: 'fs-programme-detail__links' },
        React.createElement('div', { className: 'fs-programme-detail__links-label' },
          'Linked report actions'),
        linksS.status === 'idle' || linksS.status === 'loading'
          ? React.createElement('div', { className: 'fs-programme-detail__empty' },
              'Loading linked actions…')
          : linksS.status === 'error'
          ? React.createElement('div', { className: 'fs-programme-detail__empty' },
              'Could not load linked actions.')
          : linksS.items.length === 0
          ? React.createElement('div', { className: 'fs-programme-detail__empty' },
              'No linked report actions for this task.')
          : React.createElement('div', { className: 'fs-programme-detail__links-list' },
              linksS.items.map(function (link, i) {
                return React.createElement('button', {
                  key:       link.date + '_' + link.topic_id + '_' + link.action_index,
                  type:      'button',
                  className: 'fs-programme-detail__link',
                  onClick:   function () { openLink(link); },
                  title:     'Open on /timeline',
                },
                  React.createElement('div', { className: 'fs-programme-detail__link-text' },
                    link.text),
                  React.createElement('div', { className: 'fs-programme-detail__link-meta' },
                    fmtDate(link.date)
                      + (link.topic_title ? ' · ' + link.topic_title : '')
                      + (link.responsible ? ' · ' + link.responsible : '')),
                );
              }),
            ),
      ),
    );
  }

  function DetailRow(props) {
    return React.createElement('div', { className: 'fs-programme-detail__row' },
      React.createElement('div', { className: 'fs-programme-detail__row-label' },
        props.label),
      React.createElement('div', { className: 'fs-programme-detail__row-value' },
        props.value),
    );
  }

  /* ---------- Register --------------------------------------------------- */

  if (!window.FieldSight) window.FieldSight = {};
  if (!window.FieldSight.PAGES) window.FieldSight.PAGES = {};
  window.FieldSight.PAGES['/programme'] = {
    Middle:   ProgrammeMiddleColumn,
    Right:    ProgrammeRightDetail,
    Provider: ProgrammeProvider,
    /* Sprint 4.7 — Programme uses the entire content area (Gantt is
       wider than the default middle column will ever be). The right
       detail comes in as a slide-in drawer when a task is selected;
       AppShell wires it via window.FieldSight.RightDrawer. */
    layout:   'full-width',
  };

})();
