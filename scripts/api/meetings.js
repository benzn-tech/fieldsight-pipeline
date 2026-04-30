/* ==========================================================================
   FieldSight API · Meeting Minutes — BACKEND-CONTEXT §5.4
   --------------------------------------------------------------------------
   The backend has no dedicated /api/meetings endpoint. Meeting minutes
   are fetched via the generic media presigner against either:

     reports/{date}/{user}/meeting_minutes.json
     meeting_minutes/{date}/{title}.json

   This module hides that detail behind a single async getter that
   matches the Daily Report's call style.

   Returns a meeting JSON or { _notFound:true, message, date }.

   In Sprint 2.8, fixtures.meetings[date][folder] is the source. When
   wired live, switch on FS.api.useMocks: when false, build the key,
   call FS.api.media.presignedUrl, then fetch the JSON behind the
   presigned URL and apply the same content-type guard as /api/timeline
   (BUG-20).
   ========================================================================== */

(function () {
  'use strict';

  function lookup(date, user) {
    var meetings = window.FieldSight && window.FieldSight.fixtures && window.FieldSight.fixtures.meetings;
    if (!meetings || !meetings[date]) return null;
    var folder = window.FS.api.folderName(user || '');
    return meetings[date][folder] || null;
  }

  async function getMeetingMinutes(opts) {
    opts = opts || {};
    await window.FS.api.delay(120);

    var caller = (window.AuthMock && window.AuthMock.currentUser) || {};
    var user = opts.user;
    if (caller.role === 'worker') user = window.FS.api.folderName(caller.name);

    var record = lookup(opts.date, user);
    if (record) return record;

    return {
      _notFound: true,
      message:   'No meeting minutes for ' + (user || '(unknown)') + ' on ' + opts.date,
      date:      opts.date,
    };
  }

  window.FS.api.meetings = { getMeetingMinutes: getMeetingMinutes };

})();
