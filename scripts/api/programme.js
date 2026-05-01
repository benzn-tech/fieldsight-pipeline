/* ==========================================================================
   FieldSight API · Programme (Sprint 4.4 — MVP)
   --------------------------------------------------------------------------
   Hypothetical backend endpoints (NOT YET implemented server-side):
     GET /api/programmes/:programme_id
       → Full programme object: { programme_id, name, site_id, start_date,
         end_date, baseline_*, critical_path[], tasks[] }
     GET /api/programmes/:programme_id/tasks?from=&to=&user=
       → Filtered tasks within a date window, optionally scoped to a
         user (for the worker-forced-self rule).

   Mock branch reads from window.FieldSight.fixtures.programme; backend
   branch is a stub for the future migration. UI consumers see the same
   row contract regardless.

   Worker rule (BACKEND-CONTEXT §3): when caller is a worker, scope the
   returned tasks to ones where the worker's folder name appears in
   `assignees`. Mock api does this client-side.

   Exported to:
     window.FS.api.programme = {
       getProgramme(programme_id),
       getProgrammeTasksForRange({ programme_id, from, to, user? }),
     }
   ========================================================================== */

(function () {
  'use strict';

  function fixtures() {
    return (window.FieldSight && window.FieldSight.fixtures) || {};
  }

  function callerFolder() {
    var u = (window.AuthMock && window.AuthMock.currentUser) || {};
    if (!u.name) return null;
    return window.FS.api.folderName(u.name);
  }

  function isAdminLike(u) {
    return u && (u.role === 'admin' || u.role === 'gm' || u.isAdmin);
  }

  function applyWorkerScope(tasks) {
    var caller = (window.AuthMock && window.AuthMock.currentUser) || {};
    if (caller.role !== 'worker') return tasks;
    var folder = callerFolder();
    if (!folder) return tasks;
    return tasks.filter(function (t) {
      if (t.status === 'group') return false;          /* hide WBS groups for workers */
      return (t.assignees || []).indexOf(folder) !== -1;
    });
  }

  function rangeOverlap(task, from, to) {
    if (!from || !to) return true;
    return !(task.end < from || task.start > to);
  }

  async function getProgramme(programme_id) {
    if (!window.FS.api.useMocks) {
      return window.FS.api.request('/programmes/' + encodeURIComponent(programme_id));
    }
    await window.FS.api.delay();
    var p = fixtures().programme;
    if (!p) return { _notFound: true, programme_id: programme_id };
    if (p.programme_id !== programme_id) {
      return { _notFound: true, programme_id: programme_id };
    }
    /* Deep-copy so callers can mutate state freely. */
    var copy = JSON.parse(JSON.stringify(p));
    copy.tasks = applyWorkerScope(copy.tasks);
    return copy;
  }

  async function getProgrammeTasksForRange(opts) {
    opts = opts || {};
    if (!window.FS.api.useMocks) {
      return window.FS.api.request(
        '/programmes/' + encodeURIComponent(opts.programme_id) + '/tasks',
        { params: { from: opts.from, to: opts.to, user: opts.user } });
    }
    await window.FS.api.delay();
    var p = fixtures().programme;
    if (!p || p.programme_id !== opts.programme_id) {
      return { tasks: [], programme_id: opts.programme_id };
    }
    var tasks = JSON.parse(JSON.stringify(p.tasks))
      .filter(function (t) { return t.status !== 'group'; })
      .filter(function (t) { return rangeOverlap(t, opts.from, opts.to); });
    tasks = applyWorkerScope(tasks);
    return {
      programme_id: opts.programme_id,
      tasks:        tasks,
      from:         opts.from || null,
      to:           opts.to   || null,
    };
  }

  if (!window.FS) window.FS = {};
  if (!window.FS.api) window.FS.api = {};
  window.FS.api.programme = {
    getProgramme:               getProgramme,
    getProgrammeTasksForRange:  getProgrammeTasksForRange,
  };

})();
