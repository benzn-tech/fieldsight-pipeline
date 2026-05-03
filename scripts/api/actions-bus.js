/* ==========================================================================
   FieldSight · Actions event bus (Sprint 6.7.1)
   --------------------------------------------------------------------------
   A tiny pub/sub for action-item check-state synchronisation. Solves the
   problem where the same action_item appears twice (middle TopicCard +
   right detail OverviewTab) and toggling one didn't update the other.

   Each ActionItemRow:
     - Subscribes on mount
     - Emits on successful toggle
     - On incoming event whose key matches its own (date+topic+index),
       updates its local `checked` state to match server truth

   Each timeline parent state (state.actions in MiddleColumn,
   refActions in RightDetail) ALSO subscribes and updates its own slot
   so subsequent renders / remounts see fresh data.

   Lightweight by design — no dependency, no React context, no AppShell
   plumbing. Mirrors the no-build-step ethos of the prototype.

   Public API:
     window.FS.actionsBus.emit(payload)   → fires payload to all subscribers
     window.FS.actionsBus.subscribe(cb)   → returns unsubscribe fn

   Payload shape:
     { date, topic_id, action_index, checked, checked_by, checked_at }

   Key derivation (for matching across components):
     `${date}|${topic_id}_${action_index}`
   ========================================================================== */

(function () {
  'use strict';

  var subscribers = new Set();

  function emit(payload) {
    subscribers.forEach(function (cb) {
      try { cb(payload); }
      catch (e) { console.error('[actionsBus]', e); }
    });
  }

  function subscribe(cb) {
    subscribers.add(cb);
    return function () { subscribers.delete(cb); };
  }

  if (!window.FS) window.FS = {};
  window.FS.actionsBus = { emit: emit, subscribe: subscribe };

})();
