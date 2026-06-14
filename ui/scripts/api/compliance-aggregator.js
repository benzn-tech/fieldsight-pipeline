/* ==========================================================================
   FieldSight API · Compliance aggregator (Sprint 6.0)
   --------------------------------------------------------------------------
   Cross-day fan-out for the two compliance pages, /safety and /quality.

   Each call iterates every date with a report inside [from, to], pulls the
   timeline for the resolved user, and flattens the relevant compliance
   collection into a single rows[] array. Mirrors the shape and fan-out
   pattern of tasks-aggregator.js so both pages can render without
   re-doing the join.

   Two parallel exports:

     window.FS.api.compliance.getSafetyRange({ from, to, user? })
       → {
           rows: [
             {
               id:                  '<date>_<topic_id>_<source>_<idx>',
               date:                'YYYY-MM-DD',
               site:                string,
               user_name:           string,
               user_folder:         string,
               topic_id:            number,
               topic_title:         string,
               topic_category:      'safety' | 'progress' | 'quality',
               source:              'observation' | 'topic_flag',
               observation:         string,
               risk_level:          'high' | 'medium' | 'low',
               recommended_action:  string | null,
               location:            string | null,    // null for topic_flag
               who_raised:          string | null,    // null for topic_flag
               status:              'open' | 'resolved',  // see _AUDIT-2 below
             }
           ],
           from, to, user, dates: [...]
         }

     window.FS.api.compliance.getQualityRange({ from, to, user? })
       → {
           rows: [
             {
               id:                  '<date>_<topic_id>_<source>_<idx>',
               date:                'YYYY-MM-DD',
               site:                string,
               user_name:           string,
               user_folder:         string,
               topic_id:            number,    // -1 for report-level Q&C items
               topic_title:         string,    // 'Quality & Compliance' synth title for report-level
               topic_category:      'quality' | 'progress' | 'safety',
               source:              'qc_item' | 'topic_quality',
               item:                string,        // headline
               status:               string,        // 'completed' | 'concern' | etc
               details:              string | null,
               follow_up_needed:    boolean,
               who_raised:          string | null,    // null for qc_item
             }
           ],
           from, to, user, dates: [...]
         }

   ─── _AUDIT (fixture audit notes) ─────────────────────────────────────────

   _AUDIT-1 · Fixture sparsity.
       dates.fixture.js advertises ~24 days with hasReport: true between
       early-March 2026 and 2026-04-29, BUT daily-report.fixture.js only
       carries one real report: 2026-04-29 / Jarley_Trainor. Every other
       day's getTimeline call returns _notFound. This aggregator handles
       that silently (skips _notFound days, mirrors tasks-aggregator
       line 110 behaviour) — UI will simply render 1 day's worth of
       rows for any range that includes 2026-04-29, and an empty list
       for any other range.

       Resolution: Sprint 7+ fixture expansion can populate adjacent
       days. No code change needed here.

   _AUDIT-2 · No `status` field on safety_observations or safety_flags.
       Schema (BACKEND-CONTEXT §5.1) doesn't carry a status —
       observations are append-only at capture time. UI needs an open/
       resolved split for the KPI strip. Aggregator synthesises:
         status = 'open' for every row in mock land.
       When a real backend `POST /api/safety/:id/resolve` ships, the
       row shape gains a real status without consumer changes.

   _AUDIT-3 · Permission scope vs fan-out.
       resolveUser() clamps non-admin / non-gm callers to their own
       folder (matching tasks-aggregator.js:46-55). hse_manager has
       safety:view:org and *should* see org-wide flags, but in mock
       land we don't fan out across `available_users`. Behaviour
       parity with tasks-aggregator was prioritised over correctness;
       the fixture gap (1 author only) means it's not user-visible
       anyway. Sprint 7+ to revisit when fan-out gains real value.

   _AUDIT-4 · Field shapes confirmed.
       Every report fixture has: safety_observations (array),
       quality_and_compliance (array), and topics[].safety_flags
       (array, may be empty), and topics[].category (string).
       Verified against scripts/mock/daily-report.fixture.js:
       29-247 (single report).

   ────────────────────────────────────────────────────────────────────────── */

(function () {
  'use strict';

  /* Resolve user respecting worker-forced-self — copy of
     tasks-aggregator.js:46-55 (intentional parity). */
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

  /* Sprint 8 follow-up — admin fan-out across all known users when no
     explicit user is provided. Without this, getTimeline(date, user=null)
     for admin returns the available_users disambiguation envelope, which
     downstream loops skip — yielding empty Tasks/Safety/Quality/Evidence
     pages for admin. We materialise (date × user) cross-product instead. */
  function adminUserFolders() {
    var fx = (window.FieldSight && window.FieldSight.fixtures
      && window.FieldSight.fixtures.sites) || {};
    return (fx.users || []).map(function (u) { return u.folder_name; })
      .filter(Boolean);
  }
  function isAdminCaller() {
    var c = (window.AuthMock && window.AuthMock.currentUser) || {};
    return c.role === 'admin' || c.role === 'gm' || !!c.isAdmin;
  }

  /* Internal: discover dates with reports in [from, to], then fan out
     getTimeline for each. Returns { perDay: [{date, report}], denied? }. */
  async function fanoutDates(from, to, user) {
    var datesRes = await window.FS.api.dates.getDates({ months: 3 });
    if (datesRes && datesRes._accessDenied) {
      return { _accessDenied: true, error: datesRes.error };
    }
    var datesMap = (datesRes && datesRes.dates) || {};
    var datesInRange = Object.keys(datesMap)
      .filter(function (d) { return d >= from && d <= to && datesMap[d].hasReport; })
      .sort();

    if (datesInRange.length === 0) {
      return { perDay: [], dates: [] };
    }

    /* Admin path: cross-product (date × all users) so every report in
       the window gets included rather than being short-circuited by
       the available_users envelope. */
    if (!user && isAdminCaller()) {
      var folders = adminUserFolders();
      var perDayAdmin = await Promise.all(
        datesInRange.reduce(function (acc, d) {
          folders.forEach(function (f) {
            acc.push(window.FS.api.timeline.getTimeline({ date: d, user: f })
              .then(function (r) { return { date: d, report: r }; }));
          });
          return acc;
        }, [])
      );
      var deniedAdmin = perDayAdmin.filter(function (x) {
        return x.report && x.report._accessDenied;
      })[0];
      if (deniedAdmin) {
        return { _accessDenied: true, error: deniedAdmin.report.error };
      }
      return { perDay: perDayAdmin, dates: datesInRange };
    }

    var perDay = await Promise.all(datesInRange.map(function (d) {
      return window.FS.api.timeline.getTimeline({ date: d, user: user })
        .then(function (r) { return { date: d, report: r }; });
    }));

    var deniedHit = perDay.filter(function (x) {
      return x.report && x.report._accessDenied;
    })[0];
    if (deniedHit) {
      return { _accessDenied: true, error: deniedHit.report.error };
    }

    return { perDay: perDay, dates: datesInRange };
  }

  /* ─── Safety ─────────────────────────────────────────────────────────── */

  async function getSafetyRange(opts) {
    opts = opts || {};
    var from = opts.from, to = opts.to;
    if (!from || !to) return { rows: [], from: from, to: to };
    var user = resolveUser(opts.user);

    var fanout = await fanoutDates(from, to, user);
    if (fanout._accessDenied) {
      return { _accessDenied: true, error: fanout.error };
    }

    var rows = [];
    fanout.perDay.forEach(function (x) {
      var r = x.report;
      if (!r || r._notFound || r.available_users) return;
      var folder = r.user_name ? window.FS.api.folderName(r.user_name) : null;

      /* a) Report-level safety_observations (richer — has location +
         who_raised). */
      (r.safety_observations || []).forEach(function (o, idx) {
        rows.push({
          id:                 x.date + '_obs_' + idx,
          date:               x.date,
          site:               r.site || null,
          user_name:          r.user_name || null,
          user_folder:        folder,
          topic_id:           -1,
          topic_title:        'Site safety observations',
          topic_category:     'safety',
          source:             'observation',
          observation:        o.observation,
          risk_level:         o.risk_level,
          recommended_action: o.recommended_action || null,
          location:           o.location || null,
          who_raised:         o.who_raised || null,
          status:             'open',  /* see _AUDIT-2 */
        });
      });

      /* b) Topic-level safety_flags (less rich — no location/who, but
         carries topic context). Also surfaces related_photos so the
         /safety right panel can render them inline (Sprint 6.6.3 —
         removes the round-trip to /timeline just to see the photos). */
      (r.topics || []).forEach(function (t) {
        (t.safety_flags || []).forEach(function (f, idx) {
          rows.push({
            id:                 x.date + '_' + t.topic_id + '_flag_' + idx,
            date:               x.date,
            site:               r.site || null,
            user_name:          r.user_name || null,
            user_folder:        folder,
            topic_id:           t.topic_id,
            topic_title:        t.topic_title,
            topic_category:     t.category,
            source:             'topic_flag',
            observation:        f.observation,
            risk_level:         f.risk_level,
            recommended_action: f.recommended_action || null,
            location:           null,
            who_raised:         null,
            status:             'open',  /* see _AUDIT-2 */
            related_photos:     (t.related_photos || []).slice(),
          });
        });
      });
    });

    return { rows: rows, from: from, to: to, user: user, dates: fanout.dates };
  }

  /* ─── Quality ────────────────────────────────────────────────────────── */

  async function getQualityRange(opts) {
    opts = opts || {};
    var from = opts.from, to = opts.to;
    if (!from || !to) return { rows: [], from: from, to: to };
    var user = resolveUser(opts.user);

    var fanout = await fanoutDates(from, to, user);
    if (fanout._accessDenied) {
      return { _accessDenied: true, error: fanout.error };
    }

    var rows = [];
    fanout.perDay.forEach(function (x) {
      var r = x.report;
      if (!r || r._notFound || r.available_users) return;
      var folder = r.user_name ? window.FS.api.folderName(r.user_name) : null;

      /* a) Report-level quality_and_compliance items. */
      (r.quality_and_compliance || []).forEach(function (q, idx) {
        rows.push({
          id:               x.date + '_qc_' + idx,
          date:             x.date,
          site:             r.site || null,
          user_name:        r.user_name || null,
          user_folder:      folder,
          topic_id:         -1,
          topic_title:      'Quality & Compliance',
          topic_category:   'quality',
          source:           'qc_item',
          item:             q.item,
          status:           q.status || 'unknown',
          details:          q.details || null,
          follow_up_needed: !!q.follow_up_needed,
          who_raised:       null,
        });
      });

      /* b) Topics tagged category === 'quality' — surface as a row each
         so the page covers both shapes. related_photos carried through
         for /quality right panel inline preview (Sprint 6.6.3). */
      (r.topics || []).forEach(function (t) {
        if (t.category !== 'quality') return;
        rows.push({
          id:               x.date + '_' + t.topic_id + '_topic',
          date:             x.date,
          site:             r.site || null,
          user_name:        r.user_name || null,
          user_folder:      folder,
          topic_id:         t.topic_id,
          topic_title:      t.topic_title,
          topic_category:   t.category,
          source:           'topic_quality',
          item:             t.topic_title,
          status:           'observed',
          details:          t.summary || null,
          follow_up_needed: false,
          who_raised:       (t.participants && t.participants[0]) || null,
          related_photos:   (t.related_photos || []).slice(),
        });
      });
    });

    return { rows: rows, from: from, to: to, user: user, dates: fanout.dates };
  }

  if (!window.FS) window.FS = {};
  if (!window.FS.api) window.FS.api = {};
  window.FS.api.compliance = {
    getSafetyRange:  getSafetyRange,
    getQualityRange: getQualityRange,
  };

})();
