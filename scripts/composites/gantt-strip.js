/* ==========================================================================
   FieldSight GanttStrip — Layer 5 composite (Sprint 4.4)
   --------------------------------------------------------------------------
   Date strip header at the top of the Gantt timeline. Three tiers:
     day   — every date (compact, scrolls horizontally)
     week  — Monday markers only ("Mon 4 May")
     month — first-of-month markers ("May 2026")

   Width = (totalDays × pixelsPerDay). Today's marker rendered as a
   thin accent line via an absolutely-positioned element (rendered
   by the page, not this composite — it just provides the strip).

   Props:
     from           ISO date — programme start
     to             ISO date — programme end
     pixelsPerDay   number   — set by the page (24 / 6 / 2 typical)
     tier           'day' | 'week' | 'month'

   Exported to:
     window.FieldSight.GanttStrip
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function dateRangeISO(from, to) {
    var dates = [];
    var c = from;
    while (c <= to) {
      dates.push(c);
      c = window.FS.api.addDaysISO(c, 1);
    }
    return dates;
  }

  function formatDay(d) {
    var p = d.split('-').map(Number);
    return String(p[2]);
  }
  function formatWeek(d) {
    var p = d.split('-').map(Number);
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return p[2] + ' ' + months[p[1] - 1];
  }
  function formatMonth(d) {
    var p = d.split('-').map(Number);
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return months[p[1] - 1] + ' ' + p[0];
  }

  function isMonday(iso) {
    var p = iso.split('-').map(Number);
    var d = new Date(Date.UTC(p[0], p[1] - 1, p[2]));
    return d.getUTCDay() === 1;
  }
  function isFirstOfMonth(iso) {
    return iso.endsWith('-01');
  }

  function GanttStrip(props) {
    var from = props.from;
    var to   = props.to;
    var ppd  = props.pixelsPerDay || 24;
    var tier = props.tier || 'day';

    var dates = dateRangeISO(from, to);
    var totalWidth = dates.length * ppd;

    var markers = [];
    if (tier === 'day') {
      dates.forEach(function (d, i) {
        markers.push({ iso: d, label: formatDay(d), x: i * ppd });
      });
    } else if (tier === 'week') {
      dates.forEach(function (d, i) {
        if (isMonday(d) || i === 0) {
          markers.push({ iso: d, label: formatWeek(d), x: i * ppd });
        }
      });
    } else {
      /* month */
      dates.forEach(function (d, i) {
        if (isFirstOfMonth(d) || i === 0) {
          markers.push({ iso: d, label: formatMonth(d), x: i * ppd });
        }
      });
    }

    return React.createElement('div', {
      className: 'fs-gantt-strip',
      style:     { width: totalWidth + 'px' },
    },
      markers.map(function (m, i) {
        return React.createElement('div', {
          key:       m.iso + '_' + i,
          className: 'fs-gantt-strip__marker'
                     + (tier !== 'day' ? ' fs-gantt-strip__marker--label' : ''),
          style:     { left: m.x + 'px' },
        }, m.label);
      }),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.GanttStrip = GanttStrip;
})();
