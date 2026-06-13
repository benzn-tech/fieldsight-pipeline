/* ==========================================================================
   FieldSight Mock Data — Sprint 2.4 (Phase D — vestigial)
   --------------------------------------------------------------------------
   This file used to be the source of truth for the Today page. As of
   Phase D, today.js fetches its own data through FS.api.timeline and
   FS.api.actions, then derives the Today shape via FS.api.todayAdapter.

   What remains here:
     WEATHER  — there is no backend weather endpoint yet (PLAN §E mentions
                a future MetService integration). The weather indicator
                in the app shell still reads this fixture.

   Exported to:
     window.FieldSight.MockData = { WEATHER }

   Note: any code still referencing MockData.TODAY / findItemById /
   getRelated / getTimeline is stale — those moved into
   scripts/pages/today.js (and the Timeline page in scripts/pages/
   timeline.js handles its own state).
   ========================================================================== */

(function () {
  'use strict';

  var WEATHER = {
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

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.MockData = { WEATHER: WEATHER };

})();
