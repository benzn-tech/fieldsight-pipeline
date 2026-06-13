/* ==========================================================================
   FieldSight API · Insights aggregator
   --------------------------------------------------------------------------
   Sprint 9 (Track A.1). Builds the rollup that the /insights dashboard
   renders.

   Reuses compliance-aggregator.getSafetyRange() / getQualityRange() to
   fetch flat per-row arrays for a date window, then enriches each row
   with:
     • subcontractor_id   — derived from who_raised via FS.insights
                            .resolveSubcontractor (Track A.0 lookup)
     • tags[]             — emitted by FS.insights.inferTagsFromText
                            (heuristic stand-in for backend tags[])

   Then computes three groupings the dashboard renders:
     • bySub     — sorted descending by row count, top-N
     • byTag     — sorted descending, top-N
     • byDay     — daily counts split by risk_level for sparkline

   Public API:
     window.FS.api.insights.getInsights({ from, to, kind })

       from, to: 'YYYY-MM-DD'
       kind:     'safety' | 'quality' | 'all'  (default 'all')

       returns: {
         safety: { rows, bySub, byTag, byDay, totals } | null,
         quality: { rows, bySub, byTag, byDay, totals } | null,
         range:  { from, to },
       }

   Honours admin fan-out via the upstream compliance-aggregator (Sprint 8
   follow-up — admin without explicit user gets `(date × all-users)`
   cross-product instead of the available_users disambiguation envelope).

   No new backend dependency — all derivation is client-side until
   PLAN §6 "Backend wiring for Sprint 9 schema" lands.
   ========================================================================== */

(function () {
  'use strict';

  /* ─── Enrichment helpers ─────────────────────────────────────────── */

  function enrichRow(row) {
    var ins = window.FS && window.FS.insights;
    if (!ins) return row;
    var subId = ins.resolveSubcontractor(row.who_raised || row.user_name);
    var tags  = ins.inferTagsFromText(
      row.observation || row.item || '',
      row.recommended_action || row.details || ''
    );
    return Object.assign({}, row, {
      subcontractor_id: subId,
      tags:             tags,
    });
  }

  /* ─── Grouping helpers ───────────────────────────────────────────── */

  /* Group by subcontractor_id; rows with null subId roll up under the
     synthetic 'unknown' bucket so PMs can see how much data is
     unattributed (signals fixture / backend gaps). */
  function groupBySubcontractor(rows) {
    var ins = window.FS && window.FS.insights;
    var buckets = {};
    rows.forEach(function (r) {
      var key = r.subcontractor_id || 'unknown';
      if (!buckets[key]) {
        var sub = (key !== 'unknown' && ins) ? ins.subcontractorById(key) : null;
        buckets[key] = {
          subcontractor_id: key,
          name:             sub ? sub.name  : 'Unattributed',
          trade:            sub ? sub.trade : null,
          count:            0,
          high:             0,
          medium:           0,
          low:              0,
          rows:             [],
        };
      }
      var b = buckets[key];
      b.count += 1;
      var risk = (r.risk_level || 'low').toLowerCase();
      if (risk === 'high')   b.high   += 1;
      else if (risk === 'medium') b.medium += 1;
      else                   b.low    += 1;
      b.rows.push(r);
    });
    return Object.keys(buckets).map(function (k) { return buckets[k]; })
      .sort(function (a, b) { return b.count - a.count; });
  }

  function groupByTag(rows) {
    var ins = window.FS && window.FS.insights;
    var buckets = {};
    rows.forEach(function (r) {
      (r.tags || []).forEach(function (slug) {
        if (!buckets[slug]) {
          buckets[slug] = {
            tag:              slug,
            label:            ins ? ins.tagLabel(slug) : slug,
            count:            0,
            top_subcontractor: null,
            sub_counts:       {},
            rows:             [],
          };
        }
        var b = buckets[slug];
        b.count += 1;
        b.rows.push(r);
        var subKey = r.subcontractor_id || 'unknown';
        b.sub_counts[subKey] = (b.sub_counts[subKey] || 0) + 1;
      });
    });
    /* Resolve each bucket's top subcontractor (the one most-often
       responsible for issues bearing this tag). */
    Object.keys(buckets).forEach(function (k) {
      var b = buckets[k];
      var topSub = null;
      var topN   = 0;
      Object.keys(b.sub_counts).forEach(function (subId) {
        if (b.sub_counts[subId] > topN) {
          topN = b.sub_counts[subId];
          topSub = subId;
        }
      });
      b.top_subcontractor = topSub;
    });
    return Object.keys(buckets).map(function (k) { return buckets[k]; })
      .sort(function (a, b) { return b.count - a.count; });
  }

  /* Bucket rows by date for sparkline rendering. Counts split by
     risk_level so the sparkline can stack (or render the high-risk
     band on top). */
  function groupByDay(rows, from, to) {
    /* Build a fully-zeroed day map from `from` → `to` so sparklines
       render flat segments on quiet days rather than gaps. */
    var byDay = {};
    var d = from;
    while (d <= to) {
      byDay[d] = { date: d, count: 0, high: 0, medium: 0, low: 0 };
      d = window.FS.api.addDaysISO(d, 1);
      if (!d) break;
    }
    rows.forEach(function (r) {
      var bucket = byDay[r.date];
      if (!bucket) {
        bucket = { date: r.date, count: 0, high: 0, medium: 0, low: 0 };
        byDay[r.date] = bucket;
      }
      bucket.count += 1;
      var risk = (r.risk_level || 'low').toLowerCase();
      if (risk === 'high')   bucket.high   += 1;
      else if (risk === 'medium') bucket.medium += 1;
      else                   bucket.low    += 1;
    });
    return Object.keys(byDay).sort().map(function (k) { return byDay[k]; });
  }

  function totalsFromRows(rows) {
    var subs  = {};
    var tags  = {};
    var risk  = { high: 0, medium: 0, low: 0 };
    rows.forEach(function (r) {
      if (r.subcontractor_id) subs[r.subcontractor_id] = true;
      (r.tags || []).forEach(function (t) { tags[t] = true; });
      var rk = (r.risk_level || 'low').toLowerCase();
      if (risk[rk] != null) risk[rk] += 1;
    });
    return {
      count:           rows.length,
      high:            risk.high,
      medium:          risk.medium,
      low:             risk.low,
      distinct_subs:   Object.keys(subs).length,
      distinct_tags:   Object.keys(tags).length,
    };
  }

  /* ─── Public API ─────────────────────────────────────────────────── */

  async function getInsights(opts) {
    opts = opts || {};
    var from = opts.from;
    var to   = opts.to;
    var kind = opts.kind || 'all';

    if (!from || !to) {
      return { safety: null, quality: null, range: { from: from, to: to } };
    }

    var compliance = window.FS && window.FS.api && window.FS.api.compliance;
    if (!compliance) {
      return {
        _error: 'compliance aggregator not loaded',
        safety: null, quality: null, range: { from: from, to: to },
      };
    }

    /* Fetch in parallel where requested. */
    var safetyP  = (kind === 'all' || kind === 'safety')
      ? compliance.getSafetyRange({ from: from, to: to }) : Promise.resolve(null);
    var qualityP = (kind === 'all' || kind === 'quality')
      ? compliance.getQualityRange({ from: from, to: to }) : Promise.resolve(null);

    var results = await Promise.all([safetyP, qualityP]);
    var safetyRes  = results[0];
    var qualityRes = results[1];

    function buildSection(res) {
      if (!res) return null;
      if (res._accessDenied) return { _accessDenied: true, error: res.error };
      var rows = (res.rows || []).map(enrichRow);
      return {
        rows:    rows,
        bySub:   groupBySubcontractor(rows),
        byTag:   groupByTag(rows),
        byDay:   groupByDay(rows, from, to),
        totals:  totalsFromRows(rows),
      };
    }

    return {
      safety:  buildSection(safetyRes),
      quality: buildSection(qualityRes),
      range:   { from: from, to: to },
    };
  }

  if (!window.FS)         window.FS = {};
  if (!window.FS.api)     window.FS.api = {};
  window.FS.api.insights = { getInsights: getInsights };

})();
