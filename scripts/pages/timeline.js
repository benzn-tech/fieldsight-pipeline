/* ==========================================================================
   FieldSight Timeline Page — Sprint 2.2 (PLAN.md Phase B)
   --------------------------------------------------------------------------
   The PRIMARY surface the backend was designed to serve:
     /timeline?date=YYYY-MM-DD&user=Jarley_Trainor

   Middle column:
     • Header: date · user · site
     • KpiStrip:   Topics · Safety · Recordings · Words
     • ExecutiveSummaryCard
     • Topic list (TopicCard, collapsible, click to open in right detail)
     • Empty / no-report / admin-disambiguation states

   Right detail:
     • TopicDetail panel with tabs (Overview, Transcript, Audio, Video,
       Photos). Sprint 2.2 ships Overview + Photos against real fixtures;
       Transcript / Audio / Video tabs have placeholder content that
       Phase C (Sprint 2.3) wires up against the existing api modules.

   Bug-traps honoured here:
     • BUG-19 NZDT date math — uses FS.api.addDaysISO, never new Date(str).
     • BUG-20 CloudFront-HTML-404 — getTimeline returns { _notFound:true }
       on either a real 404 or a 200/HTML body, so the no-report branch
       triggers for both.
     • §8.7 empty arrays render gracefully.

   Registers as window.FieldSight.PAGES['/timeline']
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  /* ---------- helpers --------------------------------------------------- */

  function readRouteParams() {
    var route = window.FS && window.FS.Router && window.FS.Router.getCurrentRoute();
    return (route && route.params) || {};
  }

  function callerFolder() {
    var u = (window.AuthMock && window.AuthMock.currentUser) || {};
    if (!u.name) return null;
    return window.FS.api.folderName(u.name);
  }

  function isAdminLike(user) {
    return user && (user.role === 'admin' || user.role === 'gm' || user.isAdmin);
  }

  /* Pick the most recent date with a report from /api/dates, or null.
     Mirrors the helper in today.js so the two pages share the same
     fallback semantics — when "today" has no report, the user lands
     on the latest available rather than a stale hardcoded date. */
  function findLatestReportDate(datesMap) {
    var keys = Object.keys(datesMap || {}).filter(function (d) {
      return datesMap[d] && datesMap[d].hasReport;
    });
    if (keys.length === 0) return null;
    keys.sort();
    return keys[keys.length - 1];
  }

  function formatDateLabel(yyyymmdd) {
    if (!yyyymmdd) return '';
    var p = yyyymmdd.split('-').map(Number);
    var d = new Date(Date.UTC(p[0], p[1] - 1, p[2]));
    var months = ['Jan','Feb','Mar','Apr','May','Jun',
                  'Jul','Aug','Sep','Oct','Nov','Dec'];
    var days   = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    return days[d.getUTCDay()] + ' ' + d.getUTCDate() + ' ' + months[d.getUTCMonth()] + ' ' + p[0];
  }

  function unfolder(folder) {
    return (folder || '').replace(/_/g, ' ');
  }

  /* ---------- shared header rendering ---------------------------------- */

  function PageHeader(props) {
    var report = props.report;
    var date   = props.date;
    var user   = props.user;
    var DatePicker = window.FieldSight.DatePicker;

    var subtitleParts = [];
    if (date) subtitleParts.push(formatDateLabel(date));
    if (user) subtitleParts.push(unfolder(user));
    if (report && report.site) subtitleParts.push(report.site);

    /* Sprint 4.5 — when the URL carries `from=today`, the user arrived
       here by clicking "View daily report" on /today. Surface an
       explicit back link so they don't have to dig for the left-nav. */
    var fromToday = (readRouteParams().from === 'today');

    /* Navigate the timeline route to a new date while preserving the
       active user query param (Sprint 2.5 / Phase E). */
    function onChangeDate(newDate) {
      var params = readRouteParams();
      var u = params.user || (user || '');
      var qs = '?date=' + newDate + (u ? '&user=' + u : '');
      window.FS.Router.navigate('/timeline' + qs);
    }

    return React.createElement('div', {
      className: 'fs-timeline-page__header',
    },
      fromToday ? React.createElement('button', {
        type:      'button',
        className: 'fs-timeline-page__back',
        onClick:   function () { window.FS.Router.navigate('/today'); },
      },
        React.createElement('span', { className: 'fs-timeline-page__back-arrow' },
          '←'),
        React.createElement('span', null, 'Back to Today'),
      ) : null,
      React.createElement('h2', { className: 'fs-timeline-page__title' },
        'Daily Report'),
      React.createElement('div', { className: 'fs-timeline-page__subtitle' },
        subtitleParts.join(' · ')),
      DatePicker && date ? React.createElement(DatePicker, {
        date:        date,
        onChange:    onChangeDate,
        monthsRange: 3,
      }) : null,
    );
  }

  /* ---------- KpiStrip wired from report metadata ---------------------- */

  function ReportKpis(props) {
    var KpiStrip = window.FieldSight.KpiStrip;
    var StatCard = window.FieldSight.StatCard;
    var report = props.report || {};

    var topics  = (report.topics || []).length;
    var safetyCount = (report.topics || []).reduce(function (acc, t) {
      var tagged = (t.category === 'safety') || ((t.safety_flags || []).length > 0);
      return acc + (tagged ? 1 : 0);
    }, 0);
    var meta = report._report_metadata || {};

    return React.createElement(KpiStrip, null,
      React.createElement(StatCard, {
        value: topics, label: 'Topics',
      }),
      React.createElement(StatCard, {
        value: safetyCount, label: 'Safety', tone: safetyCount > 0 ? 'danger' : 'neutral',
      }),
      React.createElement(StatCard, {
        value: meta.recordings_processed || 0, label: 'Recordings',
      }),
      React.createElement(StatCard, {
        value: meta.total_words ? meta.total_words.toLocaleString() : 0,
        label: 'Words',
      }),
    );
  }

  /* ---------- Empty / not-found states --------------------------------- */

  function NoReportState(props) {
    var Card = window.FieldSight.Card;
    return React.createElement(Card, {
      padding: 'lg', className: 'fs-timeline-page__empty',
    },
      React.createElement(Card.Body, null,
        React.createElement('div', { className: 'fs-timeline-page__empty-title' },
          'No report yet'),
        React.createElement('div', { className: 'fs-timeline-page__empty-body' },
          props.message || 'No report has been generated for this date and user.'),
      ),
    );
  }

  function AvailableUsersState(props) {
    var Card = window.FieldSight.Card;
    return React.createElement(Card, {
      padding: 'lg', className: 'fs-timeline-page__picker',
    },
      React.createElement(Card.Body, null,
        React.createElement('div', { className: 'fs-timeline-page__empty-title' },
          'Pick a user to view the report for ' + formatDateLabel(props.date)),
        React.createElement('ul', { className: 'fs-timeline-page__users' },
          (props.users || []).map(function (u) {
            return React.createElement('li', { key: u },
              React.createElement('button', {
                type: 'button',
                className: 'fs-timeline-page__user',
                onClick: function () {
                  window.FS.Router.navigate('/timeline?date=' + props.date + '&user=' + u);
                },
              }, unfolder(u)),
            );
          }),
        ),
      ),
    );
  }

  /* =====================================================================
     TimelineMiddleColumn
     ===================================================================== */
  function TimelineMiddleColumn(props) {
    var fs = window.FieldSight;
    var ExecutiveSummaryCard = fs.ExecutiveSummaryCard;
    var TopicCard            = fs.TopicCard;

    var refParams = React.useState(function () { return readRouteParams(); });
    var params    = refParams[0];
    var setParams = refParams[1];

    React.useEffect(function () {
      return window.FS.Router.subscribe(function (route) {
        setParams(Object.assign({}, route.params || {}));
      });
    }, []);

    /* Sprint 6.6.4 — deep-link target topic. When /safety or /quality
       launches into /timeline?topic=N, we auto-open + flash that
       topic; all other topics auto-collapse (focus mode). Parsed
       once per params change so navigating again resets the focus. */
    var targetTopicId = params.topic != null && params.topic !== ''
      ? parseInt(params.topic, 10)
      : null;
    if (targetTopicId !== null && isNaN(targetTopicId)) targetTopicId = null;

    /* Sprint 6.7.2 — deeper precision: when /safety includes
       &flag=<idx>, highlight that specific safety_flag inside the
       target topic (not the whole topic card). null = whole-topic
       flash from 6.6.4. */
    var targetFlagIdx = params.flag != null && params.flag !== ''
      ? parseInt(params.flag, 10)
      : null;
    if (targetFlagIdx !== null && isNaN(targetFlagIdx)) targetFlagIdx = null;

    /* Resolve effective (date, user) honouring worker-forced-self rule. */
    var caller = (window.AuthMock && window.AuthMock.currentUser) || {};
    var date   = params.date;            /* may be undefined → bootstrap resolves */
    var user   = params.user;
    if (caller.role === 'worker') user = callerFolder();
    if (!user && !isAdminLike(caller)) user = callerFolder();

    var refState = React.useState({ status: 'loading' });
    var state    = refState[0];
    var setState = refState[1];

    /* Sprint 2.8 (Phase H) — when both a daily report and meeting
       minutes exist for the date, the user picks which to view. */
    var refView = React.useState('daily');
    var view    = refView[0];
    var setView = refView[1];

    /* M-2 — when no date is in the URL, resolve one before fetching:
       try today (NZDT), fall back to the most recent date in
       /api/dates, then navigate so the URL reflects what the user is
       looking at. The fetch effect below sits in 'loading' until the
       redirect lands. */
    React.useEffect(function () {
      if (date) return undefined;
      var cancelled = false;
      var qsUser = user ? '&user=' + encodeURIComponent(user) : '';
      var today = window.FS.api.todayNZDT();

      window.FS.api.timeline.getTimeline({ date: today, user: user })
        .then(function (r) {
          if (cancelled) return null;
          if (r && !r._notFound && !r._accessDenied) return today;
          return window.FS.api.dates.getDates({ months: 3 }).then(function (res) {
            if (cancelled || !res || res._accessDenied) return today;
            return findLatestReportDate(res.dates || {}) || today;
          });
        })
        .then(function (resolved) {
          if (cancelled || !resolved) return;
          window.FS.Router.navigate('/timeline?date=' + resolved + qsUser);
        })
        .catch(function () { /* fall through; fetch effect won't run */ });

      return function () { cancelled = true; };
    }, [date, user]);

    React.useEffect(function () {
      if (!date) return undefined;            /* bootstrap above is in flight */
      var cancelled = false;
      setState({ status: 'loading' });
      Promise.all([
        window.FS.api.timeline.getTimeline({ date: date, user: user }),
        window.FS.api.actions.getActions(date),
        window.FS.api.meetings.getMeetingMinutes({ date: date, user: user }),
      ]).then(function (results) {
        if (cancelled) return;
        var report  = results[0];
        var actions = results[1].actions || {};
        var meeting = results[2];

        /* P-12 — page-level access-denied. If the daily-report endpoint
           rejected this caller (§8.4: non-admin querying another user),
           short-circuit to AccessDenied. We don't downgrade to a meeting
           view — if the timeline call was forbidden, the meeting fetch
           against the same folder almost certainly was too. */
        if (report && report._accessDenied) {
          setState({
            status:  'access_denied',
            message: report.error,
            scope:   user ? unfolder(user) + "'s daily report" : "this report",
          });
          return;
        }

        /* Meeting minutes fetched via the generic media presigner;
           a 403 there should NOT block the daily report from rendering.
           Strip access-denied / not-found responses to null. */
        if (meeting && (meeting._notFound || meeting._accessDenied)) {
          meeting = null;
        }

        var hasReport  = !!(report && !report._notFound && !report.available_users);
        var hasMeeting = !!meeting;

        /* Default to daily if it exists, otherwise meeting. The toggle
           UI surfaces only when both are present (§5.5). */
        setView(function (cur) {
          if (hasReport && hasMeeting) return cur;
          if (hasMeeting && !hasReport) return 'meeting';
          return 'daily';
        });
        setState({
          status:  'ok',
          report:  report,
          actions: actions,
          meeting: meeting,
        });
      }).catch(function (err) {
        if (cancelled) return;
        setState({ status: 'error', error: err });
      });
      return function () { cancelled = true; };
    }, [date, user]);

    /* Sprint 6.7.1 — keep state.actions in sync with cross-component
       toggles (the right-detail OverviewTab also renders the same
       action_items via its own ActionItemRow instances). When any
       sibling fires a successful toggle, mirror it into our local
       actions map so re-renders here see the new check state. */
    React.useEffect(function () {
      var bus = window.FS && window.FS.actionsBus;
      if (!bus) return undefined;
      return bus.subscribe(function (payload) {
        if (!payload || payload.date !== date) return;
        setState(function (s) {
          if (s.status !== 'ok') return s;
          var key = payload.topic_id + '_' + payload.action_index;
          var nextActions = Object.assign({}, s.actions || {});
          nextActions[key] = {
            checked:    !!payload.checked,
            checked_by: payload.checked_by,
            checked_at: payload.checked_at,
          };
          return Object.assign({}, s, { actions: nextActions });
        });
      });
    }, [date]);

    /* Sprint 6.6.4 — auto-select the deep-linked topic once per
       (date, topicId) pair. Fires after the report loads; finds the
       matching topic, asks the AppShell to open the right panel via
       props.onSelect. We track via ref so subsequent re-renders or
       state churn don't re-trigger. The ref resets when the target
       topic id changes (user clicked a different deep-link). */
    var autoSelectKeyRef = React.useRef(null);
    React.useEffect(function () {
      if (state.status !== 'ok' || targetTopicId === null) return;
      var report = state.report;
      if (!report || report._notFound || report.available_users) return;
      var key = date + '|' + targetTopicId;
      if (autoSelectKeyRef.current === key) return;
      var topic = (report.topics || []).filter(function (t) {
        return t.topic_id === targetTopicId;
      })[0];
      if (!topic) return;
      autoSelectKeyRef.current = key;
      if (props.onSelect) {
        props.onSelect({
          kind:      'topic',
          id:        'topic_' + topic.topic_id,
          topic_id:  topic.topic_id,
          topic:     topic,
          date:      date,
          user:      user,
          user_name: report.user_name,
        });
      }
    }, [state.status, targetTopicId, date]);

    /* Loading */
    if (state.status === 'loading') {
      return React.createElement('div', {
        className: 'fs-timeline-page',
      },
        React.createElement(PageHeader, { date: date, user: user }),
        React.createElement('div', { className: 'fs-timeline-page__loading' },
          'Loading report…'),
      );
    }

    if (state.status === 'error') {
      return React.createElement('div', { className: 'fs-timeline-page' },
        React.createElement(PageHeader, { date: date, user: user }),
        React.createElement(NoReportState, {
          message: 'Could not load report. ' + (state.error && state.error.message || ''),
        }),
      );
    }

    /* P-12 — empathetic 403 (BACKEND-CONTEXT §8.4). */
    if (state.status === 'access_denied') {
      var AccessDenied = window.FieldSight.AccessDenied;
      return React.createElement('div', { className: 'fs-timeline-page' },
        React.createElement(PageHeader, { date: date, user: user }),
        AccessDenied
          ? React.createElement(AccessDenied, {
              scope:   state.scope,
              message: state.message,
            })
          : React.createElement(NoReportState, { message: state.message || 'Access denied.' }),
      );
    }

    var report  = state.report;
    var meeting = state.meeting;
    var hasReport  = !!(report  && !report._notFound  && !report.available_users);
    var hasMeeting = !!meeting;

    /* Admin disambiguation shape: { date, available_users:[...] } */
    if (report && report.available_users && !hasMeeting) {
      return React.createElement('div', { className: 'fs-timeline-page' },
        React.createElement(PageHeader, { date: date, user: null }),
        React.createElement(AvailableUsersState, {
          date: date, users: report.available_users,
        }),
      );
    }

    /* No-anything shape */
    if (!hasReport && !hasMeeting) {
      return React.createElement('div', { className: 'fs-timeline-page' },
        React.createElement(PageHeader, { date: date, user: user }),
        React.createElement(NoReportState, {
          message: (report && report.message) || ('No report for ' + unfolder(user || '') + ' on ' + date),
        }),
      );
    }

    /* View toggle — surfaces only when both exist for the date (§5.5). */
    var bothExist = hasReport && hasMeeting;
    var effectiveView = view;
    if (effectiveView === 'meeting' && !hasMeeting) effectiveView = 'daily';
    if (effectiveView === 'daily'   && !hasReport)  effectiveView = 'meeting';

    var actionState = state.actions || {};
    var selectedTopicId = props.selectedItem && props.selectedItem.kind === 'topic'
      ? props.selectedItem.topic_id
      : null;

    var AskChat            = window.FieldSight.AskChat;
    var MeetingTopicCard   = window.FieldSight.MeetingTopicCard;

    function ViewToggle() {
      if (!bothExist) return null;
      return React.createElement('div', { className: 'fs-timeline-page__view-toggle', role: 'tablist' },
        React.createElement('button', {
          type: 'button', role: 'tab',
          className: 'fs-timeline-page__view-tab' + (effectiveView === 'daily'   ? ' fs-timeline-page__view-tab--active' : ''),
          'aria-selected': effectiveView === 'daily',
          onClick: function () { setView('daily'); },
        }, 'Daily report'),
        React.createElement('button', {
          type: 'button', role: 'tab',
          className: 'fs-timeline-page__view-tab' + (effectiveView === 'meeting' ? ' fs-timeline-page__view-tab--active' : ''),
          'aria-selected': effectiveView === 'meeting',
          onClick: function () { setView('meeting'); },
        }, 'Meeting minutes'),
      );
    }

    /* ---- Meeting view ---- */
    if (effectiveView === 'meeting') {
      return React.createElement('div', { className: 'fs-timeline-page' },
        React.createElement(PageHeader, { date: date, user: user, report: report || meeting }),
        React.createElement(ViewToggle),

        meeting.meeting_title ? React.createElement('div', {
          className: 'fs-timeline-page__meeting-title',
        }, meeting.meeting_title) : null,

        React.createElement(ExecutiveSummaryCard, {
          bullets: meeting.executive_summary,
          label:   'Meeting summary',
        }),

        React.createElement('div', { className: 'fs-timeline-page__section-label' },
          'Topics'),
        React.createElement('div', { className: 'fs-timeline-page__topics' },
          (meeting.topics || []).map(function (topic) {
            return React.createElement(MeetingTopicCard, {
              key:      topic.topic_id,
              topic:    topic,
              selected: selectedTopicId === topic.topic_id,
              onSelect: function () {
                if (props.onSelect) {
                  props.onSelect({
                    kind:       'meeting_topic',
                    id:         'meeting_topic_' + topic.topic_id,
                    topic_id:   topic.topic_id,
                    topic:      topic,
                    date:       date,
                    user:       user,
                    user_name:  meeting.user_name,
                  });
                }
              },
            });
          }),
        ),

        (meeting.next_steps || []).length > 0
          ? React.createElement(React.Fragment, null,
              React.createElement('div', { className: 'fs-timeline-page__section-label' },
                'Next steps'),
              React.createElement('ul', { className: 'fs-timeline-page__list' },
                meeting.next_steps.map(function (s, i) {
                  return React.createElement('li', { key: i }, s);
                })
              ),
            )
          : null,

        (meeting.parking_lot || []).length > 0
          ? React.createElement(React.Fragment, null,
              React.createElement('div', { className: 'fs-timeline-page__section-label' },
                'Parking lot'),
              React.createElement('ul', { className: 'fs-timeline-page__list' },
                meeting.parking_lot.map(function (s, i) {
                  return React.createElement('li', { key: i }, s);
                })
              ),
            )
          : null,
      );
    }

    /* ---- Daily report view (default) ---- */
    return React.createElement('div', {
      className: 'fs-timeline-page',
    },
      React.createElement(PageHeader, { date: date, user: user, report: report }),
      React.createElement(ViewToggle),
      React.createElement(ReportKpis, { report: report }),
      React.createElement(ExecutiveSummaryCard, {
        bullets: report.executive_summary,
      }),
      React.createElement('div', { className: 'fs-timeline-page__section-label' },
        'Topics'),
      React.createElement('div', { className: 'fs-timeline-page__topics' },
        (report.topics || []).map(function (topic) {
          /* Sprint 6.6.4 — focus mode + flash. When a deep-link target
             is set, the matching topic auto-opens (via defaultOpen
             boolean) and gets highlight=true (scrollIntoView + 3-pulse
             flash). Other topics force-collapse (defaultOpen=false)
             so the target reads as the focal point. When no target,
             defaultOpen=undefined leaves user-toggled state alone. */
          var isTarget = targetTopicId !== null && topic.topic_id === targetTopicId;
          var defaultOpenProp = targetTopicId === null
            ? undefined
            : isTarget;
          return React.createElement(TopicCard, {
            key:         topic.topic_id,
            topic:       topic,
            date:        date,
            actionState: actionState,
            selected:    selectedTopicId === topic.topic_id,
            defaultOpen: defaultOpenProp,
            highlight:   isTarget,
            /* Sprint 6.7.2 — only the matched topic gets a flagHighlight;
               others ignore. When targetFlagIdx is null, the topic-level
               flash from 6.6.4 owns the spotlight. */
            flagHighlight: isTarget ? targetFlagIdx : null,
            onSelect:    function () {
              if (props.onSelect) {
                props.onSelect({
                  kind:       'topic',
                  id:         'topic_' + topic.topic_id,
                  topic_id:   topic.topic_id,
                  topic:      topic,
                  date:       date,
                  user:       user,
                  user_name:  report.user_name,
                });
              }
            },
          });
        }),
      ),

      /* Per-report Ask Agent (PLAN Phase G). Stateless — each question
         is independent. Scope='both' grounds across transcript +
         report. */
      AskChat ? React.createElement(React.Fragment, null,
        React.createElement('div', { className: 'fs-timeline-page__section-label' },
          'Ask agent'),
        React.createElement(AskChat, {
          date:        date,
          user:        user,
          scope:       'both',
          placeholder: 'Ask anything about today’s report…',
          compact:     true,
          suggestions: [
            'What were today’s safety highlights?',
            'Which actions are still open?',
            'Any decisions about the scaffold inspection?',
          ],
        }),
      ) : null,
    );
  }

  /* =====================================================================
     TimelineRightDetail — TopicDetail panel + media tabs
     ===================================================================== */

  /* Tab sets — daily reports surface media (transcript / audio / video
     / photos), meeting minutes don't (their per-topic recordings live
     in a different bundle the prototype doesn't fetch). */
  var DAILY_TABS = [
    { key: 'overview',   label: 'Overview' },
    { key: 'transcript', label: 'Transcript' },
    { key: 'audio',      label: 'Audio' },
    { key: 'video',      label: 'Video' },
    { key: 'photos',     label: 'Photos' },
    { key: 'ask',        label: 'Ask' },
  ];
  var MEETING_TABS = [
    { key: 'overview', label: 'Overview' },
    { key: 'ask',      label: 'Ask' },
  ];

  /* Status / category palettes for meeting topics — kept in sync with
     the MeetingTopicCard composite. */
  var MEETING_STATUS_TONE  = { decided: 'success', deferred: 'warning', in_discussion: 'info', blocked: 'danger' };
  var MEETING_STATUS_LABEL = { decided: 'Decided', deferred: 'Deferred', in_discussion: 'In discussion', blocked: 'Blocked' };
  var MEETING_PRIORITY_TONE = { high: 'danger', medium: 'warning', low: 'info' };

  /* Topic time_range uses an en-dash: "07:00 – 07:30". Returns
     { start: 'HH:MM:SS', end: 'HH:MM:SS' } or { start: null, end: null }. */
  function parseTimeRange(time_range) {
    if (!time_range) return { start: null, end: null };
    var m = String(time_range).match(/(\d{1,2}):(\d{2})\s*[–-]\s*(\d{1,2}):(\d{2})/);
    if (!m) return { start: null, end: null };
    function pad(s) { return s.length === 1 ? '0' + s : s; }
    return {
      start: pad(m[1]) + ':' + m[2] + ':00',
      end:   pad(m[3]) + ':' + m[4] + ':00',
    };
  }

  function OverviewTab(props) {
    var topic = props.topic;
    var SafetyFlagRow = window.FieldSight.SafetyFlagRow;
    var ActionItemRow = window.FieldSight.ActionItemRow;

    var actions = topic.action_items || [];
    var flags   = topic.safety_flags || [];
    var deciss  = topic.key_decisions || [];

    return React.createElement('div', { className: 'fs-topic-detail__overview' },
      topic.summary ? React.createElement('p', {
        className: 'fs-topic-detail__summary',
      }, topic.summary) : null,

      deciss.length > 0
        ? React.createElement('div', { className: 'fs-topic-detail__section' },
            React.createElement('div', { className: 'fs-topic-detail__section-label' },
              'Key decisions'),
            React.createElement('ul', { className: 'fs-topic-detail__decisions' },
              deciss.map(function (d, i) {
                return React.createElement('li', { key: i }, d);
              }),
            ),
          )
        : null,

      actions.length > 0
        ? React.createElement('div', { className: 'fs-topic-detail__section' },
            React.createElement('div', { className: 'fs-topic-detail__section-label' },
              'Action items'),
            actions.map(function (a, idx) {
              var key = topic.topic_id + '_' + idx;
              var st  = (props.actionState || {})[key] || {};
              return React.createElement(ActionItemRow, {
                key:            key,
                date:           props.date,
                topicId:        topic.topic_id,
                actionIndex:    idx,
                action:         a,
                initialChecked: !!st.checked,
                checkedBy:      st.checked_by,
              });
            }),
          )
        : null,

      flags.length > 0
        ? React.createElement('div', { className: 'fs-topic-detail__section' },
            React.createElement('div', {
              className: 'fs-topic-detail__section-label fs-topic-detail__section-label--danger',
            }, 'Safety flags'),
            flags.map(function (f, i) {
              return React.createElement(SafetyFlagRow, { key: i, flag: f });
            }),
          )
        : null,
    );
  }

  /* Body for a meeting topic's Overview tab — different schema than the
     daily report (BACKEND-CONTEXT §5.4): action_items.owner instead of
     responsible, key_decisions are objects with rationale + decided_by,
     no safety_flags, plus open_questions. */
  function MeetingOverviewTab(props) {
    var Badge = window.FieldSight.Badge;
    var topic = props.topic;

    var actions  = topic.action_items   || [];
    var deciss   = topic.key_decisions  || [];
    var openQs   = topic.open_questions || [];

    return React.createElement('div', { className: 'fs-topic-detail__overview' },
      topic.summary ? React.createElement('p', {
        className: 'fs-topic-detail__summary',
      }, topic.summary) : null,

      deciss.length > 0
        ? React.createElement('div', { className: 'fs-topic-detail__section' },
            React.createElement('div', { className: 'fs-topic-detail__section-label' },
              'Key decisions'),
            React.createElement('div', { className: 'fs-meeting-decisions' },
              deciss.map(function (d, i) {
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

      actions.length > 0
        ? React.createElement('div', { className: 'fs-topic-detail__section' },
            React.createElement('div', { className: 'fs-topic-detail__section-label' },
              'Action items'),
            React.createElement('div', { className: 'fs-meeting-actions' },
              actions.map(function (a, i) {
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
                  a.priority ? React.createElement(Badge, {
                    tone:    MEETING_PRIORITY_TONE[p] || 'neutral',
                    size:    'sm', variant: 'outline',
                  }, a.priority.charAt(0).toUpperCase() + a.priority.slice(1)) : null,
                );
              }),
            ),
            /* P-10 — read-only caption mirrors the MeetingTopicCard. */
            React.createElement('div', { className: 'fs-meeting-actions__readonly' },
              'Read-only — meeting actions are tracked in the minutes,',
              ' not the daily-action audit log.'),
          )
        : null,

      openQs.length > 0
        ? React.createElement('div', { className: 'fs-topic-detail__section' },
            React.createElement('div', { className: 'fs-topic-detail__section-label' },
              'Open questions'),
            React.createElement('ul', { className: 'fs-topic-detail__decisions' },
              openQs.map(function (q, i) {
                return React.createElement('li', { key: i }, q);
              }),
            ),
          )
        : null,
    );
  }

  function TimelineRightDetail(props) {
    var fs       = window.FieldSight;
    var IconBtn  = fs.IconButton;
    var Badge         = fs.Badge;
    var CategoryBadge = fs.CategoryBadge;

    var refTab = React.useState('overview');
    var tab    = refTab[0];
    var setTab = refTab[1];

    var refActions = React.useState({});
    var setActions = refActions[1];

    var sel = props.selectedItem;
    var isMeeting = sel && sel.kind === 'meeting_topic';
    var isDaily   = sel && sel.kind === 'topic';

    /* Load actions audit state once per (date) — only relevant for
       daily-report topics; meeting actions are read-only. */
    React.useEffect(function () {
      if (!isDaily || !sel || !sel.date) return;
      var cancelled = false;
      window.FS.api.actions.getActions(sel.date).then(function (res) {
        if (!cancelled) setActions(res.actions || {});
      });
      return function () { cancelled = true; };
    }, [isDaily, sel && sel.date]);

    /* Sprint 6.7.1 — same bus subscription as MiddleColumn but for
       this right-detail's action map. Keeps the OverviewTab's
       ActionItemRows synced when the user toggles in the middle
       column. */
    React.useEffect(function () {
      if (!isDaily || !sel || !sel.date) return undefined;
      var bus = window.FS && window.FS.actionsBus;
      if (!bus) return undefined;
      var myDate = sel.date;
      return bus.subscribe(function (payload) {
        if (!payload || payload.date !== myDate) return;
        setActions(function (cur) {
          var key = payload.topic_id + '_' + payload.action_index;
          var next = Object.assign({}, cur || {});
          next[key] = {
            checked:    !!payload.checked,
            checked_by: payload.checked_by,
            checked_at: payload.checked_at,
          };
          return next;
        });
      });
    }, [isDaily, sel && sel.date]);

    /* Reset to overview tab whenever a new topic is selected. */
    React.useEffect(function () {
      setTab('overview');
    }, [sel && sel.id]);

    if (!isDaily && !isMeeting) {
      return React.createElement('div', {
        className: 'fs-topic-detail__placeholder',
      },
        React.createElement('div', { className: 'fs-topic-detail__placeholder-title' },
          'Select a topic'),
        React.createElement('div', { className: 'fs-topic-detail__placeholder-body' },
          'Click any topic in the timeline to view its full detail.'),
      );
    }

    var topic = sel.topic;
    var range = parseTimeRange(topic.time_range);

    var TranscriptList = fs.TranscriptList;
    var AudioPlaylist  = fs.AudioPlaylist;
    var VideoPlayer    = fs.VideoPlayer;
    var PhotoGrid      = fs.PhotoGrid;
    var AskChat        = fs.AskChat;

    var mediaProps = {
      date:  sel.date,
      user:  sel.user || (sel.user_name && window.FS.api.folderName(sel.user_name)),
      start: range.start,
      end:   range.end,
    };

    /* Tabs + body content depend on the topic kind. Meeting topics
       skip media tabs — meeting recordings aren't part of the daily
       report's recording bundle (BACKEND-CONTEXT §5.4 / §5.5). */
    var TABS = isMeeting ? MEETING_TABS : DAILY_TABS;

    var bodyByTab;
    if (isMeeting) {
      bodyByTab = {
        overview: React.createElement(MeetingOverviewTab, { topic: topic }),
        ask:      AskChat ? React.createElement(AskChat, {
          date:        sel.date,
          user:        mediaProps.user,
          scope:       'both',  /* meeting transcripts may sit alongside; widen scope */
          topic_id:    topic.topic_id,
          placeholder: 'Ask about this meeting topic…',
          suggestions: [
            'What was decided?',
            'Who owns the follow-ups?',
            'Any open questions?',
          ],
        }) : null,
      };
    } else {
      bodyByTab = {
        overview:   React.createElement(OverviewTab, {
          topic: topic, date: sel.date, actionState: refActions[0],
        }),
        transcript: TranscriptList ? React.createElement(TranscriptList,
          Object.assign({}, mediaProps, {
            participants: topic.participants || [],
          })) : null,
        audio:      AudioPlaylist  ? React.createElement(AudioPlaylist,  mediaProps) : null,
        video:      VideoPlayer    ? React.createElement(VideoPlayer,    mediaProps) : null,
        photos:     PhotoGrid      ? React.createElement(PhotoGrid, {
          photos:          topic.related_photos || [],
          userDisplayName: sel.user_name,
          date:            sel.date,
        }) : null,
        ask:        AskChat        ? React.createElement(AskChat, {
          date:        sel.date,
          user:        mediaProps.user,
          scope:       'transcript',
          topic_id:    topic.topic_id,
          placeholder: 'Ask about this topic…',
          suggestions: [
            'What was decided?',
            'Who is responsible for follow-ups?',
            'Were any risks flagged?',
          ],
        }) : null,
      };
    }

    /* Status pill (meeting only) — sits next to the category badge. */
    var statusPill = isMeeting && topic.status
      ? React.createElement(Badge, {
          tone: MEETING_STATUS_TONE[topic.status] || 'neutral',
          size: 'sm', variant: 'outline',
        }, MEETING_STATUS_LABEL[topic.status] || topic.status)
      : null;

    return React.createElement('div', {
      className: 'fs-topic-detail' + (isMeeting ? ' fs-topic-detail--meeting' : ''),
    },

      /* Header */
      React.createElement('div', { className: 'fs-topic-detail__header' },
        React.createElement('div', { className: 'fs-topic-detail__header-main' },
          React.createElement('div', { className: 'fs-topic-detail__time' },
            topic.time_range || '—'),
          React.createElement('h2', { className: 'fs-topic-detail__title' },
            topic.topic_title || '(untitled)'),
          React.createElement('div', { className: 'fs-topic-detail__metaline' },
            CategoryBadge ? React.createElement(CategoryBadge, {
              category: topic.category,
            }) : null,
            statusPill,
            (topic.participants || []).length
              ? React.createElement('span', {
                  className: 'fs-topic-detail__participants',
                }, (topic.participants || []).join(' · '))
              : null,
          ),
        ),
        IconBtn ? React.createElement(IconBtn, {
          icon: 'x', ariaLabel: 'Close detail', size: 'sm',
          onClick: function () { if (props.onClose) props.onClose(); },
        }) : null,
      ),

      /* Tab strip */
      React.createElement('div', {
        className: 'fs-topic-detail__tabs',
        role:      'tablist',
      },
        TABS.map(function (t) {
          var active = t.key === tab;
          return React.createElement('button', {
            key:           t.key,
            type:          'button',
            role:          'tab',
            'aria-selected': active,
            className:     'fs-topic-detail__tab' + (active ? ' fs-topic-detail__tab--active' : ''),
            onClick:       function () { setTab(t.key); },
          }, t.label);
        }),
      ),

      /* Body */
      React.createElement('div', { className: 'fs-topic-detail__body' },
        bodyByTab[tab],
      ),
    );
  }

  /* ---------- Register -------------------------------------------------- */

  if (!window.FieldSight) window.FieldSight = {};
  if (!window.FieldSight.PAGES) window.FieldSight.PAGES = {};
  window.FieldSight.PAGES['/timeline'] = {
    Middle: TimelineMiddleColumn,
    Right:  TimelineRightDetail,
  };

})();
