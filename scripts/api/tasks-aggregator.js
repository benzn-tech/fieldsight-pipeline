/* ==========================================================================
   FieldSight API · Tasks aggregator (Sprint 4.2)
   --------------------------------------------------------------------------
   Joins two existing data shapes into a single flat list the Tasks
   page can render without re-doing the join itself:

     • Action SOURCE    — lives inside daily-report topics
                          (text, responsible, deadline, priority)
     • Audit OVERLAY    — /api/actions per date
                          (checked, checked_by, checked_at)

   Single stable contract, regardless of mock vs real backend or
   whether a future `/api/actions/all` endpoint is added — UI
   consumers see the same row shape.

   Returned row shape:
     {
       id:           '<date>_<topic_id>_<action_index>',
       date:         'YYYY-MM-DD',
       topic_id:     number,
       action_index: number,
       action:       string,                       // free text
       responsible:  string | null,
       priority:     'high' | 'medium' | 'low' | null,
       deadline:     string | null,                // free text e.g. "Today 09:00"
       topic_title:  string,
       topic_category: string,
       user_name:    string,                       // owner of the report
       user_folder:  string,                       // folder form
       audit: {
         checked:    boolean,
         checked_by: string | null,
         checked_at: ISO string | null,
       }
     }

   Exported to:
     window.FS.api.tasks.getActionsResolvedRange({ from, to, user? })
   ========================================================================== */

(function () {
  'use strict';

  /* Resolve the user param honouring worker-forced-self, mirroring
     the rule used by today.js + activity.js. */
  function resolveUser(explicitUser) {
    var caller = (window.AuthMock && window.AuthMock.currentUser) || {};
    if (caller.role === 'worker') {
      return caller.name ? window.FS.api.folderName(caller.name) : null;
    }
    if (explicitUser) return explicitUser;
    var isAdmin = caller.role === 'admin' || caller.role === 'gm' || caller.isAdmin;
    if (!isAdmin && caller.name) return window.FS.api.folderName(caller.name);
    return explicitUser || null;
  }

  async function getActionsResolvedRange(opts) {
    opts = opts || {};
    var from = opts.from, to = opts.to;
    if (!from || !to) return { rows: [], from: from, to: to };
    var user = resolveUser(opts.user);

    /* 1) Discover days that actually have reports. /api/dates is the
       cheapest call — single round-trip, gives us a hasReport flag
       per date. We only fan out timeline + actions for those days. */
    var datesRes = await window.FS.api.dates.getDates({ months: 3 });
    if (datesRes && datesRes._accessDenied) {
      return { _accessDenied: true, error: datesRes.error };
    }
    var datesMap = (datesRes && datesRes.dates) || {};
    var datesInRange = Object.keys(datesMap)
      .filter(function (d) { return d >= from && d <= to && datesMap[d].hasReport; })
      .sort();

    if (datesInRange.length === 0) {
      return { rows: [], from: from, to: to, user: user };
    }

    /* 2) Fan out timeline (action source) AND actions (audit) per day,
       in parallel.
       Sprint 8 follow-up — admin fan-out across all known users when
       no explicit user is provided, matching compliance-aggregator. */
    var caller  = (window.AuthMock && window.AuthMock.currentUser) || {};
    var isAdmin = caller.role === 'admin' || caller.role === 'gm' || !!caller.isAdmin;
    var folders = (!user && isAdmin)
      ? ((window.FieldSight && window.FieldSight.fixtures
          && window.FieldSight.fixtures.sites && window.FieldSight.fixtures.sites.users) || [])
          .map(function (u) { return u.folder_name; }).filter(Boolean)
      : null;

    var timelinePromise;
    if (folders && folders.length > 0) {
      timelinePromise = Promise.all(
        datesInRange.reduce(function (acc, d) {
          folders.forEach(function (f) {
            acc.push(window.FS.api.timeline.getTimeline({ date: d, user: f })
              .then(function (r) { return { date: d, report: r }; }));
          });
          return acc;
        }, [])
      );
    } else {
      timelinePromise = Promise.all(datesInRange.map(function (d) {
        return window.FS.api.timeline.getTimeline({ date: d, user: user })
          .then(function (r) { return { date: d, report: r }; });
      }));
    }
    var auditPromise = window.FS.api.actions.getActionsRange({
      from: datesInRange[0], to: datesInRange[datesInRange.length - 1],
    });

    var both = await Promise.all([timelinePromise, auditPromise]);
    var perDay = both[0];
    var auditRange = both[1];

    /* 3) Surface page-level access-denied if either fan-out hit it. */
    if (auditRange && auditRange._accessDenied) {
      return { _accessDenied: true, error: auditRange.error };
    }
    var deniedHit = perDay.filter(function (x) {
      return x.report && x.report._accessDenied;
    })[0];
    if (deniedHit) {
      return { _accessDenied: true, error: deniedHit.report.error };
    }

    /* 4) Flatten into rows. Skip days where the report was not found,
       didn't materialise (admin disambiguation), or carried 0 topics. */
    var auditByDate = (auditRange && auditRange.byDate) || {};
    var rows = [];
    perDay.forEach(function (x) {
      var r = x.report;
      if (!r || r._notFound || r.available_users) return;
      (r.topics || []).forEach(function (t) {
        (t.action_items || []).forEach(function (a, idx) {
          var key   = (auditByDate[x.date] || {})[t.topic_id + '_' + idx] || {};
          rows.push({
            id:             x.date + '_' + t.topic_id + '_' + idx,
            date:           x.date,
            topic_id:       t.topic_id,
            action_index:   idx,
            action:         a.action,
            responsible:    a.responsible || null,
            priority:       a.priority || null,
            deadline:       a.deadline || null,
            topic_title:    t.topic_title,
            topic_category: t.category,
            user_name:      r.user_name,
            user_folder:    r.user_name ? window.FS.api.folderName(r.user_name) : null,
            audit: {
              checked:    !!key.checked,
              checked_by: key.checked_by || null,
              checked_at: key.checked_at || null,
            },
          });
        });
      });
    });

    return { rows: rows, from: from, to: to, user: user, dates: datesInRange };
  }

  /* ────────────────────────────────────────────────────────────────────
     Sprint 11 C.1 — getCrossDayAudit({from, to, user})
     --------------------------------------------------------------------
     Mock spec for the future endpoint
       GET /api/actions/all?from=YYYY-MM-DD&to=YYYY-MM-DD&user=<folder>
     Returns: { entries: [...], from, to, user }
       entry = {
         action_id:        '<date>_<topic_id>_<action_index>',  // unique
         topic_action_key: '<topic_id>_<action_index>',          // groups
                                                                 // same logical
                                                                 // action across
                                                                 // dates
         date:             'YYYY-MM-DD',
         topic_id, action_index,
         checked, checked_by, checked_at,
       }

     Cross-day flatten of `actions.getActionsRange`'s `{byDate}` shape so
     /today's WeeklyCompletionKpi and /tasks's history drawer can iterate
     a single flat array. Pure data — no timeline / report join (use
     `getActionsResolvedRange` for that).

     Backend wiring (Sprint 12+): drop the localStorage-backed
     `actions.getActionsRange` fan-out and replace with one
     `GET /api/actions/all` call returning the same shape. UI is
     unchanged.
     ────────────────────────────────────────────────────────────────── */

  async function getCrossDayAudit(opts) {
    opts = opts || {};
    var from = opts.from;
    var to   = opts.to;
    if (!from || !to) {
      return { entries: [], from: from, to: to };
    }

    var user      = resolveUser(opts.user);
    var auditRes  = await window.FS.api.actions.getActionsRange({
      from: from, to: to,
    });
    if (auditRes && auditRes._accessDenied) {
      return { _accessDenied: true, error: auditRes.error };
    }

    var byDate  = (auditRes && auditRes.byDate) || {};
    var entries = [];
    Object.keys(byDate).sort().forEach(function (date) {
      var dayActions = byDate[date] || {};
      Object.keys(dayActions).forEach(function (key) {
        var rec   = dayActions[key] || {};
        var parts = key.split('_');
        entries.push({
          action_id:        date + '_' + key,
          topic_action_key: key,
          date:             date,
          topic_id:         parseInt(parts[0], 10),
          action_index:     parseInt(parts[1], 10),
          checked:          !!rec.checked,
          checked_by:       rec.checked_by || null,
          checked_at:       rec.checked_at || null,
        });
      });
    });

    return { entries: entries, from: from, to: to, user: user };
  }

  if (!window.FS) window.FS = {};
  if (!window.FS.api) window.FS.api = {};
  window.FS.api.tasks = {
    getActionsResolvedRange: getActionsResolvedRange,
    getCrossDayAudit:        getCrossDayAudit,
  };

})();
