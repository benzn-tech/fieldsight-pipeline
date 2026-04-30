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

  /* Default date for the prototype: the fixture's report date. Real
     deploy will pass `?date=` or default to today via FS.api.addDaysISO
     against a NZDT clock. */
  var DEFAULT_DATE = '2026-04-29';

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

    var subtitleParts = [];
    if (date) subtitleParts.push(formatDateLabel(date));
    if (user) subtitleParts.push(unfolder(user));
    if (report && report.site) subtitleParts.push(report.site);

    return React.createElement('div', {
      className: 'fs-timeline-page__header',
    },
      React.createElement('h2', { className: 'fs-timeline-page__title' },
        'Daily Report'),
      React.createElement('div', { className: 'fs-timeline-page__subtitle' },
        subtitleParts.join(' · ')),
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

    /* Resolve effective (date, user) honouring worker-forced-self rule. */
    var caller = (window.AuthMock && window.AuthMock.currentUser) || {};
    var date   = params.date || DEFAULT_DATE;
    var user   = params.user;
    if (caller.role === 'worker') user = callerFolder();
    if (!user && !isAdminLike(caller)) user = callerFolder();

    var refState = React.useState({ status: 'loading' });
    var state    = refState[0];
    var setState = refState[1];

    React.useEffect(function () {
      var cancelled = false;
      setState({ status: 'loading' });
      Promise.all([
        window.FS.api.timeline.getTimeline({ date: date, user: user }),
        window.FS.api.actions.getActions(date),
      ]).then(function (results) {
        if (cancelled) return;
        setState({ status: 'ok', report: results[0], actions: results[1].actions || {} });
      }).catch(function (err) {
        if (cancelled) return;
        setState({ status: 'error', error: err });
      });
      return function () { cancelled = true; };
    }, [date, user]);

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

    var report = state.report;

    /* Admin disambiguation shape: { date, available_users:[...] } */
    if (report && report.available_users) {
      return React.createElement('div', { className: 'fs-timeline-page' },
        React.createElement(PageHeader, { date: date, user: null }),
        React.createElement(AvailableUsersState, {
          date: date, users: report.available_users,
        }),
      );
    }

    /* No-report shape: 404-body OR { message, date } 200-body */
    if (!report || report._notFound) {
      return React.createElement('div', { className: 'fs-timeline-page' },
        React.createElement(PageHeader, { date: date, user: user }),
        React.createElement(NoReportState, {
          message: (report && report.message) || ('No report for ' + unfolder(user || '') + ' on ' + date),
        }),
      );
    }

    /* Happy path */
    var actionState = state.actions || {};
    var selectedTopicId = props.selectedItem && props.selectedItem.kind === 'topic'
      ? props.selectedItem.topic_id
      : null;

    return React.createElement('div', {
      className: 'fs-timeline-page',
    },
      React.createElement(PageHeader, { date: date, user: user, report: report }),
      React.createElement(ReportKpis, { report: report }),
      React.createElement(ExecutiveSummaryCard, {
        bullets: report.executive_summary,
      }),
      React.createElement('div', { className: 'fs-timeline-page__section-label' },
        'Topics'),
      React.createElement('div', { className: 'fs-timeline-page__topics' },
        (report.topics || []).map(function (topic) {
          return React.createElement(TopicCard, {
            key:         topic.topic_id,
            topic:       topic,
            date:        date,
            actionState: actionState,
            selected:    selectedTopicId === topic.topic_id,
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
    );
  }

  /* =====================================================================
     TimelineRightDetail — TopicDetail panel + media tabs
     ===================================================================== */

  var TABS = [
    { key: 'overview',   label: 'Overview' },
    { key: 'transcript', label: 'Transcript' },
    { key: 'audio',      label: 'Audio' },
    { key: 'video',      label: 'Video' },
    { key: 'photos',     label: 'Photos' },
  ];

  function ComingSoon(props) {
    return React.createElement('div', { className: 'fs-topic-detail__coming-soon' },
      React.createElement('div', { className: 'fs-topic-detail__coming-soon-title' },
        props.title),
      React.createElement('div', null,
        'Wired in Sprint 2.3 (Phase C). The api module ' + props.api + ' is already in place.'),
    );
  }

  function PhotosTab(props) {
    var topic    = props.topic;
    var date     = props.date;
    var userName = props.userName;
    var photos   = topic.related_photos || [];

    if (photos.length === 0) {
      return React.createElement('div', { className: 'fs-topic-detail__empty' },
        'No photos for this topic.');
    }

    /* Build the S3 key per BACKEND-CONTEXT §5.1 + §7 — folder = name with
       spaces → underscores. Real deploy fetches presigned URLs through
       FS.api.media.presignedUrl on each open (15-min expiry, §7). */
    return React.createElement('div', { className: 'fs-photo-grid' },
      photos.map(function (filename, i) {
        var key = window.FS.api.media.photoKey({
          userDisplayName: userName, date: date, filename: filename,
        });
        return React.createElement(PhotoCell, { key: i, s3Key: key, filename: filename });
      }),
    );
  }

  function PhotoCell(props) {
    var refUrl = React.useState(null);
    var url    = refUrl[0];
    var setUrl = refUrl[1];

    React.useEffect(function () {
      var cancelled = false;
      window.FS.api.media.presignedUrl(props.s3Key).then(function (res) {
        if (!cancelled) setUrl(res.url);
      });
      return function () { cancelled = true; };
    }, [props.s3Key]);

    return React.createElement('div', { className: 'fs-photo-grid__cell' },
      url
        ? React.createElement('img', {
            src: url, alt: props.filename, loading: 'lazy',
            className: 'fs-photo-grid__img',
            onError: function (e) {
              /* Mock URLs won't actually load — show a graceful placeholder. */
              e.currentTarget.style.display = 'none';
            },
          })
        : null,
      React.createElement('div', { className: 'fs-photo-grid__caption' },
        props.filename),
    );
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

  function TimelineRightDetail(props) {
    var fs       = window.FieldSight;
    var IconBtn  = fs.IconButton;
    var CategoryBadge = fs.CategoryBadge;

    var refTab = React.useState('overview');
    var tab    = refTab[0];
    var setTab = refTab[1];

    var refActions = React.useState({});
    var setActions = refActions[1];

    var sel = props.selectedItem;

    /* Load actions audit state once per (date) — needed for OverviewTab
       to render checkbox states aligned with the middle column. */
    React.useEffect(function () {
      if (!sel || !sel.date) return;
      var cancelled = false;
      window.FS.api.actions.getActions(sel.date).then(function (res) {
        if (!cancelled) setActions(res.actions || {});
      });
      return function () { cancelled = true; };
    }, [sel && sel.date]);

    /* Reset to overview tab whenever a new topic is selected. */
    React.useEffect(function () {
      setTab('overview');
    }, [sel && sel.id]);

    if (!sel || sel.kind !== 'topic') {
      return React.createElement('div', {
        className: 'fs-topic-detail__placeholder',
      },
        React.createElement('div', { className: 'fs-topic-detail__placeholder-title' },
          'Select a topic'),
        React.createElement('div', { className: 'fs-topic-detail__placeholder-body' },
          'Click any topic in the timeline to view its full detail and recordings.'),
      );
    }

    var topic = sel.topic;
    var bodyByTab = {
      overview:   React.createElement(OverviewTab, {
        topic: topic, date: sel.date, actionState: refActions[0],
      }),
      transcript: React.createElement(ComingSoon, {
        title: 'Transcript', api: 'FS.api.transcripts.getTranscripts',
      }),
      audio:      React.createElement(ComingSoon, {
        title: 'Audio segments', api: 'FS.api.audio.getAudioSegments',
      }),
      video:      React.createElement(ComingSoon, {
        title: 'Video segments', api: 'FS.api.video.getVideoSegments',
      }),
      photos:     React.createElement(PhotosTab, {
        topic: topic, date: sel.date, userName: sel.user_name,
      }),
    };

    return React.createElement('div', {
      className: 'fs-topic-detail',
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
