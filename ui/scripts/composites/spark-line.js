/* ==========================================================================
   FieldSight SparkLine — Layer 5 composite (Sprint 9 Track A.2)
   --------------------------------------------------------------------------
   14-day inline trend chart. Renders an SVG polyline + filled-under
   area + dot markers. Hover-on-day reveals tooltip via title-tag.

   Vanilla SVG; no library. Tone-aware so dark / light themes both
   read correctly without separate builds.

   Props:
     points       [{ date: 'YYYY-MM-DD', value: number, label?: string }]
     tone         'accent' | 'danger' | 'warning' | 'success' | 'info'
                  (defaults 'accent')
     width        px (default 280)
     height       px (default 56)
     showLastValue boolean — render the final value as text on the
                  right (default false)

   Exported to: window.FieldSight.SparkLine
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  /* Theme-aware chart fill — see tokens.css §"Chart fills". */
  function toneVar(tone) {
    switch (tone) {
      case 'danger':  return 'var(--fs-chart-danger)';
      case 'warning': return 'var(--fs-chart-warning)';
      case 'success': return 'var(--fs-chart-success)';
      case 'info':    return 'var(--fs-chart-info)';
      default:        return 'var(--fs-chart-accent)';
    }
  }

  function SparkLine(props) {
    var points = props.points || [];
    var tone   = props.tone   || 'accent';
    var width  = props.width  || 280;
    var height = props.height || 56;
    var showLast = !!props.showLastValue;

    if (points.length === 0) {
      return React.createElement('div', {
        className: 'fs-spark-line fs-spark-line--empty',
        style:     { width: width + 'px', height: height + 'px' },
      });
    }

    /* Use vertical padding so the top/bottom dots aren't clipped. */
    var padX = 4;
    var padY = 6;
    var w    = width  - padX * 2;
    var h    = height - padY * 2;

    var values = points.map(function (p) { return p.value || 0; });
    var maxV   = Math.max.apply(null, values);
    var minV   = Math.min.apply(null, values);
    if (maxV === minV) maxV = minV + 1; /* avoid div0 — render flat */

    var stepX = points.length > 1 ? w / (points.length - 1) : 0;

    function xy(i) {
      var p = points[i];
      var nx = padX + i * stepX;
      var ny = padY + h - ((p.value - minV) / (maxV - minV)) * h;
      return { x: nx, y: ny };
    }

    var coords = points.map(function (_, i) { return xy(i); });
    var pathD = coords.map(function (c, i) {
      return (i === 0 ? 'M' : 'L') + c.x.toFixed(1) + ' ' + c.y.toFixed(1);
    }).join(' ');

    /* Filled-under area: line path + close back to baseline. */
    var areaD = pathD
      + ' L' + coords[coords.length - 1].x.toFixed(1) + ' ' + (padY + h).toFixed(1)
      + ' L' + coords[0].x.toFixed(1)                  + ' ' + (padY + h).toFixed(1)
      + ' Z';

    var stroke = toneVar(tone);

    return React.createElement('div', {
      className: 'fs-spark-line',
      style:     { width: width + 'px', height: height + 'px' },
    },
      React.createElement('svg', {
        width: width, height: height,
        viewBox: '0 0 ' + width + ' ' + height,
        role: 'img',
        'aria-label': 'Trend over ' + points.length + ' days',
      },
        React.createElement('path', {
          d:    areaD,
          fill: stroke,
          opacity: 0.15,
        }),
        React.createElement('path', {
          d:           pathD,
          fill:        'none',
          stroke:      stroke,
          strokeWidth: 1.5,
          strokeLinecap: 'round',
          strokeLinejoin: 'round',
        }),
        coords.map(function (c, i) {
          var p = points[i];
          return React.createElement('circle', {
            key: i,
            cx: c.x, cy: c.y, r: 2,
            fill: stroke,
          },
            React.createElement('title', null,
              (p.label || p.date) + ': ' + (p.value || 0)),
          );
        }),
      ),
      showLast ? React.createElement('span', {
        className: 'fs-spark-line__last',
        style:     { color: stroke },
      }, (values[values.length - 1] || 0).toLocaleString()) : null,
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.SparkLine = SparkLine;

})();
