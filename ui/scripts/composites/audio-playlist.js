/* ==========================================================================
   FieldSight AudioPlaylist — Layer 5 composite
   --------------------------------------------------------------------------
   Renders /api/audio-segments (BACKEND-CONTEXT §4.6) as a playable list.

   Critical bug-trap (BACKEND-CONTEXT §8.3 / BUG-21):
     audioRef.current.paused is a REF read — it does NOT trigger a
     re-render. Driving Play/Pause UI off it leads to stale state.
     This component owns a single `playingFilename` state and updates
     it ONLY in response to <audio>'s native onplay / onpause / onended
     events. When the user clicks Play on row B while row A is playing,
     we let row A's onpause fire to clear it, then start row B.

   URLs are presigned and short-lived (15 min, BACKEND-CONTEXT §7) — we
   re-fetch on each open of the parent (component remount) so callers
   should not retain old urls in localStorage.

   Props:
     date    'YYYY-MM-DD'
     user    folder-name string
     start   HH:MM[:SS] window start
     end     HH:MM[:SS] window end

   Exported to:
     window.FieldSight.AudioPlaylist
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function fmtDuration(s) {
    if (s == null) return '';
    var m = Math.floor(s / 60);
    var r = Math.round(s - m * 60);
    return m + ':' + (r < 10 ? '0' + r : r);
  }

  function AudioPlaylist(props) {
    var date  = props.date;
    var user  = props.user;
    var start = props.start;
    var end   = props.end;

    var refState = React.useState({ status: 'loading', segments: [] });
    var state    = refState[0];
    var setState = refState[1];

    /* Single source of truth for which row is playing — driven by audio
       events, NOT by reading .paused (BUG-21). */
    var refPlay = React.useState(null);
    var playing    = refPlay[0];
    var setPlaying = refPlay[1];

    /* Filename → <audio> element ref. */
    var audioRefs = React.useRef({});

    React.useEffect(function () {
      var cancelled = false;
      setState({ status: 'loading', segments: [] });
      window.FS.api.audio.getAudioSegments({
        date: date, user: user, start: start, end: end,
      }).then(function (res) {
        if (cancelled) return;
        setState({ status: 'ok', segments: res.segments || [] });
      }).catch(function (err) {
        if (cancelled) return;
        setState({ status: 'error', error: err, segments: [] });
      });
      return function () { cancelled = true; };
    }, [date, user, start, end]);

    function onPlayClick(seg) {
      var refs = audioRefs.current;
      /* Pause whatever's currently playing — its onpause handler clears state. */
      Object.keys(refs).forEach(function (fn) {
        if (fn !== seg.filename && refs[fn] && !refs[fn].paused) {
          try { refs[fn].pause(); } catch (e) { /* noop */ }
        }
      });
      var el = refs[seg.filename];
      if (!el) return;
      if (el.paused) {
        var p = el.play();
        if (p && p.catch) p.catch(function (err) {
          /* Mock URLs won't actually load — this is expected in the
             prototype. Surface as a quiet log, not a thrown error. */
          console.warn('[AudioPlaylist] play() rejected (mock URL):', err && err.message);
        });
      } else {
        el.pause();
      }
    }

    if (state.status === 'loading') {
      return React.createElement('div', { className: 'fs-audio-playlist__loading' },
        'Loading audio…');
    }
    if (state.status === 'error') {
      return React.createElement('div', { className: 'fs-audio-playlist__empty' },
        'Could not load audio.');
    }
    if (state.segments.length === 0) {
      return React.createElement('div', { className: 'fs-audio-playlist__empty' },
        'No audio segments in this window.');
    }

    return React.createElement('div', { className: 'fs-audio-playlist' },
      state.segments.map(function (seg, i) {
        var isPlaying = playing === seg.filename;
        return React.createElement('div', {
          key: i,
          className: 'fs-audio-playlist__row' + (isPlaying ? ' fs-audio-playlist__row--playing' : ''),
        },
          React.createElement('button', {
            type:      'button',
            className: 'fs-audio-playlist__play',
            onClick:   function () { onPlayClick(seg); },
            'aria-label': isPlaying ? 'Pause segment' : 'Play segment',
          }, isPlaying ? '❚❚' : '▶'),

          React.createElement('div', { className: 'fs-audio-playlist__main' },
            React.createElement('div', { className: 'fs-audio-playlist__meta' },
              React.createElement('span', { className: 'fs-audio-playlist__time' },
                seg.time_label),
              React.createElement('span', { className: 'fs-audio-playlist__dur' },
                fmtDuration(seg.duration)),
            ),
            React.createElement('div', { className: 'fs-audio-playlist__filename' },
              seg.filename),
          ),

          React.createElement('audio', {
            ref:    function (el) { audioRefs.current[seg.filename] = el; },
            src:    seg.url,
            preload: 'none',
            onPlay:  function () { setPlaying(seg.filename); },
            onPause: function () {
              /* Only clear if THIS row was the active one. */
              setPlaying(function (cur) { return cur === seg.filename ? null : cur; });
            },
            onEnded: function () {
              setPlaying(function (cur) { return cur === seg.filename ? null : cur; });
            },
          }),
        );
      }),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.AudioPlaylist = AudioPlaylist;
})();
