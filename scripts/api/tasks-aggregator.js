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
       in parallel. */
    var timelinePromise = Promise.all(datesInRange.map(function (d) {
      return window.FS.api.timeline.getTimeline({ date: d, user: user })
        .then(function (r) { return { date: d, report: r }; });
    }));
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

  if (!window.FS) window.FS = {};
  if (!window.FS.api) window.FS.api = {};
  window.FS.api.tasks = {
    getActionsResolvedRange: getActionsResolvedRange,
  };

})();
