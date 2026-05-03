/* ==========================================================================
   FieldSight DatePicker — Layer 5 composite (Sprint 2.5 / PLAN Phase E)
   --------------------------------------------------------------------------
   Heat-mapped date picker for the Timeline page. Two surfaces:

     [Strip]   compact 7-day window centred on the selected date, each
               cell shows a topic-density dot and a safety-flag dot.
               Click to navigate.

     [Modal]   full-month grid behind a popover, opened from the
               "More dates" button. Same per-day glyphs, plus headings.
               Close on outside click, Escape, or after navigation.

   Heat-map intensity buckets (BACKEND-CONTEXT §4.3):
       0          → no report
       1–5 topics → low
       6–10       → medium
       11+        → high
       safety > 0 → orange dot overlay

   All date math runs through FS.api.addDaysISO (UTC arithmetic) to dodge
   BUG-19 (NZDT off-by-one when calling new Date('YYYY-MM-DD').toISO()).

   Props:
     date        'YYYY-MM-DD' selected date
     onChange    (yyyymmdd) => void
     monthsRange optional months parameter for /api/dates (default 3)
     site        optional site filter for /api/dates

   Exported to:
     window.FieldSight.DatePicker
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  var DAY_NAMES_SHORT = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  var DAY_NAMES_HDR   = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun']; /* NZ-style header */
  var MONTH_NAMES     = ['January','February','March','April','May','June',
                         'July','August','September','October','November','December'];
  var MONTH_SHORT     = ['Jan','Feb','Mar','Apr','May','Jun',
                         'Jul','Aug','Sep','Oct','Nov','Dec'];

  function parseISO(yyyymmdd) {
    if (!yyyymmdd) return null;
    var p = yyyymmdd.split('-').map(Number);
    return new Date(Date.UTC(p[0], p[1] - 1, p[2]));
  }

  function toISO(d) {
    return d.toISOString().slice(0, 10);
  }

  function shift(yyyymmdd, days) {
    return window.FS.api.addDaysISO(yyyymmdd, days);
  }

  function intensity(meta) {
    if (!meta || !meta.hasReport) return 0;
    var t = meta.topics || 0;
    if (t >= 11) return 3;
    if (t >= 6)  return 2;
    if (t >= 1)  return 1;
    return 0;
  }

  /* Day-cell glyph used by both surfaces. */
  function DayCell(props) {
    var iso  = props.iso;
    var meta = props.meta || null;
    var label = props.label;          /* e.g. '29' for grid, 'Wed 29' for strip */
    var caption = props.caption;       /* e.g. 'Apr' under strip cells */
    var selected = props.selected;
    var muted    = props.muted;        /* for prev/next-month bleed in grid */
    var disabled = props.disabled;     /* future dates if you want to lock — not used yet */

    var i  = intensity(meta);
    var hasSafety = meta && meta.hasReport && (meta.safety || 0) > 0;

    var className = 'fs-date-picker__cell'
      + ' fs-date-picker__cell--i' + i
      + (selected ? ' fs-date-picker__cell--selected' : '')
      + (muted    ? ' fs-date-picker__cell--muted'    : '')
      + (props.variant === 'strip' ? ' fs-date-picker__cell--strip' : ' fs-date-picker__cell--grid');

    return React.createElement('button', {
      type:      'button',
      className: className,
      disabled:  disabled,
      onClick:   function () { if (props.onSelect) props.onSelect(iso); },
      'aria-label': iso + (meta && meta.hasReport
        ? ', ' + meta.topics + ' topics' + (hasSafety ? ', ' + meta.safety + ' safety' : '')
        : ', no report'),
      'aria-pressed': selected,
    },
      React.createElement('span', { className: 'fs-date-picker__cell-label' }, label),
      caption ? React.createElement('span', {
        className: 'fs-date-picker__cell-caption',
      }, caption) : null,
      React.createElement('span', { className: 'fs-date-picker__cell-dots' },
        i > 0 ? React.createElement('span', {
          className: 'fs-date-picker__cell-dot fs-date-picker__cell-dot--i' + i,
        }) : null,
        hasSafety ? React.createElement('span', {
          className: 'fs-date-picker__cell-dot fs-date-picker__cell-dot--safety',
        }) : null,
      ),
    );
  }

  /* ---------- Compact 7-day strip ---------------------------------------- */
  function DateStrip(props) {
    var date  = props.date;
    var dates = props.dates || {};
    var d = parseISO(date);
    if (!d) return null;

    var cells = [];
    /* Show -3..+3 days. */
    for (var off = -3; off <= 3; off++) {
      var iso = shift(date, off);
      var meta = dates[iso];
      var dt = parseISO(iso);
      var dayName = DAY_NAMES_SHORT[dt.getUTCDay()];
      var dayNum  = dt.getUTCDate();

      cells.push(React.createElement(DayCell, {
        key:      iso,
        iso:      iso,
        meta:     meta,
        label:    dayName + ' ' + dayNum,
        caption:  MONTH_SHORT[dt.getUTCMonth()],
        selected: iso === date,
        variant:  'strip',
        onSelect: props.onChange,
      }));
    }

    return React.createElement('div', { className: 'fs-date-picker__strip' },
      React.createElement('button', {
        type:      'button',
        className: 'fs-date-picker__nudge',
        'aria-label': 'Previous day',
        onClick:   function () { props.onChange(shift(date, -1)); },
      }, '‹'),
      React.createElement('div', { className: 'fs-date-picker__strip-cells' },
        cells,
      ),
      React.createElement('button', {
        type:      'button',
        className: 'fs-date-picker__nudge',
        'aria-label': 'Next day',
        onClick:   function () { props.onChange(shift(date, 1)); },
      }, '›'),
    );
  }

  /* ---------- Month grid (modal body) ----------------------------------- */
  function MonthGrid(props) {
    var date  = props.date;
    var month = props.month;          /* { year, month0 } visible month */
    var dates = props.dates || {};

    /* First day of the visible month, shifted to a Monday-start week. */
    var firstOfMonth = new Date(Date.UTC(month.year, month.month0, 1));
    var dayIdxMonStart = (firstOfMonth.getUTCDay() + 6) % 7; /* Mon=0..Sun=6 */
    var gridStart = new Date(Date.UTC(month.year, month.month0, 1 - dayIdxMonStart));

    var cells = [];
    for (var i = 0; i < 42; i++) {
      var d = new Date(gridStart);
      d.setUTCDate(gridStart.getUTCDate() + i);
      var iso = toISO(d);
      var inMonth = d.getUTCMonth() === month.month0;
      var meta = dates[iso];

      cells.push(React.createElement(DayCell, {
        key:      iso,
        iso:      iso,
        meta:     meta,
        label:    String(d.getUTCDate()),
        selected: iso === date,
        muted:    !inMonth,
        variant:  'grid',
        onSelect: props.onSelect,
      }));
    }

    return React.createElement('div', { className: 'fs-date-picker__grid' },
      DAY_NAMES_HDR.map(function (n) {
        return React.createElement('div', {
          key: n, className: 'fs-date-picker__grid-hdr',
        }, n);
      }),
      cells,
    );
  }

  /* ---------- DatePicker root ------------------------------------------- */
  function DatePicker(props) {
    var date = props.date;
    var monthsRange = props.monthsRange || 3;
    var site = props.site || null;

    var refDates = React.useState({ status: 'loading', map: {} });
    var datesS    = refDates[0];
    var setDatesS = refDates[1];

    var refOpen = React.useState(false);
    var open    = refOpen[0];
    var setOpen = refOpen[1];

    /* Visible month for the modal — driven by selected date but
       independent so prev/next nav works inside the modal. */
    var initial = parseISO(date) || new Date();
    var refMonth = React.useState({
      year: initial.getUTCFullYear(), month0: initial.getUTCMonth(),
    });
    var month    = refMonth[0];
    var setMonth = refMonth[1];

    React.useEffect(function () {
      var cancelled = false;
      window.FS.api.dates.getDates({ months: monthsRange, site: site }).then(function (res) {
        if (cancelled) return;
        setDatesS({ status: 'ok', map: res.dates || {} });
      }).catch(function () {
        if (cancelled) return;
        setDatesS({ status: 'error', map: {} });
      });
      return function () { cancelled = true; };
    }, [monthsRange, site]);

    /* When selected date changes externally, follow it in the modal. */
    React.useEffect(function () {
      var d = parseISO(date);
      if (!d) return;
      setMonth({ year: d.getUTCFullYear(), month0: d.getUTCMonth() });
    }, [date]);

    React.useEffect(function () {
      if (!open) return;
      function onKey(e) { if (e.key === 'Escape') setOpen(false); }
      document.addEventListener('keydown', onKey);
      return function () { document.removeEventListener('keydown', onKey); };
    }, [open]);

    function onSelect(iso) {
      props.onChange(iso);
      setOpen(false);
    }

    function shiftMonth(delta) {
      setMonth(function (m) {
        var d = new Date(Date.UTC(m.year, m.month0 + delta, 1));
        return { year: d.getUTCFullYear(), month0: d.getUTCMonth() };
      });
    }

    /* Inline mode (Sprint 6.6.1) — skips the 7-day strip and renders
       just the month grid + month-nav header inline, not as a modal.
       Used by /safety and /quality where the goal is "browse and pick"
       (commit only on cell click). The default mode keeps timeline.js's
       "step-through" UX (strip arrows shift AND commit by ±1 day). */
    if (props.inline) {
      return React.createElement('div', {
        className: 'fs-date-picker fs-date-picker--inline',
      },
        React.createElement('div', { className: 'fs-date-picker__inline-header' },
          React.createElement('button', {
            type: 'button', onClick: function () { shiftMonth(-1); },
            className: 'fs-date-picker__month-nav',
            'aria-label': 'Previous month',
          }, '‹'),
          React.createElement('div', { className: 'fs-date-picker__month-label' },
            MONTH_NAMES[month.month0] + ' ' + month.year),
          React.createElement('button', {
            type: 'button', onClick: function () { shiftMonth(1); },
            className: 'fs-date-picker__month-nav',
            'aria-label': 'Next month',
          }, '›'),
        ),
        React.createElement(MonthGrid, {
          date:     date,
          month:    month,
          dates:    datesS.map,
          onSelect: function (iso) { props.onChange(iso); },
        }),
        React.createElement('div', { className: 'fs-date-picker__legend' },
          React.createElement('span', null,
            React.createElement('span', { className: 'fs-date-picker__cell-dot fs-date-picker__cell-dot--i1' }), ' light'),
          React.createElement('span', null,
            React.createElement('span', { className: 'fs-date-picker__cell-dot fs-date-picker__cell-dot--i2' }), ' busy'),
          React.createElement('span', null,
            React.createElement('span', { className: 'fs-date-picker__cell-dot fs-date-picker__cell-dot--i3' }), ' heavy'),
          React.createElement('span', null,
            React.createElement('span', { className: 'fs-date-picker__cell-dot fs-date-picker__cell-dot--safety' }), ' safety'),
        ),
      );
    }

    return React.createElement('div', { className: 'fs-date-picker' },
      React.createElement(DateStrip, {
        date:  date,
        dates: datesS.map,
        onChange: props.onChange,
      }),

      React.createElement('button', {
        type:      'button',
        className: 'fs-date-picker__more',
        onClick:   function () { setOpen(function (o) { return !o; }); },
        'aria-expanded': open,
      }, open ? 'Close' : 'Browse month'),

      open ? React.createElement('div', {
        className: 'fs-date-picker__modal',
        role:      'dialog',
        'aria-label': 'Pick a date',
      },
        React.createElement('div', { className: 'fs-date-picker__modal-overlay',
          onClick: function () { setOpen(false); },
        }),
        React.createElement('div', { className: 'fs-date-picker__modal-card' },
          React.createElement('div', { className: 'fs-date-picker__modal-header' },
            React.createElement('button', {
              type: 'button', onClick: function () { shiftMonth(-1); },
              className: 'fs-date-picker__month-nav',
              'aria-label': 'Previous month',
            }, '‹'),
            React.createElement('div', { className: 'fs-date-picker__month-label' },
              MONTH_NAMES[month.month0] + ' ' + month.year),
            React.createElement('button', {
              type: 'button', onClick: function () { shiftMonth(1); },
              className: 'fs-date-picker__month-nav',
              'aria-label': 'Next month',
            }, '›'),
          ),
          React.createElement(MonthGrid, {
            date:     date,
            month:    month,
            dates:    datesS.map,
            onSelect: onSelect,
          }),
          React.createElement('div', { className: 'fs-date-picker__legend' },
            React.createElement('span', null,
              React.createElement('span', { className: 'fs-date-picker__cell-dot fs-date-picker__cell-dot--i1' }), ' light'),
            React.createElement('span', null,
              React.createElement('span', { className: 'fs-date-picker__cell-dot fs-date-picker__cell-dot--i2' }), ' busy'),
            React.createElement('span', null,
              React.createElement('span', { className: 'fs-date-picker__cell-dot fs-date-picker__cell-dot--i3' }), ' heavy'),
            React.createElement('span', null,
              React.createElement('span', { className: 'fs-date-picker__cell-dot fs-date-picker__cell-dot--safety' }), ' safety'),
          ),
        ),
      ) : null,
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.DatePicker = DatePicker;
})();
