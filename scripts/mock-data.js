/* ==========================================================================
   FieldSight Mock Data — Sprint 1.6 fixture
   --------------------------------------------------------------------------
   Static data for wireframe validation. Sprint 2 will fetch the same
   shape from the real API.

   Schema notes:
     - Tasks split: myTasks (assignee = current user) / teamTasks (others)
     - Urgent items carry triggeredBy explaining the rule that flagged them
     - getRelated / getTimeline support the right-detail's Related and
       Timeline sections
     - Weather block carries 48h hourly + 7-day daily forecast (mock)

   Exported to:
     window.FieldSight.MockData
   ========================================================================== */

(function () {
  'use strict';

  /* The "current user" for Today filtering. In Sprint 2 this comes from
     window.AuthMock.currentUser; here we hardcode for fixture clarity. */
  var CURRENT_USER_NAME = 'Jarley Trainor';

  var MOCK_TODAY = {
    date: '2026-04-28',
    site: 'Ellesmere Block 4',

    morningBrief: {
      generatedAt: '5:42 AM',
      bullets: [
        'Pour at Block 4 completed at 11:30 PM, all crews stood down',
        'Crane inspection pending — booked 7:00 AM',
        'Wind warning expected from 14:00; secure tarps',
      ],
    },

    /* Urgent items — each carries `triggeredBy` so it's clear what rule
       surfaced this. Sprint 2's rule engine will produce the same shape. */
    urgent: [
      {
        id: 'u_001',
        title: 'Crane inspection — Block 4',
        badgeLabel: 'Overdue by 2h',
        badgeTone: 'danger',
        body: 'Booked 7:00 AM, status not confirmed by operator.',
        triggeredBy: 'Task overdue > 2h',
        kind: 'urgent',
      },
      {
        id: 'u_002',
        title: 'Wind warning — secure tarps',
        badgeLabel: 'Action by 14:00',
        badgeTone: 'warning',
        body: 'MetService alert: gusts to 65 km/h after midday.',
        triggeredBy: 'Weather alert · level=warning',
        kind: 'urgent',
      },
    ],

    /* My tasks (assignee = current user) */
    myTasks: [
      { id: 't_001', title: 'Pour concrete — Block 4 footing',
        assignee: 'Jarley Trainor', status: 'In progress',
        statusTone: 'warning', priority: 'High',
        dueTime: '08:30', kind: 'task' },
      { id: 't_005', title: 'Daily toolbox talk — fall protection (long task name to verify wrapping)',
        assignee: 'Jarley Trainor', status: 'Done',
        statusTone: 'success', priority: 'Medium',
        dueTime: '07:00', kind: 'task' },
      { id: 't_006', title: 'Receive rebar delivery — coordinate offload with crane crew',
        assignee: 'Jarley Trainor', status: 'Open',
        statusTone: 'info', priority: 'Medium',
        dueTime: '15:30', kind: 'task' },
    ],

    /* Team's tasks (assignee != current user) — visible because SM has
       task:manage:site permission */
    teamTasks: [
      { id: 't_002', title: 'Install edge protection L2',
        assignee: 'Ben Lin', status: 'In progress',
        statusTone: 'warning', priority: 'High',
        dueTime: '10:15', kind: 'task' },
      { id: 't_003', title: 'Crane pre-start inspection',
        assignee: 'David Barillaro', status: 'Blocked',
        statusTone: 'danger', priority: 'High',
        dueTime: '—', kind: 'task' },
      { id: 't_004', title: 'Site cleanup & waste removal',
        assignee: 'Sarah Chen', status: 'Open',
        statusTone: 'info', priority: 'Low',
        dueTime: '14:00', kind: 'task' },
    ],

    activity: [
      { id: 'a_001', speaker: 'Jarley Trainor',
        snippet: 'Concrete crew finished the south footing, moving to north.',
        timeAgo: '12m ago', channel: 'General', kind: 'activity' },
      { id: 'a_002', speaker: 'David Barillaro',
        snippet: 'Crane operator says inspection slot pushed to 9:00 AM.',
        timeAgo: '24m ago', channel: 'General', kind: 'activity' },
      { id: 'a_003', speaker: 'Ben Lin',
        snippet: 'Edge protection materials short by 4 panels — ordering more.',
        timeAgo: '38m ago', channel: 'Materials', kind: 'activity' },
      { id: 'a_004', speaker: 'Sarah Chen',
        snippet: 'Toolbox talk done, 8 attendees signed off.',
        timeAgo: '1h ago', channel: 'Safety', kind: 'activity' },
    ],

    onSite: [
      { id: 's_001', name: 'Jarley Trainor' },
      { id: 's_002', name: 'Ben Lin' },
      { id: 's_003', name: 'David Barillaro' },
      { id: 's_004', name: 'Sarah Chen' },
      { id: 's_005', name: 'Mike OBrien' },
      { id: 's_006', name: 'Priya Sharma' },
    ],
  };

  /* Mock weather data — 12h hourly strip + 7-day daily.
     Sprint 2 fetches from MetService; same shape. */
  var MOCK_WEATHER = {
    current: { temp: 17, condition: 'cloud-sun', wind: '12 km/h',
               humidity: '64%', conditionLabel: 'Partly cloudy' },
    hourly: [
      { hour: '13:00', temp: 17, condition: 'cloud-sun' },
      { hour: '14:00', temp: 18, condition: 'cloud-sun' },
      { hour: '15:00', temp: 18, condition: 'wind' },
      { hour: '16:00', temp: 17, condition: 'wind' },
      { hour: '17:00', temp: 16, condition: 'cloud' },
      { hour: '18:00', temp: 15, condition: 'cloud' },
      { hour: '19:00', temp: 14, condition: 'cloud-rain' },
      { hour: '20:00', temp: 13, condition: 'cloud-rain' },
      { hour: '21:00', temp: 12, condition: 'cloud-rain' },
      { hour: '22:00', temp: 11, condition: 'cloud' },
      { hour: '23:00', temp: 10, condition: 'cloud' },
      { hour: '00:00', temp: 9,  condition: 'cloud' },
    ],
    daily: [
      { day: 'Mon', date: '28 Apr', high: 18, low: 9,  condition: 'cloud-sun' },
      { day: 'Tue', date: '29 Apr', high: 16, low: 8,  condition: 'cloud-rain' },
      { day: 'Wed', date: '30 Apr', high: 15, low: 7,  condition: 'cloud-rain' },
      { day: 'Thu', date: '01 May', high: 17, low: 8,  condition: 'cloud-sun' },
      { day: 'Fri', date: '02 May', high: 19, low: 10, condition: 'sun' },
      { day: 'Sat', date: '03 May', high: 20, low: 11, condition: 'sun' },
      { day: 'Sun', date: '04 May', high: 18, low: 10, condition: 'cloud-sun' },
    ],
  };

  /* ---------- Lookups ---------------------------------------------------- */

  function findItemById(id) {
    if (!id) return null;
    var pools = [
      MOCK_TODAY.urgent,
      MOCK_TODAY.myTasks,
      MOCK_TODAY.teamTasks,
      MOCK_TODAY.activity,
    ];
    for (var i = 0; i < pools.length; i++) {
      for (var j = 0; j < pools[i].length; j++) {
        if (pools[i][j].id === id) return pools[i][j];
      }
    }
    return null;
  }

  /* Related items: kind-specific. Sprint 2 will derive from real
     relationships; here we hand-pick a few to validate the layout. */
  function getRelated(item) {
    if (!item) return [];

    if (item.kind === 'task') {
      /* Other tasks for same assignee */
      var allTasks = MOCK_TODAY.myTasks.concat(MOCK_TODAY.teamTasks);
      return allTasks
        .filter(function(t) { return t.id !== item.id && t.assignee === item.assignee; })
        .slice(0, 3)
        .map(function(t) {
          return { id: t.id,
                   title: t.title,
                   subtitle: t.status + ' · due ' + t.dueTime };
        });
    }

    if (item.kind === 'activity') {
      /* Other activity from same speaker */
      return MOCK_TODAY.activity
        .filter(function(a) { return a.id !== item.id && a.speaker === item.speaker; })
        .slice(0, 3)
        .map(function(a) {
          return { id: a.id,
                   title: a.snippet,
                   subtitle: a.timeAgo + ' · ' + a.channel };
        });
    }

    if (item.kind === 'urgent') {
      /* Other urgent items as 'sister alerts' */
      return MOCK_TODAY.urgent
        .filter(function(u) { return u.id !== item.id; })
        .slice(0, 3)
        .map(function(u) {
          return { id: u.id, title: u.title, subtitle: u.badgeLabel };
        });
    }

    return [];
  }

  /* Timeline: kind-specific event log */
  function getTimeline(item) {
    if (!item) return [];

    if (item.kind === 'task') {
      return [
        { label: 'Created',     actor: 'Jarley Trainor', time: 'Yesterday 6:42 PM' },
        { label: 'Assigned to ' + item.assignee, actor: 'Jarley Trainor', time: 'Yesterday 6:43 PM' },
        { label: 'Status: ' + item.status, actor: item.assignee, time: 'Today 7:15 AM' },
      ];
    }

    if (item.kind === 'urgent') {
      return [
        { label: 'Flagged urgent', actor: 'System', time: '12m ago' },
        { label: 'Triggered by · ' + (item.triggeredBy || 'manual'), actor: 'System', time: '12m ago' },
      ];
    }

    if (item.kind === 'activity') {
      return [
        { label: 'Captured', actor: item.speaker, time: item.timeAgo },
        { label: 'Transcribed', actor: 'AI · Whisper-NZ', time: 'just after capture' },
        { label: 'Tagged · ' + (item.channel || 'General'), actor: 'AI', time: 'just after capture' },
      ];
    }

    return [];
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.MockData = {
    TODAY: MOCK_TODAY,
    WEATHER: MOCK_WEATHER,
    findItemById: findItemById,
    getRelated: getRelated,
    getTimeline: getTimeline,
  };

})();
