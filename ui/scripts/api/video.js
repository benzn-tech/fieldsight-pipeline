/* ==========================================================================
   FieldSight API · Video segments — BACKEND-CONTEXT §4.7
   --------------------------------------------------------------------------
   GET /api/video-segments?date=&user=&start=HH:MM:SS&end=HH:MM:SS

   Always prefer is_preview:true (H264, browser-playable). Originals
   may be H265 and won't play in Chrome — fixture only ships previews;
   a hardcoded filter mirrors the production API's de-dupe behaviour.
   ========================================================================== */

(function () {
  'use strict';

  function toSeconds(t) {
    if (!t) return null;
    var parts = String(t).split(':').map(Number);
    if (parts.length < 2) return null;
    return (parts[0] || 0) * 3600 + (parts[1] || 0) * 60 + (parts[2] || 0);
  }

  function lookup(date, user) {
    var media = window.FieldSight && window.FieldSight.fixtures && window.FieldSight.fixtures.media;
    if (!media || !media[date]) return null;
    var folder = window.FS.api.folderName(user || '');
    return media[date][folder] || null;
  }

  async function getVideoSegments(opts) {
    opts = opts || {};
    if (!window.FS.api.useMocks) {
      return window.FS.api.request('/video-segments', {
        params: { date: opts.date, user: opts.user, start: opts.start, end: opts.end },
      });
    }
    await window.FS.api.delay(120);

    var bundle = lookup(opts.date, opts.user);
    if (!bundle) return { videos: [], count: 0 };

    var startSec = toSeconds(opts.start);
    var endSec   = toSeconds(opts.end);
    var BUFFER   = 60;

    var rows = (bundle.video || []).filter(function (v) {
      if (!v.is_preview) return false;
      if (startSec == null) return true;
      /* Each preview file covers ~10 minutes; accept if the file overlaps
         the window with the spec's 60s buffer either side. */
      var fileStart = v.video_start_sec;
      var fileEnd   = v.video_start_sec + 600;
      return fileEnd >= (startSec - BUFFER) && fileStart <= (endSec + BUFFER);
    }).map(function (v) {
      return Object.assign({}, v, {
        url: window.FS.api.mockPresignedUrl(v.key),
      });
    });

    return { videos: rows, count: rows.length };
  }

  window.FS.api.video = { getVideoSegments: getVideoSegments };

})();
