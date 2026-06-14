/* ==========================================================================
   FieldSight MeetingTopicCard — Layer 5 composite (Sprint 2.8 / Phase H)
   --------------------------------------------------------------------------
   Renders one element of meeting_minutes.topics[] (BACKEND-CONTEXT §5.4).

   Differences from the Daily Report TopicCard:
     • action_items use `owner` (not `responsible`)
     • key_decisions are OBJECTS { decision, rationale, decided_by }
       (not strings)
     • there is a `status` field (decided | deferred | in_discussion |
       blocked) shown as a coloured pill
     • there is no safety_flags section
     • there is an `open_questions` section

   Topic.category is one of: strategy | operations | finance | product |
   partnership | technical | hr | legal | general — we map onto a small
   tone palette via Badge.

   Topic-level action items in meetings have no audit-state link in this
   prototype (they're not persisted by /api/actions/toggle, which keys
   off the daily report's topic_id). The row is informational only.

   Props:
     topic         meeting.topics[i]
     defaultOpen   boolean
     onSelect      (topic) => void  — open in right detail
     selected      boolean

   Exported to:
     window.FieldSight.MeetingTopicCard
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  var CATEGORY_TONE = {
    strategy:    'accent',
    operations:  'info',
    finance:     'success',
    product:     'info',
    partnership: 'accent',
    technical:   'info',
    hr:          'warning',
    legal:       'danger',
    general:     'neutral',
  };

  var STATUS_TONE = {
    decided:       'success',
    deferred:      'warning',
    in_discussion: 'info',
    blocked:       'danger',
  };

  var STATUS_LABEL = {
    decided:       'Decided',
    deferred:      'Deferred',
    in_discussion: 'In discussion',
    blocked:       'Blocked',
  };

  var PRIORITY_TONE = { high: 'danger', medium: 'warning', low: 'info' };

  function pluralise(n, sing) {
    return n + ' ' + sing + (n === 1 ? '' : 's');
  }

  function MeetingTopicCard(props) {
    var Card    = window.FieldSight.Card;
    var Badge   = window.FieldSight.Badge;
    var NavIcon = window.FieldSight.NavIcon;

    var topic = props.topic || {};

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

    var participants    = topic.participants    || [];
    var key_decisions   = topic.key_decisions   || [];
    var action_items    = topic.action_items    || [];
    var open_questions  = topic.open_questions  || [];
    var category        = (topic.category || 'general').toLowerCase();
    var status          = topic.status;

    var counts = [];
    if (key_decisions.length)  counts.push(pluralise(key_decisions.length,  'decision'));
    if (action_items.length)   counts.push(pluralise(action_items.length,   'action'));
    if (open_questions.length) counts.push(pluralise(open_questions.length, 'open question'));

    var className = 'fs-topic-card fs-topic-card--meeting'
      + (open ? ' fs-topic-card--open' : '')
      + (props.selected ? ' fs-topic-card--selected' : '');

    return React.createElement(Card, {
      padding:   'none',
      className: className,
    },

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
            React.createElement(Badge, {
              tone:    CATEGORY_TONE[category] || 'neutral',
              size:    'sm',
              variant: 'subtle',
              prefixDot: true,
            }, category.charAt(0).toUpperCase() + category.slice(1)),
            status ? React.createElement(Badge, {
              tone:    STATUS_TONE[status] || 'neutral',
              size:    'sm',
              variant: 'outline',
            }, STATUS_LABEL[status] || status) : null,
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

      !open && counts.length > 0
        ? React.createElement('div', { className: 'fs-topic-card__counts' },
            counts.join(' · '))
        : null,

      open ? React.createElement('div', { className: 'fs-topic-card__body' },

        topic.summary
          ? React.createElement('p', { className: 'fs-topic-card__summary' },
              topic.summary)
          : null,

        key_decisions.length > 0
          ? React.createElement('div', { className: 'fs-topic-card__section' },
              React.createElement('div', { className: 'fs-topic-card__section-label' },
                'Key decisions'),
              React.createElement('div', { className: 'fs-meeting-decisions' },
                key_decisions.map(function (d, i) {
                  return React.createElement('div', {
                    key: i, className: 'fs-meeting-decision',
                  },
                    React.createElement('div', { className: 'fs-meeting-decision__text' },
                      d.decision),
                    d.rationale ? React.createElement('div', {
                      className: 'fs-meeting-decision__rationale',
                    },
                      React.createElement('span', {
                        className: 'fs-meeting-decision__rationale-label',
                      }, 'Rationale · '),
                      d.rationale,
                    ) : null,
                    d.decided_by ? React.createElement('div', {
                      className: 'fs-meeting-decision__by',
                    }, 'Decided by ' + d.decided_by) : null,
                  );
                }),
              ),
            )
          : null,

        action_items.length > 0
          ? React.createElement('div', { className: 'fs-topic-card__section' },
              React.createElement('div', { className: 'fs-topic-card__section-label' },
                'Action items'),
              React.createElement('div', { className: 'fs-meeting-actions' },
                action_items.map(function (a, i) {
                  var p = (a.priority || '').toLowerCase();
                  return React.createElement('div', {
                    key: i, className: 'fs-meeting-action',
                  },
                    React.createElement('div', { className: 'fs-meeting-action__main' },
                      React.createElement('div', { className: 'fs-meeting-action__text' },
                        a.action),
                      React.createElement('div', { className: 'fs-meeting-action__meta' },
                        a.owner    ? React.createElement('span', null, a.owner) : null,
                        a.deadline ? React.createElement('span', null, 'Due ' + a.deadline) : null,
                      ),
                    ),
                    a.priority
                      ? React.createElement(Badge, {
                          tone: PRIORITY_TONE[p] || 'neutral',
                          size: 'sm', variant: 'outline',
                        }, a.priority.charAt(0).toUpperCase() + a.priority.slice(1))
                      : null,
                  );
                }),
              ),
              /* P-10 — meeting action items aren't backed by
                 /api/actions/toggle (which keys off the daily report's
                 topic_id, not meeting minutes). Surface that as a
                 caption so users don't expect to check them off. */
              React.createElement('div', { className: 'fs-meeting-actions__readonly' },
                'Read-only — meeting actions are tracked in the minutes,',
                ' not the daily-action audit log.'),
            )
          : null,

        open_questions.length > 0
          ? React.createElement('div', { className: 'fs-topic-card__section' },
              React.createElement('div', { className: 'fs-topic-card__section-label' },
                'Open questions'),
              React.createElement('ul', { className: 'fs-topic-card__decisions' },
                open_questions.map(function (q, i) {
                  return React.createElement('li', {
                    key: i, className: 'fs-topic-card__decision',
                  }, q);
                }),
              ),
            )
          : null,

      ) : null,
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.MeetingTopicCard = MeetingTopicCard;
})();
