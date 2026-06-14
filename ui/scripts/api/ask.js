/* ==========================================================================
   FieldSight API · Ask Agent — BACKEND-CONTEXT §4.12
   --------------------------------------------------------------------------
   POST /api/ask  body { date, user, question, scope?, topic_id? }
     → { answer, citations, model, ... }

   Stateless on the server (BACKEND-CONTEXT §10) — multi-turn must be
   reconstructed client-side and resent each call.
   ========================================================================== */

(function () {
  'use strict';

  async function ask(opts) {
    opts = opts || {};
    if (!window.FS.api.useMocks) {
      return window.FS.api.request('/ask', {
        method: 'POST',
        body: {
          date:     opts.date,
          user:     opts.user,
          question: opts.question,
          scope:    opts.scope,
          topic_id: opts.topic_id,
        },
      });
    }
    await window.FS.api.delay(400);
    return {
      answer:    'Mock answer for: "' + (opts.question || '') + '" (Sprint G wires the real Claude grounding).',
      citations: [],
      model:     'claude-haiku-4-5-20251001',
      scope:     opts.scope || 'both',
      _query:    opts,
    };
  }

  window.FS.api.ask = { ask: ask };

})();
