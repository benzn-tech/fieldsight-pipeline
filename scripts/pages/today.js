/* ==========================================================================
   FieldSight Today Page — Sprint 2.0 (Layer 5 composition)
   --------------------------------------------------------------------------
   Two exports: a Middle column component and a Right detail component.
   Both registered into window.FieldSight.PAGES under the '/today' key.

   This page composes Layer 5 composites — TaskCard / UrgentCard /
   ActivityCard / MorningBriefCard / OnSiteCard / Timeline — instead
   of inlining their JSX. Sprint 2.1 takes the layout to high-fidelity
   (KpiStrip, time-aware greeting, Brief truly collapses).
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  /* ---------- SectionLabel (small uppercase heading) --------------------- */
  function SectionLabel(props) {
    var color = props.color || 'var(--text-tertiary)';
    return React.createElement('div', {
      style: {
        fontSize: '10px',
        fontWeight: 600,
        letterSpacing: '0.08em',
        textTransform: 'uppercase',
        color: color,
        margin: '20px 0 8px',
        padding: '0 4px',
      },
    }, props.children);
  }

  /* ---------- SubsectionLabel (in-section grouping, smaller) ------------- */
  function SubsectionLabel(props) {
    return React.createElement('div', {
      style: {
        fontSize: '11px',
        fontWeight: 600,
        color: 'var(--text-secondary)',
        margin: '8px 0 4px',
        padding: '0 4px',
        letterSpacing: '0.02em',
      },
    }, props.children);
  }

  /* ---------- Today Middle Column ----------------------------------------- */
  function TodayMiddleColumn(props) {
    var fs       = window.FieldSight;
    var data     = fs.MockData.TODAY;
    var onSelect = props.onSelect || function() {};

    return React.createElement('div', {
      style: { display: 'flex', flexDirection: 'column', gap: 0 },
      className: 'fs-page fs-page--today',
    },

      /* MORNING BRIEF */
      React.createElement(fs.MorningBriefCard, { brief: data.morningBrief }),

      /* URGENT */
      data.urgent && data.urgent.length > 0
        ? React.createElement(React.Fragment, null,
            React.createElement(SectionLabel, { color: 'var(--color-danger-700)' }, 'Urgent now'),
            React.createElement('div', {
              style: { display: 'flex', flexDirection: 'column', gap: '6px' },
            },
              data.urgent.map(function(item) {
                return React.createElement(fs.UrgentCard, {
                  key: item.id, item: item, onSelect: onSelect,
                });
              })
            ),
          )
        : null,

      /* TASKS — split into My + Team */
      React.createElement(SectionLabel, null, 'Tasks today'),

      data.myTasks && data.myTasks.length > 0 ? React.createElement(React.Fragment, null,
        React.createElement(SubsectionLabel, null,
          'My tasks · ' + data.myTasks.length),
        React.createElement('div', {
          style: { display: 'flex', flexDirection: 'column', gap: '6px' },
        },
          data.myTasks.map(function(task) {
            return React.createElement(fs.TaskCard, {
              key: task.id, task: task, onSelect: onSelect, isMine: true,
            });
          })
        ),
      ) : null,

      data.teamTasks && data.teamTasks.length > 0 ? React.createElement(React.Fragment, null,
        React.createElement(SubsectionLabel, null,
          'Team · ' + data.teamTasks.length),
        React.createElement('div', {
          style: { display: 'flex', flexDirection: 'column', gap: '6px' },
        },
          data.teamTasks.map(function(task) {
            return React.createElement(fs.TaskCard, {
              key: task.id, task: task, onSelect: onSelect, isMine: false,
            });
          })
        ),
      ) : null,

      /* ACTIVITY */
      React.createElement(SectionLabel, null, 'Recent activity'),
      React.createElement('div', {
        style: { display: 'flex', flexDirection: 'column', gap: '6px' },
      },
        data.activity.map(function(item) {
          return React.createElement(fs.ActivityCard, {
            key: item.id, item: item, onSelect: onSelect,
          });
        })
      ),

      /* ON SITE */
      React.createElement(SectionLabel, null, 'On site now'),
      React.createElement(fs.OnSiteCard, { people: data.onSite }),

    );
  }

  /* ---------- Today Right Detail ------------------------------------------ */
  function TodayRightDetail(props) {
    var fs       = window.FieldSight;
    var Card     = fs.Card;
    var Badge    = fs.Badge;
    var Button   = fs.Button;
    var IconBtn  = fs.IconButton;
    var Timeline = fs.Timeline;

    var sel = props.selectedItem;

    /* Empty state */
    if (!sel) {
      return React.createElement('div', {
        style: {
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          height: '100%',
          padding: '32px',
          gap: '12px',
          color: 'var(--text-tertiary)',
        },
      },
        React.createElement('div', {
          style: { fontSize: '14px', fontWeight: 500, color: 'var(--text-secondary)' },
        }, 'Select an item'),
        React.createElement('div', { style: { fontSize: '13px' } },
          'Choose from the list to view details'),
      );
    }

    var item = fs.MockData.findItemById(sel.id);
    if (!item) return null;

    var rows = [];
    if (item.kind === 'task') {
      rows = [
        ['Assignee', item.assignee],
        ['Due',      item.dueTime],
        ['Status',   item.status],
        ['Priority', item.priority || 'Medium'],
      ];
    } else if (item.kind === 'urgent') {
      rows = [
        ['Severity',     item.badgeLabel],
        ['Triggered by', item.triggeredBy || 'Manual flag'],
        ['Detail',       item.body],
        ['Action',       'Pending operator confirmation.'],
      ];
    } else if (item.kind === 'activity') {
      rows = [
        ['Speaker',  item.speaker],
        ['When',     item.timeAgo],
        ['Source',   'PTT transcript'],
        ['Channel',  item.channel || 'General'],
      ];
    }

    var related  = fs.MockData.getRelated(item)  || [];
    var timeline = fs.MockData.getTimeline(item) || [];

    return React.createElement('div', {
      style: {
        padding: '24px',
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        gap: '20px',
        overflowY: 'auto',
        boxSizing: 'border-box',
      },
    },

      /* Header row: title + close */
      React.createElement('div', {
        style: {
          display: 'flex',
          alignItems: 'flex-start',
          justifyContent: 'space-between',
          gap: '12px',
        },
      },
        React.createElement('h2', {
          style: {
            margin: 0,
            fontSize: '18px',
            fontWeight: 600,
            color: 'var(--text-primary)',
            lineHeight: 1.3,
            flex: 1,
            minWidth: 0,
            display: '-webkit-box',
            WebkitBoxOrient: 'vertical',
            WebkitLineClamp: 3,
            overflow: 'hidden',
            wordBreak: 'break-word',
          },
        }, item.title || item.snippet || '(item)'),
        React.createElement(IconBtn, {
          icon: 'x',
          ariaLabel: 'Close detail',
          size: 'sm',
          onClick: function() {
            if (props.onClose) props.onClose();
          },
        }),
      ),

      /* Status badges (kind-specific) */
      item.kind === 'urgent' ? React.createElement('div', {
        style: { display: 'flex', gap: '6px' },
      },
        React.createElement(Badge, {
          tone: item.badgeTone, size: 'sm', prefixDot: true,
        }, item.badgeLabel),
      ) : null,

      item.kind === 'task' ? React.createElement('div', {
        style: { display: 'flex', gap: '6px', flexWrap: 'wrap' },
      },
        React.createElement(Badge, { tone: item.statusTone, size: 'sm' }, item.status),
        item.priority ? React.createElement(Badge, {
          tone: item.priority === 'High' ? 'danger' : item.priority === 'Low' ? 'neutral' : 'warning',
          size: 'sm', variant: 'outline',
        }, item.priority) : null,
      ) : null,

      /* Field rows */
      React.createElement('div', {
        style: { display: 'flex', flexDirection: 'column', gap: 0 },
      },
        rows.map(function(r, i) {
          return React.createElement('div', {
            key: i,
            style: {
              display: 'flex',
              gap: '12px',
              padding: '10px 0',
              borderBottom: i < rows.length - 1 ? '1px solid var(--border-subtle)' : 'none',
            },
          },
            React.createElement('div', {
              style: {
                fontSize: '11px',
                color: 'var(--text-tertiary)',
                fontWeight: 600,
                width: '88px',
                flexShrink: 0,
                textTransform: 'uppercase',
                letterSpacing: '0.06em',
                paddingTop: '2px',
              },
            }, r[0]),
            React.createElement('div', {
              style: { fontSize: '14px', color: 'var(--text-primary)', flex: 1, lineHeight: 1.45 },
            }, r[1]),
          );
        })
      ),

      /* Related */
      related.length > 0 ? React.createElement(React.Fragment, null,
        React.createElement('div', {
          style: {
            fontSize: '11px', fontWeight: 600,
            color: 'var(--text-tertiary)',
            textTransform: 'uppercase',
            letterSpacing: '0.06em',
            marginTop: '4px',
          },
        }, 'Related'),
        React.createElement('div', {
          style: { display: 'flex', flexDirection: 'column', gap: '6px' },
        },
          related.map(function(r, i) {
            return React.createElement(Card, {
              key: i, padding: 'sm', variant: 'ghost',
              onClick: function() {
                console.log('[Right] navigate to related:', r.id);
              },
            },
              React.createElement(Card.Body, null,
                React.createElement('div', {
                  style: { fontSize: '13px', color: 'var(--text-primary)', fontWeight: 500 },
                }, r.title),
                React.createElement('div', {
                  style: { fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' },
                }, r.subtitle),
              ),
            );
          })
        ),
      ) : null,

      /* Timeline (L5 composite) */
      timeline.length > 0 ? React.createElement(React.Fragment, null,
        React.createElement('div', {
          style: {
            fontSize: '11px', fontWeight: 600,
            color: 'var(--text-tertiary)',
            textTransform: 'uppercase',
            letterSpacing: '0.06em',
            marginTop: '4px',
          },
        }, 'Timeline'),
        React.createElement(Timeline, { events: timeline }),
      ) : null,

      /* Action buttons pinned to bottom */
      React.createElement('div', {
        style: {
          marginTop: 'auto',
          display: 'flex',
          gap: '8px',
          justifyContent: 'flex-end',
          paddingTop: '16px',
          borderTop: '1px solid var(--border-subtle)',
        },
      },
        React.createElement(Button, {
          variant: 'secondary',
          size: 'sm',
          onClick: function() {
            console.log('[Today] secondary action on', item.id);
          },
        }, 'Reassign'),
        React.createElement(Button, {
          size: 'sm',
          leftIcon: 'check',
          onClick: function() {
            console.log('[Today] primary action on', item.id);
          },
        }, item.kind === 'task' ? 'Mark complete' : 'Acknowledge'),
      ),

    );
  }

  /* ---------- Register ---------------------------------------------------- */
  if (!window.FieldSight) window.FieldSight = {};
  if (!window.FieldSight.PAGES) window.FieldSight.PAGES = {};
  window.FieldSight.PAGES['/today'] = {
    Middle: TodayMiddleColumn,
    Right:  TodayRightDetail,
  };

})();
