/* ==========================================================================
   FieldSight Today Page — Sprint 1.6 lo-fi wireframe
   --------------------------------------------------------------------------
   Two exports: a Middle column component and a Right detail component.
   Both registered into window.FieldSight.PAGES under the '/today' key.

   Lo-fi: real Layer 4 components, mock data, minimal custom styling.
   Sprint 2 takes this layout to high-fidelity.
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function getComponents() {
    var fs = window.FieldSight;
    return {
      Card:        fs.Card,
      Badge:       fs.Badge,
      Avatar:      fs.Avatar,
      AvatarGroup: fs.AvatarGroup,
      Button:      fs.Button,
      IconButton:  fs.IconButton,
    };
  }

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

  /* ---------- Morning Brief Card ----------------------------------------- */
  function MorningBriefCard(props) {
    var c = getComponents();
    var brief = props.brief;

    return React.createElement(c.Card, { variant: 'elevated', padding: 'md' },
      React.createElement(c.Card.Header, {
        title: 'Morning Brief',
        subtitle: 'Generated from overnight transcripts · ' + brief.generatedAt,
        actions: React.createElement(c.IconButton, {
          icon: 'chevron-up',
          ariaLabel: 'Collapse brief',
          size: 'sm',
          onClick: function() {
            console.log('[Brief] toggle collapse — Sprint 2 wires this');
          },
        }),
      }),
      React.createElement(c.Card.Body, null,
        React.createElement('ul', {
          style: {
            margin: 0,
            padding: '0 0 0 18px',
            color: 'var(--text-secondary)',
            fontSize: '14px',
            lineHeight: 1.55,
          },
        },
          brief.bullets.map(function(b, i) {
            return React.createElement('li', {
              key: i,
              style: { marginBottom: i < brief.bullets.length - 1 ? '6px' : 0 },
            }, b);
          })
        ),
      ),
      React.createElement(c.Card.Footer, { align: 'start' },
        React.createElement(c.Button, {
          variant: 'tertiary',
          size: 'sm',
          rightIcon: 'arrow-right',
          onClick: function() {
            console.log('[Brief] open full brief — Sprint 2 wires this');
          },
        }, 'Read full brief'),
      ),
    );
  }

  /* ---------- Urgent Card ------------------------------------------------- */
  function UrgentCard(props) {
    var c = getComponents();
    var item = props.item;

    return React.createElement(c.Card, {
      variant: 'default',
      padding: 'sm',
      onClick: function() { props.onSelect(item); },
    },
      React.createElement(c.Card.Header, {
        title: item.title,
        actions: React.createElement(c.Badge, {
          tone: item.badgeTone,
          size: 'sm',
          prefixDot: true,
        }, item.badgeLabel),
      }),
      React.createElement(c.Card.Body, null,
        React.createElement('div', {
          style: { fontSize: '13px', color: 'var(--text-secondary)' },
        }, item.body),
      ),
    );
  }

  /* ---------- Task Row Card ----------------------------------------------- */
  function TaskRow(props) {
    var c = getComponents();
    var task = props.task;
    var isMine = props.isMine;

    return React.createElement(c.Card, {
      padding: 'sm',
      onClick: function() { props.onSelect(task); },
      className: isMine ? 'fs-task-row fs-task-row--mine' : 'fs-task-row',
    },
      React.createElement(c.Card.Body, null,
        React.createElement('div', {
          style: { display: 'flex', alignItems: 'center', gap: '10px' },
        },
          React.createElement(c.Avatar, { name: task.assignee, size: 'sm' }),
          React.createElement('div', {
            style: { flex: 1, minWidth: 0 },
          },
            React.createElement('div', {
              style: {
                fontSize: '14px',
                fontWeight: 500,
                color: 'var(--text-primary)',
                lineHeight: 1.35,
                display: '-webkit-box',
                WebkitBoxOrient: 'vertical',
                WebkitLineClamp: 2,
                overflow: 'hidden',
                wordBreak: 'break-word',
              },
            }, task.title),
          ),
          React.createElement('div', {
            style: { display: 'flex', alignItems: 'center', gap: '8px', flexShrink: 0 },
          },
            React.createElement(c.Badge, { tone: task.statusTone, size: 'sm' }, task.status),
            React.createElement('span', {
              style: {
                fontSize: '12px',
                color: 'var(--text-tertiary)',
                fontFamily: 'var(--font-mono)',
                minWidth: '36px',
                textAlign: 'right',
              },
            }, task.dueTime),
          ),
        ),
      ),
    );
  }

  /* ---------- Activity Row ------------------------------------------------ */
  function ActivityRow(props) {
    var c = getComponents();
    var item = props.item;

    return React.createElement(c.Card, {
      padding: 'sm',
      onClick: function() { props.onSelect(item); },
    },
      React.createElement(c.Card.Body, null,
        React.createElement('div', {
          style: { display: 'flex', alignItems: 'flex-start', gap: '10px' },
        },
          React.createElement(c.Avatar, { name: item.speaker, size: 'sm' }),
          React.createElement('div', { style: { flex: 1, minWidth: 0 } },
            React.createElement('div', {
              style: {
                fontSize: '13px',
                color: 'var(--text-primary)',
                lineHeight: 1.4,
              },
            }, item.snippet),
            React.createElement('div', {
              style: {
                fontSize: '11px',
                color: 'var(--text-tertiary)',
                marginTop: '4px',
              },
            }, item.speaker + ' · ' + item.timeAgo),
          ),
        ),
      ),
    );
  }

  /* ---------- On Site Card ------------------------------------------------ */
  function OnSiteCard(props) {
    var c = getComponents();
    return React.createElement(c.Card, { padding: 'md' },
      React.createElement(c.Card.Body, null,
        React.createElement('div', {
          style: {
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
          },
        },
          React.createElement(c.AvatarGroup, { size: 'md', max: 5 },
            props.people.map(function(p) {
              return React.createElement(c.Avatar, { key: p.id, name: p.name });
            })
          ),
          React.createElement('span', {
            style: { fontSize: '13px', color: 'var(--text-tertiary)' },
          }, props.people.length + ' on site'),
        ),
      ),
    );
  }

  /* ---------- Today Middle Column ----------------------------------------- */
  function TodayMiddleColumn(props) {
    var data = window.FieldSight.MockData.TODAY;
    var onSelect = props.onSelect || function() {};

    return React.createElement('div', {
      style: { display: 'flex', flexDirection: 'column', gap: 0 },
      className: 'fs-page fs-page--today',
    },

      React.createElement(MorningBriefCard, { brief: data.morningBrief }),

      data.urgent && data.urgent.length > 0
        ? React.createElement(React.Fragment, null,
            React.createElement(SectionLabel, { color: 'var(--color-danger-700)' }, 'Urgent now'),
            React.createElement('div', {
              style: { display: 'flex', flexDirection: 'column', gap: '6px' },
            },
              data.urgent.map(function(item) {
                return React.createElement(UrgentCard, {
                  key: item.id, item: item, onSelect: onSelect,
                });
              })
            ),
          )
        : null,

      /* TASKS TODAY — split: my tasks first, then team's */
      React.createElement(SectionLabel, null, 'Tasks today'),

      /* My tasks */
      data.myTasks && data.myTasks.length > 0 ? React.createElement(React.Fragment, null,
        React.createElement(SubsectionLabel, null,
          'My tasks · ' + data.myTasks.length),
        React.createElement('div', {
          style: { display: 'flex', flexDirection: 'column', gap: '6px' },
        },
          data.myTasks.map(function(task) {
            return React.createElement(TaskRow, {
              key: task.id, task: task, onSelect: onSelect, isMine: true,
            });
          })
        ),
      ) : null,

      /* Team's tasks — visible when user has site-level visibility */
      data.teamTasks && data.teamTasks.length > 0 ? React.createElement(React.Fragment, null,
        React.createElement(SubsectionLabel, null,
          'Team · ' + data.teamTasks.length),
        React.createElement('div', {
          style: { display: 'flex', flexDirection: 'column', gap: '6px' },
        },
          data.teamTasks.map(function(task) {
            return React.createElement(TaskRow, {
              key: task.id, task: task, onSelect: onSelect, isMine: false,
            });
          })
        ),
      ) : null,

      React.createElement(SectionLabel, null, 'Recent activity'),
      React.createElement('div', {
        style: { display: 'flex', flexDirection: 'column', gap: '6px' },
      },
        data.activity.map(function(item) {
          return React.createElement(ActivityRow, {
            key: item.id, item: item, onSelect: onSelect,
          });
        })
      ),

      React.createElement(SectionLabel, null, 'On site now'),
      React.createElement(OnSiteCard, { people: data.onSite }),

    );
  }

  /* ---------- Today Right Detail ------------------------------------------ */
  function TodayRightDetail(props) {
    var c = getComponents();
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

    var item = window.FieldSight.MockData.findItemById(sel.id);
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

    var related  = window.FieldSight.MockData.getRelated(item)  || [];
    var timeline = window.FieldSight.MockData.getTimeline(item) || [];

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

      /* Header row: title + close button */
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
        React.createElement(c.IconButton, {
          icon: 'x',
          ariaLabel: 'Close detail',
          size: 'sm',
          onClick: function() {
            if (props.onClose) props.onClose();
          },
        }),
      ),

      /* Status badges row (kind-specific) */
      item.kind === 'urgent' ? React.createElement('div', { style: { display: 'flex', gap: '6px' } },
        React.createElement(c.Badge, {
          tone: item.badgeTone, size: 'sm', prefixDot: true,
        }, item.badgeLabel),
      ) : null,

      item.kind === 'task' ? React.createElement('div', { style: { display: 'flex', gap: '6px', flexWrap: 'wrap' } },
        React.createElement(c.Badge, { tone: item.statusTone, size: 'sm' }, item.status),
        item.priority ? React.createElement(c.Badge, {
          tone: item.priority === 'High' ? 'danger' : item.priority === 'Low' ? 'neutral' : 'warning',
          size: 'sm', variant: 'outline',
        }, item.priority) : null,
      ) : null,

      /* Key/value field rows */
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

      /* Related section */
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
            return React.createElement(c.Card, {
              key: i, padding: 'sm', variant: 'ghost',
              onClick: function() {
                console.log('[Right] navigate to related:', r.id);
              },
            },
              React.createElement(c.Card.Body, null,
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

      /* Timeline section */
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
        React.createElement('div', {
          style: { display: 'flex', flexDirection: 'column', gap: '8px',
                   paddingLeft: '8px',
                   borderLeft: '2px solid var(--border-subtle)' },
        },
          timeline.map(function(t, i) {
            return React.createElement('div', {
              key: i,
              style: { display: 'flex', flexDirection: 'column', gap: '2px',
                       paddingLeft: '8px' },
            },
              React.createElement('div', {
                style: { fontSize: '13px', color: 'var(--text-primary)' },
              }, t.label),
              React.createElement('div', {
                style: { fontSize: '11px', color: 'var(--text-tertiary)' },
              }, t.actor + ' · ' + t.time),
            );
          })
        ),
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
        React.createElement(c.Button, {
          variant: 'secondary',
          size: 'sm',
          onClick: function() {
            console.log('[Today] secondary action on', item.id);
          },
        }, 'Reassign'),
        React.createElement(c.Button, {
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
