/* ==========================================================================
   FieldSight VideoPlayer — Layer 5 composite
   --------------------------------------------------------------------------
   Plays the H264 preview clips from /api/video-segments (BACKEND-CONTEXT
   §4.7).

   Critical bug-traps:
     • Always prefer is_preview:true (BUG §8.10). Originals may be H265
       and won't play in Chrome. The api filters on the server side and
       this component double-checks defensively.
     • Use offset_sec to seek to the topic-relevant moment inside the
       file (start - file_start). Seek when metadata is loaded — before
       that the duration is unknown and seeking is a no-op.
     • <video preload="metadata"> — never preload a 200–300 MB file
       (BACKEND-CONTEXT §8.10).
     • URLs are presigned, expire 15 min — re-fetch on remount (no
       localStorage caching).

   Props:
     date    'YYYY-MM-DD'
     user    folder-name string
     start   HH:MM[:SS]
     end     HH:MM[:SS]

   Exported to:
     window.FieldSight.VideoPlayer
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function VideoPlayer(props) {
    var date  = props.date;
    var user  = props.user;
    var start = props.start;
    var end   = props.end;

    var refState = React.useState({ status: 'loading', videos: [] });
    var state    = refState[0];
    var setState = refState[1];

    var refIdx = React.useState(0);
    var idx    = refIdx[0];
    var setIdx = refIdx[1];

    var videoEl = React.useRef(null);

    React.useEffect(function () {
      var cancelled = false;
      setState({ status: 'loading', videos: [] });
      setIdx(0);
      window.FS.api.video.getVideoSegments({
        date: date, user: user, start: start, end: end,
      }).then(function (res) {
        if (cancelled) return;
        var previews = (res.videos || []).filter(function (v) { return v.is_preview; });
        setState({ status: 'ok', videos: previews });
      }).catch(function (err) {
        if (cancelled) return;
        setState({ status: 'error', error: err, videos: [] });
      });
      return function () { cancelled = true; };
    }, [date, user, start, end]);

    var current = state.videos[idx] || null;

    /* Seek to offset_sec once metadata is loaded; resets when current
       video changes. */
    function onLoadedMetadata() {
      var el = videoEl.current;
      if (!el || !current) return;
      try {
        if (current.offset_sec && el.duration && current.offset_sec < el.duration) {
          el.currentTime = current.offset_sec;
        }
      } catch (e) { /* noop */ }
    }

    if (state.status === 'loading') {
      return React.createElement('div', { className: 'fs-video-player__loading' },
        'Loading video…');
    }
    if (state.status === 'error') {
      return React.createElement('div', { className: 'fs-video-player__empty' },
        'Could not load video.');
    }
    if (!current) {
      return React.createElement('div', { className: 'fs-video-player__empty' },
        'No video preview in this window.');
    }

    return React.createElement('div', { className: 'fs-video-player' },

      React.createElement('video', {
        ref:        videoEl,
        src:        current.url,
        controls:   true,
        preload:    'metadata',
        playsInline: true,
        className:  'fs-video-player__media',
        onLoadedMetadata: onLoadedMetadata,
      }),

      React.createElement('div', { className: 'fs-video-player__meta' },
        React.createElement('span', { className: 'fs-video-player__time' },
          current.time_label),
        React.createElement('span', { className: 'fs-video-player__filename' },
          current.filename),
        React.createElement('span', { className: 'fs-video-player__size' },
          current.size_mb ? current.size_mb.toFixed(1) + ' MB' : ''),
      ),

      state.videos.length > 1 ? React.createElement('div', {
        className: 'fs-video-player__nav',
      },
        React.createElement('button', {
          type:     'button',
          disabled: idx === 0,
          onClick:  function () { setIdx(function (i) { return Math.max(0, i - 1); }); },
        }, '← Previous'),
        React.createElement('span', { className: 'fs-video-player__count' },
          (idx + 1) + ' of ' + state.videos.length),
        React.createElement('button', {
          type:     'button',
          disabled: idx >= state.videos.length - 1,
          onClick:  function () { setIdx(function (i) { return Math.min(state.videos.length - 1, i + 1); }); },
        }, 'Next →'),
      ) : null,
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.VideoPlayer = VideoPlayer;
})();
