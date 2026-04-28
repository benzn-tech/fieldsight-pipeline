/* ==========================================================================
   FieldSight Mock Data — Sprint 1.6 fixture
   --------------------------------------------------------------------------
   Static data for wireframe validation. Sprint 2 will fetch the same
   shape from the real API.

   Exported to:
     window.FieldSight.MockData
   ========================================================================== */

(function () {
  'use strict';

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

    urgent: [
      {
        id: 'u_001',
        title: 'Crane inspection — Block 4',
        badgeLabel: 'Overdue by 2h',
        badgeTone: 'danger',
        body: 'Booked 7:00 AM, status not confirmed by operator.',
        kind: 'urgent',
      },
      {
        id: 'u_002',
        title: 'Wind warning — secure tarps',
        badgeLabel: 'Action by 14:00',
        badgeTone: 'warning',
        body: 'MetService alert: gusts to 65 km/h after midday.',
        kind: 'urgent',
      },
    ],

    tasks: [
      { id: 't_001', title: 'Pour concrete — Block 4 footing',
        assignee: 'Jarley Trainor', status: 'In progress',
        statusTone: 'warning', dueTime: '08:30', kind: 'task' },
      { id: 't_002', title: 'Install edge protection L2',
        assignee: 'Ben Lin', status: 'In progress',
        statusTone: 'warning', dueTime: '10:15', kind: 'task' },
      { id: 't_003', title: 'Crane pre-start inspection',
        assignee: 'David Barillaro', status: 'Blocked',
        statusTone: 'danger', dueTime: '—', kind: 'task' },
      { id: 't_004', title: 'Site cleanup & waste removal',
        assignee: 'Sarah Chen', status: 'Open',
        statusTone: 'info', dueTime: '14:00', kind: 'task' },
      { id: 't_005', title: 'Daily toolbox talk — fall protection',
        assignee: 'Mike OBrien', status: 'Done',
        statusTone: 'success', dueTime: '07:00', kind: 'task' },
      { id: 't_006', title: 'Receive rebar delivery',
        assignee: 'Priya Sharma', status: 'Open',
        statusTone: 'info', dueTime: '15:30', kind: 'task' },
    ],

    activity: [
      { id: 'a_001', speaker: 'Jarley Trainor',
        snippet: 'Concrete crew finished the south footing, moving to north.',
        timeAgo: '12m ago', kind: 'activity' },
      { id: 'a_002', speaker: 'David Barillaro',
        snippet: 'Crane operator says inspection slot pushed to 9:00 AM.',
        timeAgo: '24m ago', kind: 'activity' },
      { id: 'a_003', speaker: 'Ben Lin',
        snippet: 'Edge protection materials short by 4 panels — ordering more.',
        timeAgo: '38m ago', kind: 'activity' },
      { id: 'a_004', speaker: 'Sarah Chen',
        snippet: 'Toolbox talk done, 8 attendees signed off.',
        timeAgo: '1h ago', kind: 'activity' },
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

  /* Lookup helper used by RightDetail */
  function findItemById(id) {
    if (!id) return null;
    var pools = [MOCK_TODAY.urgent, MOCK_TODAY.tasks, MOCK_TODAY.activity];
    for (var i = 0; i < pools.length; i++) {
      for (var j = 0; j < pools[i].length; j++) {
        if (pools[i][j].id === id) return pools[i][j];
      }
    }
    return null;
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.MockData = {
    TODAY: MOCK_TODAY,
    findItemById: findItemById,
  };

})();
