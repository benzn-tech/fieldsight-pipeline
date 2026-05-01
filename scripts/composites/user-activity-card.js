/* ==========================================================================
   FieldSight UserActivityCard — Layer 5 composite (Sprint 4.6)
   --------------------------------------------------------------------------
   One row in the redesigned /activity page. Header (avatar + name +
   role + primary_site) + 4 count chips + up to 3 most-recent event
   preview rows. Click → page selects this user and the right pane
   shows the full event timeline.

   Counts:
     Topics  · participations across the time window
     Actions · action items owned (any status)
     Photos  · related_photos contributed (own reports)
     Safety  · safety_flags raised (own reports)

   Event preview rows (top 3 by recency):
     [HH:MM] [kind icon]  Topic title — short snippet

   Props:
     user         { user_name, user_folder, role, primary_site,
                    counts: { topics, actions, photos, safety_flags },
                    events: [...] }
     selected     boolean
     onSelect     (user) => void

   Exported to:
     window.FieldSight.UserActivityCard
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  var KIND_ICON = {
    topic:  '◇',
    action: '✓',
    photo:  '▤',
    safety: '⚠',
  };

  function fmtDate(yyyymmdd) {
    if (!yyyymmdd) return '';
    var p = String(yyyymmdd).split('-').map(Number);
    if (p.length !== 3) return yyyymmdd;
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return p[2] + ' ' + months[p[1] - 1];
  }

  function UserActivityCard(props) {
    var Card    = window.FieldSight.Card;
    var Avatar  = window.FieldSight.Avatar;
    var Badge   = window.FieldSight.Badge;

    var u        = props.user || {};
    var counts   = u.counts || { topics: 0, actions: 0, photos: 0, safety_flags: 0 };
    var preview  = (u.events || []).slice(0, 3);
    var selected = !!props.selected;
    var onSelect = props.onSelect;

    var totalEvents = counts.topics + counts.actions + counts.photos + counts.safety_flags;

    var className = 'fs-user-activity-card'
      + (selected ? ' fs-user-activity-card--selected' : '')
      + (totalEvents === 0 ? ' fs-user-activity-card--quiet' : '');

    return React.createElement(Card, {
      padding:   'sm',
      className: className,
      onClick:   onSelect ? function () { onSelect(u); } : undefined,
    },
      React.createElement(Card.Body, null,

        /* Header row */
        React.createElement('div', { className: 'fs-user-activity-card__header' },
          React.createElement(Avatar, { name: u.user_name || '—', size: 'md' }),
          React.createElement('div', { className: 'fs-user-activity-card__id' },
            React.createElement('div', { className: 'fs-user-activity-card__name' },
              u.user_name || '—'),
            React.createElement('div', { className: 'fs-user-activity-card__sub' },
              [u.role, u.primary_site].filter(Boolean).join(' · ')),
          ),
          React.createElement('div', { className: 'fs-user-activity-card__chev' }, '›'),
        ),

        /* Counts strip */
        React.createElement('div', { className: 'fs-user-activity-card__counts' },
          React.createElement(CountChip, { label: 'Topics',  value: counts.topics  }),
          React.createElement(CountChip, { label: 'Actions', value: counts.actions }),
          React.createElement(CountChip, { label: 'Photos',  value: counts.photos  }),
          React.createElement(CountChip, {
            label: 'Safety',
            value: counts.safety_flags,
            tone:  counts.safety_flags > 0 ? 'danger' : 'neutral',
          }),
        ),

        /* Recent-events preview */
        totalEvents === 0
          ? React.createElement('div', { className: 'fs-user-activity-card__empty' },
              'No activity in this window.')
          : React.createElement('div', { className: 'fs-user-activity-card__preview' },
              preview.map(function (ev, i) {
                return React.createElement('div', {
                  key:       i,
                  className: 'fs-user-activity-card__preview-row'
                              + ' fs-user-activity-card__preview-row--' + ev.kind,
                },
                  React.createElement('span', {
                    className: 'fs-user-activity-card__preview-icon',
                    title:     ev.kind,
                  }, KIND_ICON[ev.kind] || '·'),
                  React.createElement('span', { className: 'fs-user-activity-card__preview-when' },
                    fmtDate(ev.date) + (ev.time_label ? ' ' + ev.time_label : '')),
                  React.createElement('span', { className: 'fs-user-activity-card__preview-text' },
                    ev.summary || ev.topic_title),
                );
              }),
              totalEvents > preview.length
                ? React.createElement('div', { className: 'fs-user-activity-card__preview-more' },
                    '+' + (totalEvents - preview.length) + ' more')
                : null,
            ),
      ),
    );
  }

  function CountChip(props) {
    var className = 'fs-user-activity-card__count'
      + (props.tone === 'danger' && props.value > 0
          ? ' fs-user-activity-card__count--danger' : '');
    return React.createElement('div', { className: className },
      React.createElement('span', { className: 'fs-user-activity-card__count-value' },
        props.value),
      React.createElement('span', { className: 'fs-user-activity-card__count-label' },
        props.label),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.UserActivityCard = UserActivityCard;
})();
