/* ==========================================================================
   FieldSight API · Transcripts — BACKEND-CONTEXT §4.5
   --------------------------------------------------------------------------
   GET /api/transcripts?date=&user=&start=HH:MM:SS&end=HH:MM:SS

   Backed in Sprint 2.3 by fixtures.media[date][folder].speaker_segments.
   Filtering: 60s buffer on each side per the spec — segments whose
   [start, end] overlap [windowStart-60, windowEnd+60] are kept.
   ========================================================================== */

(function () {
  'use strict';

  /* HH:MM[:SS] string → seconds since midnight. Returns null for blanks. */
  function toSeconds(t) {
    if (!t) return null;
    var parts = String(t).split(':').map(Number);
    if (parts.length < 2) return null;
    var h = parts[0] || 0, m = parts[1] || 0, s = parts[2] || 0;
    return h * 3600 + m * 60 + s;
  }

  function lookup(date, user) {
    var media = window.FieldSight && window.FieldSight.fixtures && window.FieldSight.fixtures.media;
    if (!media || !media[date]) return null;
    var folder = window.FS.api.folderName(user || '');
    return media[date][folder] || null;
  }

  async function getTranscripts(opts) {
    opts = opts || {};
    if (!window.FS.api.useMocks) {
      return window.FS.api.request('/transcripts', {
        params: { date: opts.date, user: opts.user, start: opts.start, end: opts.end },
      });
    }
    await window.FS.api.delay(120);

    var bundle = lookup(opts.date, opts.user);
    if (!bundle) {
      return {
        text: '', filtered_text: '',
        segments: [], speaker_segments: [],
        speakers: [], count: 0, speaker_count: 0,
        total_speaker_segments: 0,
      };
    }

    var startSec = toSeconds(opts.start);
    var endSec   = toSeconds(opts.end);
    var BUFFER   = 60;

    var allSeg = bundle.speaker_segments || [];
    var inRange = allSeg.slice();
    if (startSec != null && endSec != null) {
      inRange = allSeg.filter(function (s) {
        return s.end >= (startSec - BUFFER) && s.start <= (endSec + BUFFER);
      });
    }

    var speakers = [];
    inRange.forEach(function (s) {
      if (speakers.indexOf(s.speaker) === -1) speakers.push(s.speaker);
    });

    /* Match the segments[] (per source-file) shape — filter by time too. */
    var fileSegs = (bundle.segments || []).filter(function (f) {
      if (startSec == null) return true;
      var fileStart = f.time_seconds;
      var fileEnd   = f.time_seconds + 600; /* 10-min chunks */
      return fileEnd >= (startSec - BUFFER) && fileStart <= (endSec + BUFFER);
    });

    return {
      text:             inRange.map(function (s) { return s.text; }).join(' '),
      filtered_text:    inRange.map(function (s) { return s.text; }).join(' '),
      segments:         fileSegs,
      speaker_segments: inRange,
      speakers:         speakers,
      count:            fileSegs.length,
      speaker_count:    speakers.length,
      total_speaker_segments: inRange.length,
    };
  }

  window.FS.api.transcripts = { getTranscripts: getTranscripts };

})();
