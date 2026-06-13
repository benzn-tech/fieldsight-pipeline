/* ==========================================================================
   FieldSight TranscriptList — Layer 5 composite
   --------------------------------------------------------------------------
   Renders speaker_segments from /api/transcripts (BACKEND-CONTEXT §4.5).

   Critical bug-trap (BACKEND-CONTEXT §8.6 / BUG 8.6):
     spk_0, spk_1 from Transcribe diarization are NOT stable across
     recording files. The same person may be spk_0 in one file and spk_2
     in another. We therefore colour speakers by POSITION WITHIN THE
     CURRENT VIEW — first unique label gets palette[0], next gets
     palette[1], etc. — and never persist a speaker→colour mapping
     globally.

   When the report supplies `participants`, we additionally attach human
   names to the first N labels seen (best-effort hint, NOT authoritative).

   Props:
     date          'YYYY-MM-DD'
     user          folder-name string (Jarley_Trainor)
     start         HH:MM[:SS] window start
     end           HH:MM[:SS] window end
     participants  string[] (optional) — names to overlay onto labels
     onJump        (segment) => void  — caller wires this to audio/video
                   playback if present

   Exported to:
     window.FieldSight.TranscriptList
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  /* Five-colour palette mapped by FIRST-APPEARANCE order in the current
     view. Six is enough for any single topic — diarization won't reach
     more in practice. */
  var SPEAKER_PALETTE = [
    { fg: '#1E40AF', bg: '#DBEAFE' }, /* info */
    { fg: '#15803D', bg: '#DCFCE7' }, /* success */
    { fg: '#B45309', bg: '#FEF3C7' }, /* warning */
    { fg: '#9A2A13', bg: '#FFE6D5' }, /* accent */
    { fg: '#6B21A8', bg: '#EDE9FE' }, /* purple */
    { fg: '#0F766E', bg: '#CCFBF1' }, /* teal */
  ];

  function TranscriptList(props) {
    var refState = React.useState({ status: 'loading', segments: [] });
    var state    = refState[0];
    var setState = refState[1];

    var date  = props.date;
    var user  = props.user;
    var start = props.start;
    var end   = props.end;

    React.useEffect(function () {
      var cancelled = false;
      setState({ status: 'loading', segments: [] });
      window.FS.api.transcripts.getTranscripts({
        date: date, user: user, start: start, end: end,
      }).then(function (res) {
        if (cancelled) return;
        setState({
          status:   'ok',
          segments: res.speaker_segments || [],
          speakers: res.speakers || [],
          counts: {
            files:    res.count,
            segments: res.total_speaker_segments,
            speakers: res.speaker_count,
          },
        });
      }).catch(function (err) {
        if (cancelled) return;
        setState({ status: 'error', error: err, segments: [] });
      });
      return function () { cancelled = true; };
    }, [date, user, start, end]);

    if (state.status === 'loading') {
      return React.createElement('div', { className: 'fs-transcript-list__loading' },
        'Loading transcript…');
    }
    if (state.status === 'error') {
      return React.createElement('div', { className: 'fs-transcript-list__empty' },
        'Could not load transcript.');
    }
    if (state.segments.length === 0) {
      return React.createElement('div', { className: 'fs-transcript-list__empty' },
        'No speaker segments in this window.');
    }

    /* Build position-within-view label → palette index map. */
    var labelToIdx = {};
    state.segments.forEach(function (s) {
      if (!(s.speaker in labelToIdx)) {
        labelToIdx[s.speaker] = Object.keys(labelToIdx).length;
      }
    });

    /* Best-effort name overlay from participants[]. */
    var participantHint = {};
    var participants = props.participants || [];
    Object.keys(labelToIdx).forEach(function (label, i) {
      if (participants[i]) participantHint[label] = participants[i];
    });

    return React.createElement('div', { className: 'fs-transcript-list' },

      React.createElement('div', { className: 'fs-transcript-list__caption' },
        state.counts.segments + ' segments · '
          + state.counts.speakers + ' speakers · '
          + state.counts.files + ' source files'),

      state.segments.map(function (s, i) {
        var palette = SPEAKER_PALETTE[labelToIdx[s.speaker] % SPEAKER_PALETTE.length];
        var nameHint = participantHint[s.speaker];

        return React.createElement('div', {
          key: i, className: 'fs-transcript-list__row',
        },
          React.createElement('button', {
            type:    'button',
            className: 'fs-transcript-list__chip',
            style:   { color: palette.fg, background: palette.bg },
            onClick: function () { if (props.onJump) props.onJump(s); },
            title:   'Jump to ' + s.time_label,
          },
            React.createElement('span', {
              className: 'fs-transcript-list__chip-label',
            }, nameHint || s.speaker),
            React.createElement('span', {
              className: 'fs-transcript-list__chip-time',
            }, s.time_label),
          ),

          React.createElement('div', { className: 'fs-transcript-list__text' },
            s.text),
        );
      }),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.TranscriptList = TranscriptList;
})();
