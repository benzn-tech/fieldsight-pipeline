/* ==========================================================================
   FieldSight API · User-activity aggregator (Sprint 4.6)
   --------------------------------------------------------------------------
   Replaces the chronological topic-flatten pattern of the old
   /activity page. Direction C (per design alternatives) — group
   field activity by user, so the caller can see at a glance what
   each person on their team has been doing across a time window.

   Output: one entry per user visible to the caller, each carrying:

     {
       user_name:     'Jarley Trainor',
       user_folder:   'Jarley_Trainor',
       role:          'site_manager',
       primary_site:  'sb1108-ellesmere',
       counts: {
         topics:       N,
         actions:      M,
         photos:       P,
         safety_flags: S,
       },
       events: [   // sorted desc by (date, time_label)
         {
           kind:        'topic' | 'action' | 'photo' | 'safety',
           date:        'YYYY-MM-DD',
           time_label:  'HH:MM' | null,
           topic_id:    number,
           topic_title: string,
           summary:     string,    // free-text snippet
           extra:       any,       // kind-specific payload
         },
         ...
       ],
     }

   Worker rule (BACKEND-CONTEXT §3): when caller is a worker, the
   aggregator reduces to a single entry — caller's own.

   Exported to:
     window.FS.api.userActivity.getUserActivityRange({ from, to })
   ========================================================================== */

(function () {
  'use strict';

  function isAdminLike(u) {
    return u && (u.role === 'admin' || u.role === 'gm' || u.isAdmin);
  }

  function startTime(time_range) {
    if (!time_range) return null;
    var m = String(time_range).match(/(\d{1,2}):(\d{2})/);
    if (!m) return null;
    return (m[1].length === 1 ? '0' + m[1] : m[1]) + ':' + m[2];
  }

  /* Resolve which user folders the caller is allowed to see. */
  function resolveVisibleUsers() {
    var caller   = (window.AuthMock && window.AuthMock.currentUser) || {};
    var fixtures = (window.FieldSight && window.FieldSight.fixtures && window.FieldSight.fixtures.sites) || { users: [] };
    var allUsers = fixtures.users || [];

    if (caller.role === 'worker') {
      var folder = window.FS.api.folderName(caller.name || '');
      return allUsers.filter(function (u) { return u.folder_name === folder; });
    }
    if (isAdminLike(caller)) return allUsers;
    /* site_manager / pm see users on the sites they manage. The
       fixture doesn't carry a managed-sites array, so fall back to
       caller's primary_site for now (consistent with /sites page). */
    var callerRecord = allUsers.filter(function (u) { return u.name === caller.name; })[0];
    var primary = callerRecord && callerRecord.primary_site;
    if (!primary) return allUsers;
    return allUsers.filter(function (u) {
      return (u.sites || []).indexOf(primary) !== -1;
    });
  }

  async function getUserActivityRange(opts) {
    opts = opts || {};
    var from = opts.from, to = opts.to;
    if (!from || !to) return { users: [], from: from, to: to };

    /* Build the dates list — only days that actually have a report
       are worth fanning out; reuse /api/dates as the cheap probe. */
    var datesRes = await window.FS.api.dates.getDates({ months: 3 });
    if (datesRes && datesRes._accessDenied) {
      return { _accessDenied: true, error: datesRes.error };
    }
    var datesMap = (datesRes && datesRes.dates) || {};
    var dates = Object.keys(datesMap)
      .filter(function (d) { return d >= from && d <= to && datesMap[d].hasReport; })
      .sort();

    var visibleUsers = resolveVisibleUsers();
    if (visibleUsers.length === 0 || dates.length === 0) {
      return { users: visibleUsers.map(toBlankAggregation),
               from: from, to: to, dates: dates };
    }

    /* Per (date × user) fan-out. Each (date, user) tuple may yield a
       _notFound (no report from that user on that date) — that's
       expected and dropped silently. Audit overlay piggy-backs on
       the same date list via getActionsRange. */
    var perCall = [];
    visibleUsers.forEach(function (u) {
      dates.forEach(function (d) {
        perCall.push({ user: u, date: d });
      });
    });

    var [reports, audit] = await Promise.all([
      Promise.all(perCall.map(function (k) {
        return window.FS.api.timeline.getTimeline({ date: k.date, user: k.user.folder_name })
          .then(function (r) { return Object.assign({ report: r }, k); });
      })),
      window.FS.api.actions.getActionsRange({ from: dates[0], to: dates[dates.length - 1] }),
    ]);

    if (audit && audit._accessDenied) {
      return { _accessDenied: true, error: audit.error };
    }
    var deniedHit = reports.filter(function (r) { return r.report && r.report._accessDenied; })[0];
    if (deniedHit) return { _accessDenied: true, error: deniedHit.report.error };

    var byDate = (audit && audit.byDate) || {};

    /* Group results by user. perUserByName is the attribution index —
       events go to whoever's name appears in topic.participants /
       action.responsible / report.user_name, regardless of which
       report we pulled them from. (A worker's name can show up in
       another worker's report — e.g. as the responsible party for
       an action — and we still want that activity to count.) */
    var perUser       = {};
    var perUserByName = {};
    visibleUsers.forEach(function (u) {
      var bucket = toBlankAggregation(u);
      perUser[u.folder_name] = bucket;
      perUserByName[u.name]  = bucket;
    });

    /* Dedupe seen (date, topic_id) pairs across users — multiple
       users may co-author the same topic, but we only want to count
       each topic ONCE per user. */
    var seenTopic = {};
    var seenAction = {};
    var seenPhoto = {};
    var seenSafety = {};

    reports.forEach(function (rec) {
      var r = rec.report;
      if (!r || r._notFound || r.available_users) return;
      var date         = rec.date;
      var auditForDate = byDate[date] || {};

      (r.topics || []).forEach(function (t) {

        /* Topic participation — every participant on the topic gets
           credit (not just the report owner). */
        (t.participants || []).forEach(function (participantName) {
          var bucket = perUserByName[participantName];
          if (!bucket) return;
          var key = participantName + '|' + date + '|' + t.topic_id;
          if (seenTopic[key]) return;
          seenTopic[key] = true;

          bucket.counts.topics++;
          bucket.events.push({
            kind:        'topic',
            date:        date,
            time_label:  startTime(t.time_range),
            topic_id:    t.topic_id,
            topic_title: t.topic_title,
            summary:     t.summary || '',
            extra:       { participants: t.participants || [] },
          });
        });

        /* Action items: attribute to the responsible person, even
           when the action lives in someone else's report. */
        (t.action_items || []).forEach(function (a, idx) {
          var bucket = perUserByName[a.responsible];
          if (!bucket) return;
          var key = a.responsible + '|' + date + '|' + t.topic_id + '|' + idx;
          if (seenAction[key]) return;
          seenAction[key] = true;

          var auditKey = auditForDate[t.topic_id + '_' + idx] || {};
          bucket.counts.actions++;
          bucket.events.push({
            kind:        'action',
            date:        date,
            time_label:  startTime(t.time_range),
            topic_id:    t.topic_id,
            topic_title: t.topic_title,
            summary:     a.action,
            extra:       {
              priority:   a.priority,
              deadline:   a.deadline,
              checked:    !!auditKey.checked,
              checked_at: auditKey.checked_at || null,
            },
          });
        });

        /* Safety flags + photos remain attributed to the report
           owner (no per-photo / per-flag author in the daily-report
           shape — same convention as PhotoGrid). */
        if (perUserByName[r.user_name]) {
          var ownerBucket = perUserByName[r.user_name];

          (t.safety_flags || []).forEach(function (f, fi) {
            var key = r.user_name + '|safety|' + date + '|' + t.topic_id + '|' + fi;
            if (seenSafety[key]) return;
            seenSafety[key] = true;
            ownerBucket.counts.safety_flags++;
            ownerBucket.events.push({
              kind:        'safety',
              date:        date,
              time_label:  startTime(t.time_range),
              topic_id:    t.topic_id,
              topic_title: t.topic_title,
              summary:     typeof f === 'string' ? f : (f.flag || f.text || ''),
              extra:       f,
            });
          });

          (t.related_photos || []).forEach(function (filename) {
            var key = r.user_name + '|photo|' + date + '|' + filename;
            if (seenPhoto[key]) return;
            seenPhoto[key] = true;
            ownerBucket.counts.photos++;
            ownerBucket.events.push({
              kind:        'photo',
              date:        date,
              time_label:  startTime(t.time_range),
              topic_id:    t.topic_id,
              topic_title: t.topic_title,
              summary:     filename,
              extra:       { filename: filename },
            });
          });
        }
      });
    });

    /* Sort each user's events desc by (date, time_label). */
    Object.keys(perUser).forEach(function (k) {
      perUser[k].events.sort(function (a, b) {
        if (a.date !== b.date) return a.date < b.date ? 1 : -1;
        return (a.time_label || '') < (b.time_label || '') ? 1 : -1;
      });
    });

    var users = visibleUsers.map(function (u) { return perUser[u.folder_name]; });
    return { users: users, from: from, to: to, dates: dates };
  }

  function toBlankAggregation(u) {
    return {
      user_name:    u.name,
      user_folder:  u.folder_name,
      role:         u.role,
      primary_site: u.primary_site,
      counts:       { topics: 0, actions: 0, photos: 0, safety_flags: 0 },
      events:       [],
    };
  }

  if (!window.FS) window.FS = {};
  if (!window.FS.api) window.FS.api = {};
  window.FS.api.userActivity = {
    getUserActivityRange: getUserActivityRange,
  };

})();
