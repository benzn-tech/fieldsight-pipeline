/* ==========================================================================
   FieldSight OverAllocationBanner — Sprint 8.3.2
   --------------------------------------------------------------------------
   Renders above the Gantt whenever detectOverAllocations() finds double-
   booked assignees. Dismissible per session (re-appears after next
   mutation that creates new conflicts via a fresh `overAllocationMap` prop).

   Props:
     overAllocationMap   { [userId]: dateISO[] }  — from programme-schedule
     onDismiss           () => void

   Visibility is gated in the parent (programme.js) on programme:manage.

   Exported to: window.FieldSight.OverAllocationBanner
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  /* Format a run of consecutive ISO dates as "3–5 May" style range. */
  function formatDateRanges(dates) {
    if (!dates || !dates.length) return '';
    var sorted = dates.slice().sort();
    var ranges = [];
    var rangeStart = sorted[0];
    var rangeEnd   = sorted[0];

    function pushRange() {
      var s = new Date(rangeStart + 'T00:00:00Z');
      var e = new Date(rangeEnd   + 'T00:00:00Z');
      var months = ['Jan','Feb','Mar','Apr','May','Jun',
                    'Jul','Aug','Sep','Oct','Nov','Dec'];
      if (s.getTime() === e.getTime()) {
        ranges.push(s.getUTCDate() + ' ' + months[s.getUTCMonth()]);
      } else if (s.getUTCMonth() === e.getUTCMonth()) {
        ranges.push(s.getUTCDate() + '–' + e.getUTCDate() + ' ' + months[s.getUTCMonth()]);
      } else {
        ranges.push(
          s.getUTCDate() + ' ' + months[s.getUTCMonth()] + ' – ' +
          e.getUTCDate() + ' ' + months[e.getUTCMonth()]
        );
      }
    }

    for (var i = 1; i < sorted.length; i++) {
      var prev = new Date(sorted[i - 1] + 'T00:00:00Z');
      var cur  = new Date(sorted[i]     + 'T00:00:00Z');
      var gap  = Math.round((cur.getTime() - prev.getTime()) / 86400000);
      if (gap === 1) {
        rangeEnd = sorted[i];
      } else {
        pushRange();
        rangeStart = sorted[i];
        rangeEnd   = sorted[i];
      }
    }
    pushRange();
    return ranges.join(', ');
  }

  function formatUserId(userId) {
    /* folder_name → "Folder Name" */
    return (userId || '').replace(/_/g, ' ').replace(/\b\w/g, function (c) {
      return c.toUpperCase();
    });
  }

  function OverAllocationBanner(props) {
    var map       = props.overAllocationMap || {};
    var onDismiss = props.onDismiss || function () {};

    var users = Object.keys(map);
    if (!users.length) return null;

    return React.createElement('div', {
      className:   'fs-over-allocation-banner',
      role:        'alert',
      'aria-live': 'polite',
    },
      React.createElement('div', { className: 'fs-over-allocation-banner__icon' }, '⚠'),
      React.createElement('div', { className: 'fs-over-allocation-banner__body' },
        React.createElement('strong', { className: 'fs-over-allocation-banner__title' },
          'Over-allocation detected'),
        React.createElement('ul', { className: 'fs-over-allocation-banner__list' },
          users.map(function (userId) {
            return React.createElement('li', { key: userId },
              React.createElement('strong', null, formatUserId(userId)),
              ' is double-booked on ',
              formatDateRanges(map[userId])
            );
          })
        ),
      ),
      React.createElement('button', {
        type:       'button',
        className:  'fs-over-allocation-banner__dismiss',
        onClick:    onDismiss,
        'aria-label': 'Dismiss over-allocation warning',
      }, '×'),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.OverAllocationBanner = OverAllocationBanner;

}());
