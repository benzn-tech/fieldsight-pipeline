/* ==========================================================================
   FieldSight Fixtures · Meeting Minutes
   --------------------------------------------------------------------------
   Schema-faithful to BACKEND-CONTEXT §5.4 — generic business-meeting
   flavour, NOT site-inspection. Different field names from Daily Report
   on purpose:

     • action_items[*].owner          (not  responsible)
     • topics[*].key_decisions[*]      ARE OBJECTS  ({decision, rationale,
                                       decided_by}), not strings
     • no  safety_flags  on a topic
     • topic.status        (decided | deferred | in_discussion | blocked)
     • topic.open_questions
     • follow_ups, next_steps, parking_lot at root

   Topic categories (§5.4):
     strategy | operations | finance | product | partnership |
     technical | hr | legal | general

   §5.5 — a date can have BOTH a daily_report.json AND a
   meeting_minutes.json. The Timeline page toggles between them.

   Exported to:
     window.FieldSight.fixtures.meetings[date][folder]
   ========================================================================== */

(function () {
  'use strict';

  var EN = '–';

  var MEETING_2026_04_29_JARLEY = {
    report_date:    '2026-04-29',
    report_type:    'meeting',
    user_name:      'Jarley Trainor',
    meeting_title:  'Q3 budget & programme review',
    site:           'SB1108 Ellesmere College',
    executive_summary: [
      'Q3 budget approved with reallocations to scaffold remediation and Block 4 site services.',
      'Block C programme slip (~5 working days) acknowledged; recovery plan tabled for next week.',
      'New rebar supplier RFQ deferred to May; current supplier extended on existing terms.',
      'Council inspection scheduled for Friday 03 May — sign-off package owners assigned.',
    ],
    topics: [
      {
        topic_id:    0,
        time_range:  '10:05 ' + EN + ' 10:32',
        topic_title: 'Q3 budget reallocation',
        category:    'finance',
        status:      'decided',
        participants: ['Jarley Trainor', 'Ben Lin', 'Sarah Chen'],
        summary:
          'Reviewed Q3 forecast against actuals. Scaffold remediation overrun (NZ$48k) absorbed via ' +
          'reallocation from contingency; Block 4 site services line increased by NZ$32k to cover the ' +
          'rebar coordination resource through end of quarter.',
        key_decisions: [
          {
            decision:   'Approve Q3 budget at NZ$3.2M with two reallocations',
            rationale:  'Within annual envelope; reallocations preserve programme intent',
            decided_by: 'Ben Lin',
          },
          {
            decision:   'Hold contingency reserve at 5% of remaining quarter spend',
            rationale:  'Mitigates against further weather-related delays',
            decided_by: 'Sarah Chen',
          },
        ],
        action_items: [
          { action:   'Submit reallocated Q3 budget for board notification',
            owner:    'Sarah Chen',  deadline: 'By Friday',  priority: 'high' },
          { action:   'Update site cost forecast tool with new line allocations',
            owner:    'Ben Lin',     deadline: 'Monday 06 May', priority: 'medium' },
        ],
        open_questions: [
          'Should Q4 materials be pre-bought given supplier price hold expiring?',
        ],
      },

      {
        topic_id:    1,
        time_range:  '10:35 ' + EN + ' 11:02',
        topic_title: 'Block C programme slip & recovery',
        category:    'operations',
        status:      'in_discussion',
        participants: ['Jarley Trainor', 'David Barillaro', 'Ben Lin'],
        summary:
          'Block C is running ~5 working days behind plan due to scaffold remediation and weather. ' +
          'Recovery options discussed: bringing forward the Block 4 polished concrete pour to free crew, ' +
          'or splitting the L2 cladding crew across both blocks. No decision tonight — recovery plan ' +
          'tabled for next Wednesday.',
        key_decisions: [
          {
            decision:   'Defer recovery decision to next week',
            rationale:  'Need labour capacity confirmation from cladding subcontractor first',
            decided_by: 'Jarley Trainor',
          },
        ],
        action_items: [
          { action:   'Confirm cladding crew availability for split deployment',
            owner:    'David Barillaro', deadline: 'Tomorrow', priority: 'high' },
          { action:   'Cost both recovery options for the next meeting',
            owner:    'Ben Lin',         deadline: 'Tuesday',  priority: 'medium' },
        ],
        open_questions: [
          'Will the scaffold remediation be cleared by Friday inspection?',
          'Can L2 cladding start ahead of full scaffold sign-off in Zone B?',
        ],
      },

      {
        topic_id:    2,
        time_range:  '11:05 ' + EN + ' 11:18',
        topic_title: 'Council inspection — Friday 03 May',
        category:    'general',
        status:      'decided',
        participants: ['Jarley Trainor', 'Jack Gibson'],
        summary:
          'Walk-through with Council inspector booked for 09:00 Friday. Sign-off package owners ' +
          'assigned across scaffold, edge protection, and waste management.',
        key_decisions: [
          {
            decision:   'Jarley walks the inspector; Jack supplies the documentation pack',
            rationale:  'Site manager / SHE coordinator split keeps inspection focused',
            decided_by: 'Jarley Trainor',
          },
        ],
        action_items: [
          { action:   'Assemble inspection sign-off pack',
            owner:    'Jack Gibson',     deadline: 'Thursday 02 May 16:00', priority: 'high' },
          { action:   'Confirm scaffold remediation sign-off prior to walk-through',
            owner:    'Jarley Trainor',  deadline: 'Thursday 02 May',       priority: 'high' },
        ],
        open_questions: [],
      },

      {
        topic_id:    3,
        time_range:  '11:20 ' + EN + ' 11:35',
        topic_title: 'New rebar supplier RFQ',
        category:    'partnership',
        status:      'deferred',
        participants: ['Jarley Trainor', 'Ben Lin'],
        summary:
          'Discussion of moving to a second rebar supplier for resilience. Current supplier offered to ' +
          'extend existing terms through end of May — RFQ deferred to allow that window.',
        key_decisions: [
          {
            decision:   'Defer new-supplier RFQ to mid-May',
            rationale:  'Current supplier extension reduces immediate need; gives time to qualify alternates',
            decided_by: 'Ben Lin',
          },
        ],
        action_items: [
          { action:   'Draft RFQ scope for second-supplier pilot',
            owner:    'Ben Lin',     deadline: '15 May',     priority: 'low' },
        ],
        open_questions: [
          'Pre-qualification: which second suppliers meet our certification bar?',
        ],
      },
    ],

    follow_ups: [
      { item:       'Recovery plan for Block C',
        owner:      'Jarley Trainor',
        deadline:   'Next Wednesday',
        priority:   'high',
        depends_on: 'Cladding crew availability' },
      { item:       'Inspection sign-off package',
        owner:      'Jack Gibson',
        deadline:   'Thursday 02 May',
        priority:   'high',
        depends_on: 'Scaffold remediation closeout' },
    ],

    next_steps: [
      'Confirm cladding crew split tomorrow.',
      'Submit Q3 reallocated budget to board.',
      'Complete inspection sign-off pack by Thursday.',
    ],

    parking_lot: [
      'Roll-out of digital permit-to-work app across NZ sites.',
      'Long-lead Q4 procurement — review at end of May.',
    ],

    _report_metadata: {
      version:              'v3.5-meeting',
      generated_at:         '2026-04-29T11:50:00Z',
      generated_by:         'system',
      recordings_processed: 2,
      total_words:          2840,
      model:                'claude-sonnet-4-6',
      parse_success:        true,
    },
  };

  if (!window.FieldSight) window.FieldSight = {};
  if (!window.FieldSight.fixtures) window.FieldSight.fixtures = {};
  if (!window.FieldSight.fixtures.meetings) window.FieldSight.fixtures.meetings = {};
  if (!window.FieldSight.fixtures.meetings['2026-04-29']) {
    window.FieldSight.fixtures.meetings['2026-04-29'] = {};
  }
  window.FieldSight.fixtures.meetings['2026-04-29'].Jarley_Trainor = MEETING_2026_04_29_JARLEY;

})();
