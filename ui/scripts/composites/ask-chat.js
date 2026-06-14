/* ==========================================================================
   FieldSight AskChat — Layer 5 composite (Sprint 2.7 / PLAN Phase G)
   --------------------------------------------------------------------------
   Q&A strip backed by /api/ask (BACKEND-CONTEXT §4.12).

   The backend is STATELESS — every question is independent and the API
   doesn't carry conversation memory (BACKEND-CONTEXT §10). The chat
   illusion is reconstructed client-side: we keep messages[] in local
   state for display, but each request sends only the one question.
   No prior turns are forwarded in the body.

   Two scoping modes:
     • scope='transcript' + topic_id → grounds answers to ONE topic's
       time range (used in TopicDetail's Ask tab)
     • scope='both' (default) → grounds to the whole report (transcript
       + report) — used by the per-report Ask card on /timeline

   Worker rule (BACKEND-CONTEXT §3, §8.5): the server forces user=self
   for workers. We pass the user param along and trust the API to
   override; no UI gating needed beyond that.

   Props:
     date          'YYYY-MM-DD'
     user          folder-name string (optional — server handles default)
     scope         'report' | 'transcript' | 'both'  (default 'both')
     topic_id      number | null
     placeholder   string for the input (e.g. "Ask about this topic…")
     suggestions   string[] of pre-canned questions (clickable chips)
     compact       boolean — render in a tighter layout for sidebars

   Exported to:
     window.FieldSight.AskChat
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function AskChat(props) {
    var date     = props.date;
    var user     = props.user;
    var scope    = props.scope || 'both';
    var topic_id = props.topic_id != null ? props.topic_id : null;

    /* messages: [{ role: 'user'|'assistant', text, citations?, model? }] */
    var refMsgs = React.useState([]);
    var msgs    = refMsgs[0];
    var setMsgs = refMsgs[1];

    var refQ = React.useState('');
    var q    = refQ[0];
    var setQ = refQ[1];

    var refBusy = React.useState(false);
    var busy    = refBusy[0];
    var setBusy = refBusy[1];

    var listRef = React.useRef(null);

    /* Auto-scroll the message list to the bottom whenever it grows. */
    React.useEffect(function () {
      var el = listRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    }, [msgs.length, busy]);

    /* When scope keys change (e.g. user switched topics), drop history
       since prior context no longer applies. */
    React.useEffect(function () {
      setMsgs([]);
    }, [date, user, scope, topic_id]);

    function send(question) {
      if (!question || busy) return;
      var userMsg = { role: 'user', text: question };
      setMsgs(function (m) { return m.concat([userMsg]); });
      setQ('');
      setBusy(true);

      window.FS.api.ask.ask({
        date:     date,
        user:     user,
        scope:    scope,
        topic_id: topic_id,
        question: question,
      }).then(function (res) {
        setMsgs(function (m) { return m.concat([{
          role:      'assistant',
          text:      res.answer || '',
          citations: res.citations || [],
          model:     res.model,
        }]); });
      }).catch(function (err) {
        setMsgs(function (m) { return m.concat([{
          role:  'assistant',
          text:  'Could not reach the agent. ' + (err && err.message || ''),
          error: true,
        }]); });
      }).then(function () {
        setBusy(false);
      });
    }

    function onSubmit(e) {
      if (e) e.preventDefault();
      var trimmed = (q || '').trim();
      if (!trimmed) return;
      send(trimmed);
    }

    var className = 'fs-ask-chat' + (props.compact ? ' fs-ask-chat--compact' : '');

    return React.createElement('div', { className: className },

      /* Suggestions row — only shown while history is empty. */
      props.suggestions && props.suggestions.length > 0 && msgs.length === 0
        ? React.createElement('div', { className: 'fs-ask-chat__suggestions' },
            props.suggestions.map(function (s, i) {
              return React.createElement('button', {
                key: i, type: 'button',
                className: 'fs-ask-chat__suggestion',
                onClick:   function () { send(s); },
                disabled:  busy,
              }, s);
            })
          )
        : null,

      /* Message log */
      React.createElement('div', {
        className: 'fs-ask-chat__messages',
        ref:       listRef,
      },
        msgs.length === 0 && (!props.suggestions || props.suggestions.length === 0)
          ? React.createElement('div', { className: 'fs-ask-chat__empty' },
              'Ask anything grounded in this ' + (topic_id != null ? 'topic.' : 'report.'))
          : null,

        msgs.map(function (m, i) {
          return React.createElement('div', {
            key: i,
            className: 'fs-ask-chat__msg fs-ask-chat__msg--' + m.role
              + (m.error ? ' fs-ask-chat__msg--error' : ''),
          },
            React.createElement('div', { className: 'fs-ask-chat__msg-text' },
              m.text),
            m.citations && m.citations.length > 0
              ? React.createElement('div', { className: 'fs-ask-chat__citations' },
                  'Citations: ' + m.citations.length)
              : null,
            m.role === 'assistant' && m.model
              ? React.createElement('div', { className: 'fs-ask-chat__model' },
                  m.model)
              : null,
          );
        }),

        busy ? React.createElement('div', {
          className: 'fs-ask-chat__msg fs-ask-chat__msg--assistant fs-ask-chat__msg--pending',
        },
          React.createElement('span', { className: 'fs-ask-chat__pending-dot' }),
          React.createElement('span', { className: 'fs-ask-chat__pending-dot' }),
          React.createElement('span', { className: 'fs-ask-chat__pending-dot' }),
        ) : null,
      ),

      /* Input */
      React.createElement('form', {
        className: 'fs-ask-chat__form',
        onSubmit:  onSubmit,
      },
        React.createElement('input', {
          type:      'text',
          className: 'fs-ask-chat__input',
          placeholder: props.placeholder || 'Ask the agent…',
          value:     q,
          onChange:  function (e) { setQ(e.target.value); },
          disabled:  busy,
        }),
        React.createElement('button', {
          type:      'submit',
          className: 'fs-ask-chat__send',
          disabled:  busy || !q.trim(),
          'aria-label': 'Send question',
        }, busy ? '…' : 'Ask'),
      ),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.AskChat = AskChat;
})();
