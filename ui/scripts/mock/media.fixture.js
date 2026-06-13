/* ==========================================================================
   FieldSight Fixtures · Recording media (transcripts / audio / video)
   --------------------------------------------------------------------------
   Per-(date, folder) media bundles consumed by the api modules in §4.5,
   §4.6 and §4.7 of BACKEND-CONTEXT.md. Fixture data spans the four
   topics in daily-report.fixture.js for Jarley_Trainor on 2026-04-29.

   Schema notes the fixture honours:
     • speaker_segments[].start / .end are absolute seconds since midnight
     • time_label format is HH:MM:SS (NZDT clock time)
     • spk_0 / spk_1 labels are NOT stable across files (BUG §8.6) —
       UI must colour by position-within-current-view, not globally
     • video segments include is_preview:true (H264 preview); originals
       (H265) are de-duped by the API — fixture only ships previews
     • offset_sec is the seek target inside the file (start - file_start)
     • absolute_start / absolute_end on audio segments are seconds since
       midnight (BACKEND-CONTEXT §4.6)
     • filenames carry critical metadata per BUG-01 — anchor regex AFTER
       the date when parsing them

   Exported to:
     window.FieldSight.fixtures.media[date][folder] = {
       speaker_segments,  segments,  audio,  video
     }
   ========================================================================== */

(function () {
  'use strict';

  /* HH:MM:SS → seconds since midnight. */
  function hms(h, m, s) {
    return h * 3600 + m * 60 + (s || 0);
  }
  function label(h, m, s) {
    function pad(n) { return n < 10 ? '0' + n : '' + n; }
    return pad(h) + ':' + pad(m) + ':' + pad(s || 0);
  }

  /* Speaker segments — diarized turns sorted by absolute time. spk_0 in
     this view is Jarley Trainor; the first time spk_1 appears it's
     Jack Gibson; spk_2 is David Barillaro. Real diarization wouldn't
     stabilise these labels across the day — this is fixture convenience. */
  var speaker_segments = [
    /* Topic 0 — 07:00–07:30 — Morning Safety Briefing */
    { speaker: 'spk_0', text: "Right team, gather in. Few things to cover before we kick off, mainly around fall protection on Block C scaffolding.",
      start: hms(7, 0, 12), end: hms(7, 0, 27), time_label: label(7, 0, 12), duration: 15 },
    { speaker: 'spk_1', text: "We've had a loose board reported on level two. I'll have it quarantined and replaced before next shift.",
      start: hms(7, 1, 5),  end: hms(7, 1, 18), time_label: label(7, 1, 5),  duration: 13 },
    { speaker: 'spk_0', text: "Good. Also there's a coiled hose by gate two creating a trip hazard — we need that rerouted along the fence line by nine.",
      start: hms(7, 4, 22), end: hms(7, 4, 38), time_label: label(7, 4, 22), duration: 16 },
    { speaker: 'spk_1', text: "I'll task Sarah on it. Replacement scaffold boards are ordered, eight thirty pickup.",
      start: hms(7, 12, 4), end: hms(7, 12, 14), time_label: label(7, 12, 4), duration: 10 },

    /* Topic 1 — 08:30–09:15 — Crane pre-start */
    { speaker: 'spk_2', text: "Crane operator just rang. Pre-start is pushed to nine. We need to re-coordinate the rebar offload around the new lift slot.",
      start: hms(8, 31, 8),  end: hms(8, 31, 24), time_label: label(8, 31, 8),  duration: 16 },
    { speaker: 'spk_0', text: "Move the offload to nine thirty. Brief the crane crew on tag-line positions and the exclusion zone.",
      start: hms(8, 33, 2),  end: hms(8, 33, 14), time_label: label(8, 33, 2),  duration: 12 },
    { speaker: 'spk_2', text: "Confirmed. I'll get the inspection sign-off paperwork done by nine thirty as well.",
      start: hms(8, 47, 51), end: hms(8, 48, 0),  time_label: label(8, 47, 51), duration: 9  },

    /* Topic 2 — 11:00–11:45 — Concrete pour */
    { speaker: 'spk_0', text: "South footing pour is on. Slump test ready, mix is on site.",
      start: hms(11, 2, 8),  end: hms(11, 2, 16), time_label: label(11, 2, 8),  duration: 8  },
    { speaker: 'spk_2', text: "Slump came in at ninety-five mil — within spec. Pour finished eleven thirty, going to north footing prep.",
      start: hms(11, 31, 0), end: hms(11, 31, 14), time_label: label(11, 31, 0), duration: 14 },
    { speaker: 'spk_0', text: "Strip forms at fourteen hundred once the initial set is reached. Ben will handle that.",
      start: hms(11, 38, 22), end: hms(11, 38, 33), time_label: label(11, 38, 22), duration: 11 },

    /* Topic 3 — 13:30–14:00 — Wind warning */
    { speaker: 'spk_0', text: "MetService alert — gusts to sixty-five from fourteen hundred. Tarps, edge protection, anything loose, lock it down.",
      start: hms(13, 31, 18), end: hms(13, 31, 32), time_label: label(13, 31, 18), duration: 14 },
    { speaker: 'spk_1', text: "Walking the perimeter now, tying off panels along the western boundary.",
      start: hms(13, 41, 2),  end: hms(13, 41, 11), time_label: label(13, 41, 2),  duration: 9  },
  ];

  /* Source-file metadata (one row per 10-min recording chunk, BACKEND-
     CONTEXT §4.5 segments[] — parallel to speaker_segments[]). */
  var segments = [
    { time: '07:00:00', time_seconds: hms(7, 0, 0),
      text: '(full file transcript — Topic 0 morning brief)',
      filtered_text: '(in-range portion)',
      filename: 'Benl1_2026-04-29_07-00-00.json',
      word_count: 612, in_range_count: 612, speaker_segment_count: 4 },
    { time: '08:30:00', time_seconds: hms(8, 30, 0),
      text: '(Topic 1 crane pre-start)',
      filtered_text: '(in-range portion)',
      filename: 'Benl1_2026-04-29_08-30-00.json',
      word_count: 488, in_range_count: 488, speaker_segment_count: 3 },
    { time: '11:00:00', time_seconds: hms(11, 0, 0),
      text: '(Topic 2 concrete pour)',
      filtered_text: '(in-range portion)',
      filename: 'Benl1_2026-04-29_11-00-00.json',
      word_count: 540, in_range_count: 540, speaker_segment_count: 3 },
    { time: '13:30:00', time_seconds: hms(13, 30, 0),
      text: '(Topic 3 wind warning)',
      filtered_text: '(in-range portion)',
      filename: 'Benl1_2026-04-29_13-30-00.json',
      word_count: 320, in_range_count: 320, speaker_segment_count: 2 },
  ];

  /* Audio segments — VAD chunks per topic (BACKEND-CONTEXT §4.6).
     Mock URLs are placeholder strings; the api module re-derives via
     FS.api.mockPresignedUrl on each call so callers see fresh URLs. */
  var audio = [
    { filename: 'Benl1_2026-04-29_07-00-12_off0_to60_srcwav.wav',
      absolute_start: hms(7, 0, 12),  absolute_end: hms(7, 1, 12),  duration: 60,
      time_label: label(7, 0, 12) },
    { filename: 'Benl1_2026-04-29_07-04-22_off0_to42_srcwav.wav',
      absolute_start: hms(7, 4, 22),  absolute_end: hms(7, 5, 4),   duration: 42,
      time_label: label(7, 4, 22) },
    { filename: 'Benl1_2026-04-29_08-31-08_off0_to55_srcwav.wav',
      absolute_start: hms(8, 31, 8),  absolute_end: hms(8, 32, 3),  duration: 55,
      time_label: label(8, 31, 8) },
    { filename: 'Benl1_2026-04-29_08-47-51_off0_to30_srcwav.wav',
      absolute_start: hms(8, 47, 51), absolute_end: hms(8, 48, 21), duration: 30,
      time_label: label(8, 47, 51) },
    { filename: 'Benl1_2026-04-29_11-02-08_off0_to70_srcwav.wav',
      absolute_start: hms(11, 2, 8),  absolute_end: hms(11, 3, 18), duration: 70,
      time_label: label(11, 2, 8) },
    { filename: 'Benl1_2026-04-29_11-31-00_off0_to48_srcwav.wav',
      absolute_start: hms(11, 31, 0), absolute_end: hms(11, 31, 48), duration: 48,
      time_label: label(11, 31, 0) },
    { filename: 'Benl1_2026-04-29_13-31-18_off0_to52_srcwav.wav',
      absolute_start: hms(13, 31, 18), absolute_end: hms(13, 32, 10), duration: 52,
      time_label: label(13, 31, 18) },
  ];

  /* Video segments — H264 previews only (BACKEND-CONTEXT §4.7 / §8.10).
     base_name strips the extension; offset_sec is the seek target inside
     the file (start - file_start). */
  var video = [
    { key: 'web_video/Jarley_Trainor/2026-04-29/Benl1_2026-04-29_07-00-00.mp4',
      filename: 'Benl1_2026-04-29_07-00-00.mp4',
      base_name: 'Benl1_2026-04-29_07-00-00',
      video_start_sec: hms(7, 0, 0), time_label: label(7, 0, 12),
      offset_sec: 12, size_mb: 38.4,
      is_preview: true, codec: 'h264' },
    { key: 'web_video/Jarley_Trainor/2026-04-29/Benl1_2026-04-29_08-30-00.mp4',
      filename: 'Benl1_2026-04-29_08-30-00.mp4',
      base_name: 'Benl1_2026-04-29_08-30-00',
      video_start_sec: hms(8, 30, 0), time_label: label(8, 31, 8),
      offset_sec: 68, size_mb: 41.7,
      is_preview: true, codec: 'h264' },
    { key: 'web_video/Jarley_Trainor/2026-04-29/Benl1_2026-04-29_11-00-00.mp4',
      filename: 'Benl1_2026-04-29_11-00-00.mp4',
      base_name: 'Benl1_2026-04-29_11-00-00',
      video_start_sec: hms(11, 0, 0), time_label: label(11, 2, 8),
      offset_sec: 128, size_mb: 48.2,
      is_preview: true, codec: 'h264' },
    { key: 'web_video/Jarley_Trainor/2026-04-29/Benl1_2026-04-29_13-30-00.mp4',
      filename: 'Benl1_2026-04-29_13-30-00.mp4',
      base_name: 'Benl1_2026-04-29_13-30-00',
      video_start_sec: hms(13, 30, 0), time_label: label(13, 31, 18),
      offset_sec: 78, size_mb: 26.5,
      is_preview: true, codec: 'h264' },
  ];

  if (!window.FieldSight) window.FieldSight = {};
  if (!window.FieldSight.fixtures) window.FieldSight.fixtures = {};
  if (!window.FieldSight.fixtures.media) window.FieldSight.fixtures.media = {};
  if (!window.FieldSight.fixtures.media['2026-04-29']) {
    window.FieldSight.fixtures.media['2026-04-29'] = {};
  }
  window.FieldSight.fixtures.media['2026-04-29'].Jarley_Trainor = {
    speaker_segments: speaker_segments,
    segments:         segments,
    audio:            audio,
    video:            video,
  };

})();
