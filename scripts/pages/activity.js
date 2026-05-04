/* ==========================================================================
   FieldSight Activity Page — Sprint 4.6 (rebuild on direction C)
   --------------------------------------------------------------------------
   /activity — *user-centred* activity stream. Each visible user gets
   a card showing what they contributed in the time window: topics
   participated in, actions owned, photos uploaded, safety flags
   raised. Click any card → right pane opens the full chronological
   event timeline for that user.

   Replaces the original Sprint 4.1 implementation (chronological
   topic feed across days), which had high overlap with /timeline.
   The kept-but-not-chosen alternatives (A: kill /activity entirely,
   B: raw on-site stream) live in PLAN.md under
   "Design alternatives held for revisit".

   Middle column:
     • Header: "Activity" + range caption + "Load more" (extends
       the time window backward by 14 days)
     • One UserActivityCard per visible user, sorted by total event
       count desc

   Right detail:
     • Selected user's full chronological event timeline
     • Click any event → /timeline?date=…&user=…&from=activity

   Architecture:
     • ActivityProvider fetches once via the new
       FS.api.userActivity.getUserActivityRange aggregator
     • Worker rule: aggregator returns just caller's own row
     • site_manager / pm: scoped to caller's primary_site
     • admin / gm: full visibility

   Registers as window.FieldSight.PAGES['/activity']
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  var DEFAULT_DAYS = 14;
  var LOAD_STEP    = 14;

  var KIND_ICON = {
    topic:  '◇',
    action: '✓',
    photo:  '▤',
    safety: '⚠',
  };

  var KIND_LABEL = {
    topic:  'Participated in topic',
    action: 'Owned action item',
    photo:  'Uploaded photo',
    safety: 'Raised safety flag',
  };

  /* ---------- Helpers --------------------------------------------------- */

  function fmtDate(yyyymmdd) {
    if (!yyyymmdd) return '';
    var p = String(yyyymmdd).split('-').map(Number);
    if (p.length !== 3) return yyyymmdd;
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    var days   = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    var d = new Date(Date.UTC(p[0], p[1] - 1, p[2]));
    return days[d.getUTCDay()] + ' ' + p[2] + ' ' + months[p[1] - 1];
  }

  function fmtDateShort(yyyymmdd) {
    if (!yyyymmdd) return '';
    var p = String(yyyymmdd).split('-').map(Number);
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return p[2] + ' ' + months[p[1] - 1];
  }

  /* ---------- ActivityContext ----------------------------------------- */

  var ActivityContext = React.createContext(null);

  function ActivityProvider(props) {
    var caller = (window.AuthMock && window.AuthMock.currentUser) || {};
    var depKey = (caller.name || '') + '|' + (caller.role || '') + '|' + (caller.isAdmin ? 'admin' : '');

    var refDays = React.useState(DEFAULT_DAYS);
    var daysToLoad    = refDays[0];
    var setDaysToLoad = refDays[1];

    var refState = React.useState({ status: 'loading' });
    var state    = refState[0];
    var setState = refState[1];

    var retryRef   = React.useState(0);
    var retryCount = retryRef[0];
    var setRetry   = retryRef[1];

    React.useEffect(function () {
      var cancelled = false;
      setState({ status: 'loading' });

      var today = window.FS.api.todayNZDT();
      var from  = window.FS.api.addDaysISO(today, -(daysToLoad - 1));

      window.FS.api.userActivity.getUserActivityRange({
        from: from, to: today,
      }).then(function (res) {
        if (cancelled) return;
        if (res && res._accessDenied) {
          setState({ status: 'access_denied', message: res.error });
          return;
        }
        var users = (res && res.users) || [];
        users.sort(function (a, b) {
          var ta = a.counts.topics + a.counts.actions + a.counts.photos + a.counts.safety_flags;
          var tb = b.counts.topics + b.counts.actions + b.counts.photos + b.counts.safety_flags;
          return tb - ta;
        });
        setState({
          status: 'ok',
          users:  users,
          from:   from,
          to:     today,
          dates:  res.dates || [],
        });
      }).catch(function (err) {
        if (cancelled) return;
        setState({ status: 'error', error: { code: (err && err.status) || 0, message: (err && err.message) || 'Could not load activity', retryable: true }, retry: function () { setRetry(function (n) { return n + 1; }); } });
      });

      return function () { cancelled = true; };
    }, [depKey, daysToLoad, retryCount]);

    function loadMore() { setDaysToLoad(function (n) { return n + LOAD_STEP; }); }

    var ctx = { state: state, daysToLoad: daysToLoad, loadMore: loadMore };
    return React.createElement(ActivityContext.Provider, { value: ctx },
      props.children);
  }

  /* ---------- ActivityMiddleColumn ------------------------------------- */

  function ActivityMiddleColumn(props) {
    var fs                = window.FieldSight;
    var UserActivityCard  = fs.UserActivityCard;
    var Button            = fs.Button;
    var onSelect          = props.onSelect || function () {};

    var ctx = React.useContext(ActivityContext);
    if (!ctx) {
      console.warn('[ActivityMiddleColumn] ActivityContext missing');
      return null;
    }
    var state = ctx.state;

    if (state.status === 'loading') {
      return React.createElement('div', { className: 'fs-activity' },
        React.createElement('div', { className: 'fs-activity__loading' },
          'Aggregating user activity…'),
      );
    }
    if (state.status === 'error') {
      var ErrorBanner = window.FieldSight.ErrorBanner;
      return React.createElement('div', { className: 'fs-activity' },
        ErrorBanner
          ? React.createElement(ErrorBanner, {
              message:   (state.error && state.error.message) || 'Could not load activity',
              retryable: true,
              onRetry:   state.retry,
            })
          : React.createElement('div', { className: 'fs-activity__empty' },
              (state.error && state.error.message) || 'Could not load activity'),
      );
    }
    if (state.status === 'access_denied') {
      var AccessDenied = fs.AccessDenied;
      return React.createElement('div', { className: 'fs-activity' },
        AccessDenied
          ? React.createElement(AccessDenied, {
              scope:   'this activity stream',
              message: state.message,
            })
          : React.createElement('div', null, 'Access denied.'),
      );
    }

    var users = state.users || [];
    var selectedFolder = props.selectedItem && props.selectedItem.kind === 'activity_user'
      ? props.selectedItem.user_folder
      : null;

    return React.createElement('div', { className: 'fs-activity' },

      /* Header */
      React.createElement('div', { className: 'fs-activity__header' },
        React.createElement('h2', { className: 'fs-activity__title' }, 'Activity'),
        React.createElement('div', { className: 'fs-activity__subtitle' },
          'What each person on your team has been doing recently'),
        React.createElement('div', { className: 'fs-activity__meta' },
          users.length + ' ' + (users.length === 1 ? 'person' : 'people')
            + ' · last ' + ctx.daysToLoad + ' days'
            + ' (' + fmtDateShort(state.from) + ' → ' + fmtDateShort(state.to) + ')'),
      ),

      /* User list */
      users.length === 0
        ? React.createElement('div', { className: 'fs-activity__empty' },
            'No users visible to your role.')
        : React.createElement('div', { className: 'fs-activity__list' },
            users.map(function (u) {
              return React.createElement(UserActivityCard, {
                key:      u.user_folder,
                user:     u,
                selected: selectedFolder === u.user_folder,
                onSelect: function () {
                  onSelect({
                    kind:        'activity_user',
                    id:          'user_' + u.user_folder,
                    user_folder: u.user_folder,
                    user:        u,
                  });
                },
              });
            }),
          ),

      /* Load more */
      users.length > 0
        ? React.createElement('div', { className: 'fs-activity__load-more' },
            React.createElement(Button, {
              variant: 'secondary', size: 'sm',
              onClick: ctx.loadMore,
            }, 'Load more (+' + LOAD_STEP + ' days)'),
          )
        : null,
    );
  }

  /* ---------- ActivityRightDetail -------------------------------------- */

  function ActivityRightDetail(props) {
    var fs       = window.FieldSight;
    var Card     = fs.Card;
    var Avatar   = fs.Avatar;
    var Badge    = fs.Badge;
    var Button   = fs.Button;
    var IconBtn  = fs.IconButton;

    var sel = props.selectedItem;

    if (!sel || sel.kind !== 'activity_user') {
      return React.createElement('div', { className: 'fs-activity-detail__placeholder' },
        React.createElement('div', { className: 'fs-activity-detail__placeholder-title' },
          'Select a person'),
        React.createElement('div', { className: 'fs-activity-detail__placeholder-body' },
          'Pick any card to see the full timeline of what that person has been doing — every topic they joined, every action they own, every photo they uploaded, every safety flag they raised.'),
      );
    }

    var u = sel.user;
    var counts = u.counts;
    var events = u.events || [];

    function openInTimeline(ev) {
      var qs = '?date=' + encodeURIComponent(ev.date)
             + '&user=' + encodeURIComponent(u.user_folder)
             + '&from=activity';
      window.FS.Router.navigate('/timeline' + qs);
    }

    /* Group events by date for visual rhythm. */
    var groups = [];
    var current = null;
    events.forEach(function (ev) {
      if (ev.date !== current) {
        groups.push({ date: ev.date, events: [] });
        current = ev.date;
      }
      groups[groups.length - 1].events.push(ev);
    });

    return React.createElement('div', { className: 'fs-activity-detail' },

      /* Header */
      React.createElement('div', { className: 'fs-activity-detail__header' },
        React.createElement(Avatar, { name: u.user_name, size: 'lg' }),
        React.createElement('div', { className: 'fs-activity-detail__id' },
          React.createElement('h2', { className: 'fs-activity-detail__name' }, u.user_name),
          React.createElement('div', { className: 'fs-activity-detail__sub' },
            [u.role, u.primary_site].filter(Boolean).join(' · ')),
        ),
        IconBtn ? React.createElement(IconBtn, {
          icon: 'x', ariaLabel: 'Close detail', size: 'sm',
          onClick: function () { if (props.onClose) props.onClose(); },
        }) : null,
      ),

      /* Counts repeated for context */
      React.createElement('div', { className: 'fs-activity-detail__counts' },
        ['topics', 'actions', 'photos', 'safety_flags'].map(function (k) {
          var label = ({ topics: 'Topics', actions: 'Actions',
                         photos: 'Photos', safety_flags: 'Safety' })[k];
          return React.createElement('div', {
            key:       k,
            className: 'fs-activity-detail__count'
              + (k === 'safety_flags' && counts[k] > 0 ? ' fs-activity-detail__count--danger' : ''),
          },
            React.createElement('div', { className: 'fs-activity-detail__count-value' },
              counts[k]),
            React.createElement('div', { className: 'fs-activity-detail__count-label' },
              label),
          );
        }),
      ),

      /* Event timeline */
      events.length === 0
        ? React.createElement('div', { className: 'fs-activity-detail__empty' },
            'No activity for this person in the selected window.')
        : React.createElement('div', { className: 'fs-activity-detail__timeline' },
            groups.map(function (g) {
              return React.createElement(React.Fragment, { key: g.date },
                React.createElement('div', {
                  className: 'fs-activity-detail__date-header',
                }, fmtDate(g.date)),
                React.createElement('div', { className: 'fs-activity-detail__group' },
                  g.events.map(function (ev, i) {
                    return React.createElement('button', {
                      key:       g.date + '_' + i,
                      type:      'button',
                      className: 'fs-activity-detail__event'
                                  + ' fs-activity-detail__event--' + ev.kind,
                      onClick:   function () { openInTimeline(ev); },
                      title:     'Open in /timeline',
                    },
                      React.createElement('span', {
                        className: 'fs-activity-detail__event-icon',
                      }, KIND_ICON[ev.kind] || '·'),
                      React.createElement('div', { className: 'fs-activity-detail__event-main' },
                        React.createElement('div', { className: 'fs-activity-detail__event-text' },
                          ev.summary || ev.topic_title),
                        React.createElement('div', { className: 'fs-activity-detail__event-meta' },
                          (KIND_LABEL[ev.kind] || ev.kind)
                            + (ev.time_label ? ' · ' + ev.time_label : '')
                            + (ev.topic_title ? ' · ' + ev.topic_title : '')
                            + (ev.kind === 'action' && ev.extra && ev.extra.checked
                                ? ' · ✓ done' : '')),
                      ),
                    );
                  }),
                ),
              );
            }),
          ),
    );
  }

  /* ---------- Register --------------------------------------------------- */

  if (!window.FieldSight) window.FieldSight = {};
  if (!window.FieldSight.PAGES) window.FieldSight.PAGES = {};
  window.FieldSight.PAGES['/activity'] = {
    Middle:   ActivityMiddleColumn,
    Right:    ActivityRightDetail,
    Provider: ActivityProvider,
  };

})();
