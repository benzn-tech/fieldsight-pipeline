/* ==========================================================================
   FieldSight TopicCard — Layer 5 composite
   --------------------------------------------------------------------------
   Renders one element of report.topics[] (BACKEND-CONTEXT §5.1):
     header    : time_range · topic_title · CategoryBadge
     metaline  : participants (chip strip) + photo count
     summary   : 2–4 sentence summary
     sections  : key_decisions / action_items (with toggle) / safety_flags

   Time range uses an EN-DASH (U+2013) per backend contract. Participants
   may include device IDs ("Benl1") when names couldn't be resolved
   (BACKEND-CONTEXT §5.1 / §8.6).

   The card is collapsible — header is always visible, the body lives
   under an `expanded` flag. Selecting the header (anywhere outside the
   expand chevron) fires onSelect for the right-detail panel.

   Props:
     topic         DailyReport.topics[i]
     date          'YYYY-MM-DD' — needed for action toggle key
     actionState   { '<topic_id>_<action_index>': { checked, checked_by, checked_at } }
     defaultOpen   boolean
     onSelect      (topic) => void  — open in right detail
     selected      boolean — applies a selected style to the card

   Exported to:
     window.FieldSight.TopicCard
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function pluralise(n, sing) {
    return n + ' ' + sing + (n === 1 ? '' : 's');
  }

  function TopicCard(props) {
    var Card          = window.FieldSight.Card;
    var Badge         = window.FieldSight.Badge;
    var CategoryBadge = window.FieldSight.CategoryBadge;
    var ActionItemRow = window.FieldSight.ActionItemRow;
    var SafetyFlagRow = window.FieldSight.SafetyFlagRow;
    var NavIcon       = window.FieldSight.NavIcon;

    var topic       = props.topic || {};
    var actionState = props.actionState || {};
    var date        = props.date;

    var ref = React.useState(!!props.defaultOpen);
    var open    = ref[0];
    var setOpen = ref[1];

    function toggle(e) {
      if (e) e.stopPropagation();
      setOpen(function (o) { return !o; });
    }

    function onHeaderClick() {
      if (props.onSelect) props.onSelect(topic);
    }

    var participants = topic.participants || [];
    var decisions    = topic.key_decisions || [];
    var actions      = topic.action_items  || [];
    var flags        = topic.safety_flags  || [];
    var photos       = topic.related_photos || [];

    /* Quick counts to surface in collapsed mode. */
    var counts = [];
    if (decisions.length) counts.push(pluralise(decisions.length, 'decision'));
    if (actions.length)   counts.push(pluralise(actions.length,   'action'));
    if (flags.length)     counts.push(pluralise(flags.length,     'safety flag'));
    if (photos.length)    counts.push(pluralise(photos.length,    'photo'));

    var className = 'fs-topic-card'
      + (open ? ' fs-topic-card--open' : '')
      + (props.selected ? ' fs-topic-card--selected' : '');

    return React.createElement(Card, {
      padding:   'none',
      className: className,
    },

      /* Header — clickable everywhere except the chevron */
      React.createElement('div', {
        className: 'fs-topic-card__header',
        onClick:   onHeaderClick,
        role:      'button',
        tabIndex:  0,
        onKeyDown: function (e) {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            onHeaderClick();
          }
        },
      },
        React.createElement('div', { className: 'fs-topic-card__time' },
          topic.time_range || '—'),

        React.createElement('div', { className: 'fs-topic-card__title-wrap' },
          React.createElement('div', { className: 'fs-topic-card__title' },
            topic.topic_title || 'Untitled topic'),
          React.createElement('div', { className: 'fs-topic-card__metaline' },
            CategoryBadge ? React.createElement(CategoryBadge, {
              category: topic.category,
            }) : null,
            participants.length
              ? React.createElement('span', {
                  className: 'fs-topic-card__participants',
                }, participants.join(' · '))
              : null,
          ),
        ),

        React.createElement('button', {
          type:        'button',
          onClick:     toggle,
          className:   'fs-topic-card__chev',
          'aria-label': open ? 'Collapse topic' : 'Expand topic',
          'aria-expanded': open,
        },
          NavIcon ? React.createElement(NavIcon, {
            name: open ? 'chevron-up' : 'chevron-down',
            size: 16,
          }) : (open ? '▴' : '▾'),
        ),
      ),

      /* Collapsed-mode quick counts */
      !open && counts.length > 0
        ? React.createElement('div', { className: 'fs-topic-card__counts' },
            counts.join(' · '))
        : null,

      /* Body */
      open ? React.createElement('div', { className: 'fs-topic-card__body' },

        topic.summary
          ? React.createElement('p', { className: 'fs-topic-card__summary' },
              topic.summary)
          : null,

        decisions.length > 0
          ? React.createElement('div', { className: 'fs-topic-card__section' },
              React.createElement('div', { className: 'fs-topic-card__section-label' },
                'Key decisions'),
              React.createElement('ul', { className: 'fs-topic-card__decisions' },
                decisions.map(function (d, i) {
                  return React.createElement('li', {
                    key: i, className: 'fs-topic-card__decision',
                  }, d);
                }),
              ),
            )
          : null,

        actions.length > 0
          ? React.createElement('div', { className: 'fs-topic-card__section' },
              React.createElement('div', { className: 'fs-topic-card__section-label' },
                'Action items'),
              React.createElement('div', { className: 'fs-topic-card__actions' },
                actions.map(function (a, idx) {
                  var key = topic.topic_id + '_' + idx;
                  var state = actionState[key] || {};
                  return React.createElement(ActionItemRow, {
                    key:            key,
                    date:           date,
                    topicId:        topic.topic_id,
                    actionIndex:    idx,
                    action:         a,
                    initialChecked: !!state.checked,
                    checkedBy:      state.checked_by,
                    checkedAt:      state.checked_at,
                  });
                }),
              ),
            )
          : null,

        flags.length > 0
          ? React.createElement('div', { className: 'fs-topic-card__section' },
              React.createElement('div', {
                className: 'fs-topic-card__section-label fs-topic-card__section-label--danger',
              }, 'Safety flags'),
              React.createElement('div', { className: 'fs-topic-card__flags' },
                flags.map(function (f, i) {
                  return React.createElement(SafetyFlagRow, {
                    key: i, flag: f, dense: true,
                  });
                }),
              ),
            )
          : null,

        photos.length > 0
          ? React.createElement('div', { className: 'fs-topic-card__section' },
              React.createElement('div', { className: 'fs-topic-card__section-label' },
                'Photos'),
              React.createElement('div', { className: 'fs-topic-card__photos' },
                React.createElement(Badge, {
                  tone: 'neutral', size: 'sm', variant: 'subtle',
                  leftIcon: 'image',
                }, pluralise(photos.length, 'photo') + ' · open detail to view'),
              ),
            )
          : null,

      ) : null,
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.TopicCard = TopicCard;
})();
