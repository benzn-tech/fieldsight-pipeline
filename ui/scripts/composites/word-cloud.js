/* ==========================================================================
   FieldSight WordCloud — Layer 5 composite (Sprint 9.5.3)
   --------------------------------------------------------------------------
   Frequency-sized tag cloud. Each tag is rendered as an inline-flex
   <span> with its font-size linearly mapped from data[].count to a
   range between minFontPx and maxFontPx. Used by /insights to make
   the 12-tag vocabulary scannable at a glance — "the biggest words
   are what's tripping us up this week".

   No SVG, no canvas, no library — vanilla HTML + inline style. Auto
   re-flows to fill the available canvas width. Click-to-filter.

   Sprint 9.5.6 update: rendered tags now use the page's normal
   text colour (not the per-tag colour) — frequency is communicated
   by font-size + the sup-script count number alone, which reads
   calmer on dashboards than 12 separate hues. The `color` prop
   on each tag is still accepted but ignored by render; downstream
   composites (HeatmapGrid) continue to use it.

   Props:
     data       [{ slug, label, count, color?, tone? }]
                  count: number — drives font-size
                  color/tone: now ignored visually (kept on shape
                              for compatibility with TAG_VOCAB)
     onSelect   (slug) => void  — click handler per tag
     selected   string — slug currently filtering (renders bold +
                  underlined for selected; opacity-faded for others)
     maxFontPx  default 32
     minFontPx  default 12
     emptyText  string — fallback when data is empty

   Exported to: window.FieldSight.WordCloud
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function WordCloud(props) {
    var data       = props.data || [];
    var onSelect   = props.onSelect;
    var selected   = props.selected || null;
    var maxFontPx  = props.maxFontPx || 32;
    var minFontPx  = props.minFontPx || 12;
    var emptyText  = props.emptyText || 'No tagged issues in this range.';

    if (data.length === 0) {
      return React.createElement('div', { className: 'fs-word-cloud__empty' },
        emptyText);
    }

    /* Linear-scale font-size by count. Single-tag edge case: render
       at midpoint to avoid div0. */
    var maxCount = data.reduce(function (m, t) {
      return t.count > m ? t.count : m;
    }, 0);
    var minCount = data.reduce(function (m, t) {
      return t.count < m ? t.count : m;
    }, maxCount);
    var span = (maxCount - minCount) || 1;

    function fontFor(count) {
      var ratio = (count - minCount) / span;
      return Math.round(minFontPx + ratio * (maxFontPx - minFontPx));
    }

    /* Sort by count descending so the eye lands on the biggest first;
       within same count, alpha by label for stability. */
    var sorted = data.slice().sort(function (a, b) {
      if (a.count !== b.count) return b.count - a.count;
      return (a.label || '').localeCompare(b.label || '');
    });

    return React.createElement('ul', {
      className: 'fs-word-cloud',
      role:      'list',
      'aria-label': 'Tag frequency cloud',
    },
      sorted.map(function (tag) {
        var isSelected = selected === tag.slug;
        var anySelected = selected !== null;
        var clickable = !!onSelect;
        return React.createElement('li', {
          key:       tag.slug,
          className: 'fs-word-cloud__item'
            + (clickable ? ' fs-word-cloud__item--clickable' : '')
            + (isSelected ? ' fs-word-cloud__item--selected' : '')
            + (anySelected && !isSelected ? ' fs-word-cloud__item--faded' : ''),
          style: {
            /* Sprint 9.5.6 — only font-size is per-tag; colour is
               inherited from .fs-word-cloud (var(--text-primary))
               so the cloud reads as a calm size-encoded list. */
            fontSize: fontFor(tag.count) + 'px',
          },
          onClick:   clickable ? function () { onSelect(tag.slug); } : undefined,
          tabIndex:  clickable ? 0 : undefined,
          role:      clickable ? 'button' : undefined,
          onKeyDown: clickable ? function (e) {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              onSelect(tag.slug);
            }
          } : undefined,
          title:     tag.label + ' · ' + tag.count + ' issue' + (tag.count === 1 ? '' : 's'),
        },
          tag.label,
          React.createElement('sup', { className: 'fs-word-cloud__count' }, tag.count),
        );
      }),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.WordCloud = WordCloud;

})();
