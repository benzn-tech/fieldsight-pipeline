/* ==========================================================================
   FieldSight API · Today-Programme adapter (Sprint 4.10)
   --------------------------------------------------------------------------
   Bridges Programme (the high-level work plan) into the Today page,
   so a user logging in sees the programme tasks they're scheduled
   on TODAY alongside the action items derived from yesterday's
   daily report.

   Returns rows shaped to slot into the existing `myTasks` list,
   distinguished by `source: 'programme'` (vs `'action_item'` for
   the existing daily-report-derived rows). Today's UI uses that
   marker to render a separate sub-group with the ProgrammeTaskCard
   visual variant.

   Worker rule (BACKEND-CONTEXT §3) is honoured automatically — the
   underlying FS.api.programme.getProgramme already filters tasks
   to the worker's assignments.

   Returned shape:
     {
       rows: [
         {
           source:        'programme',
           task_id:       'T-004',
           wbs:           '2.1',
           name:          'Steel frame',
           start:         '2026-05-01',
           end:           '2026-05-15',
           duration_days: 15,
           progress_pct:  12,
           status:        'in_progress',
           critical:      true,
           day_index:     1,        // 1-based: day N of M
           day_total:     15,
           assignees:     ['Jarley_Trainor', 'Ben_Lin', 'David_Barillaro'],
           linked_action_items: [{ date, topic_id, action_index }, ...],
         },
         ...
       ],
     }

   Exported to:
     window.FS.api.todayProgramme.getTodayProgrammeTasks({ today, user? })
   ========================================================================== */

(function () {
  'use strict';

  var DEFAULT_PROGRAMME_ID = 'sb1108-2026-q2';

  function callerFolder() {
    var u = (window.AuthMock && window.AuthMock.currentUser) || {};
    if (!u.name) return null;
    return window.FS.api.folderName(u.name);
  }

  function diffDays(fromISO, toISO) {
    var a = new Date(fromISO + 'T00:00:00Z').getTime();
    var b = new Date(toISO   + 'T00:00:00Z').getTime();
    return Math.round((b - a) / 86400000);
  }

  async function getTodayProgrammeTasks(opts) {
    opts = opts || {};
    var today = opts.today || (window.FS.api.todayNZDT && window.FS.api.todayNZDT());
    if (!today) return { rows: [] };

    var folder = opts.user || callerFolder();

    var prog = await window.FS.api.programme.getProgramme(DEFAULT_PROGRAMME_ID);
    if (!prog || prog._notFound || prog._accessDenied) {
      return { rows: [] };
    }

    var critical = new Set(prog.critical_path || []);
    var rows = [];

    (prog.tasks || []).forEach(function (t) {
      if (t.status === 'group') return;
      /* Today must fall within the task's date range. */
      if (today < t.start || today > t.end) return;
      /* Caller must be in the assignee list. (When folder is null —
         e.g. admin without an explicit folder — fall through and
         return everything they can see.) */
      if (folder && (t.assignees || []).indexOf(folder) === -1) return;

      var dayIndex = diffDays(t.start, today) + 1;          /* 1-based */
      var dayTotal = (t.duration_days != null)
        ? t.duration_days
        : (diffDays(t.start, t.end) + 1);

      rows.push({
        source:               'programme',
        task_id:              t.task_id,
        wbs:                  t.wbs,
        name:                 t.name,
        start:                t.start,
        end:                  t.end,
        duration_days:        dayTotal,
        progress_pct:         t.progress_pct || 0,
        status:               t.status,
        critical:             critical.has(t.task_id),
        day_index:            dayIndex,
        day_total:            dayTotal,
        assignees:            t.assignees || [],
        linked_action_items:  t.linked_action_items || [],
        tags:                 t.tags || [],
      });
    });

    /* Sort: critical first, then by progress desc (closest to done
       wraps up first in the user's mental model). */
    rows.sort(function (a, b) {
      if (a.critical !== b.critical) return a.critical ? -1 : 1;
      return (b.progress_pct || 0) - (a.progress_pct || 0);
    });

    return { rows: rows, today: today, user: folder };
  }

  if (!window.FS) window.FS = {};
  if (!window.FS.api) window.FS.api = {};
  window.FS.api.todayProgramme = {
    getTodayProgrammeTasks: getTodayProgrammeTasks,
  };

})();
