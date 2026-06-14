/* ==========================================================================
   FieldSight Programme Schedule — Sprint 5.6 (cascade engine + CPM)
   --------------------------------------------------------------------------
   Pure module. Two responsibilities:

   1.  cascadeFromTask(leaves, task_id, deltaDays)
         Walk forward through the dependents graph and shift every
         downstream task's start + end by deltaDays. "Medium-depth
         cascade" per the Sprint 5 plan: chain-shift only — we do
         NOT recompute slack or attempt to leave a task in place
         when its non-shifted dependency could still satisfy it.
         Mathematically that means we sometimes shift more than
         strictly necessary, but it's predictable and matches what
         a user dragging a Gantt bar expects to see.

   2.  computeCriticalPath(leaves, programmeStartISO)
         Standard CPM forward-pass on the dependency DAG using each
         task's `duration_days`. Returns the set of `task_id`s with
         zero slack (i.e. on the longest-cumulative-duration path
         through the graph).

   Cycle detection (Kahn's topological sort) runs up front in BOTH
   functions. If a cycle is found the function logs `console.warn`
   and returns the input unchanged (cascade) or `[]` (CPM). The page
   reducer must NEVER deadlock here — Sprint 5.3 (delete) scrubs
   `depends_on` to keep this guarantee, but the engine is defensive
   in case bad fixture data slips in.

   Inlined date helpers (`addDaysISO`, `diffDaysISO`) so the module
   stays self-contained and node-importable without booting the
   FS.api stack. Same arithmetic as `scripts/api/index.js`.

   Exported to:
     window.FieldSight.programmeSchedule = {
       cascadeFromTask, computeCriticalPath,
       addDaysISO, diffDaysISO,   // exposed for tests + reducers
     }
   ========================================================================== */

/* global window */

(function () {
  'use strict';

  /* ---------- Date helpers (inlined, see file header) ----------------- */

  function addDaysISO(iso, n) {
    if (!iso || !n) return iso;
    var d = new Date(iso + 'T00:00:00Z');
    d.setUTCDate(d.getUTCDate() + n);
    return d.toISOString().slice(0, 10);
  }

  function diffDaysISO(a, b) {
    if (!a || !b) return 0;
    var ms = new Date(b + 'T00:00:00Z').getTime()
           - new Date(a + 'T00:00:00Z').getTime();
    return Math.round(ms / 86400000);
  }

  /* ---------- Topological sort (Kahn's) ------------------------------- */

  /* Returns { topo: [task_id...], hasCycle: bool }. When hasCycle is
     true, topo contains only the acyclic portion. */
  function topoSort(leaves) {
    var byId = {};
    leaves.forEach(function (t) { byId[t.task_id] = t; });

    /* in-degree counts only deps that resolve to a leaf in `leaves`;
       parent_id refs are intentionally excluded — the cascade only
       runs on leaves and orphaned deps shouldn't block sorting. */
    var indeg = {};
    leaves.forEach(function (t) { indeg[t.task_id] = 0; });
    leaves.forEach(function (t) {
      (t.depends_on || []).forEach(function (dep) {
        if (byId[dep]) indeg[t.task_id] += 1;
      });
    });

    var queue = leaves
      .filter(function (t) { return indeg[t.task_id] === 0; })
      .map(function (t) { return t.task_id; });

    var topo = [];
    while (queue.length) {
      var id = queue.shift();
      topo.push(id);
      leaves.forEach(function (t) {
        if ((t.depends_on || []).indexOf(id) !== -1) {
          indeg[t.task_id] -= 1;
          if (indeg[t.task_id] === 0) queue.push(t.task_id);
        }
      });
    }
    return { topo: topo, hasCycle: topo.length !== leaves.length };
  }

  /* ---------- cascadeFromTask ----------------------------------------- */

  function cascadeFromTask(leaves, task_id, deltaDays) {
    if (!leaves || !leaves.length || !deltaDays) return leaves;

    var sort = topoSort(leaves);
    if (sort.hasCycle) {
      console.warn('[programme-schedule] cycle detected; cascade skipped');
      return leaves;
    }

    /* Reverse adjacency: dependents[X] = [Y where Y.depends_on includes X]. */
    var dependents = {};
    leaves.forEach(function (t) {
      (t.depends_on || []).forEach(function (dep) {
        (dependents[dep] = dependents[dep] || []).push(t.task_id);
      });
    });

    /* Transitive forward closure from task_id. The trigger task itself
       is NOT shifted — the caller has already applied its patch. */
    var toShift = {};
    var stack   = (dependents[task_id] || []).slice();
    while (stack.length) {
      var id = stack.pop();
      if (toShift[id]) continue;
      toShift[id] = true;
      (dependents[id] || []).forEach(function (d) { stack.push(d); });
    }

    return leaves.map(function (t) {
      if (!toShift[t.task_id]) return t;
      return Object.assign({}, t, {
        start: addDaysISO(t.start, deltaDays),
        end:   addDaysISO(t.end,   deltaDays),
      });
    });
  }

  /* ---------- computeCriticalPath (CPM forward + backward) ----------- */

  /* Algorithm:
       1. Topological sort
       2. Forward pass: ES[v] = max(EF[u]+1 for u in deps(v)), or 0 if none
                        EF[v] = ES[v] + duration_days[v] - 1
       3. project_finish = max(EF over leaf-end tasks, i.e. tasks no
          one depends on)
       4. Backward pass: LF[v] = min(LS[s]-1 for s in successors(v)),
                         or project_finish if v is a leaf-end
                         LS[v] = LF[v] - duration[v] + 1
       5. Critical path = { v | ES[v] === LS[v] }
     ES/EF/LS/LF are integer day offsets from the programme start.
     Anchor: every task without deps starts at day 0 (the programme
     start), so the CP reflects the longest-duration chain
     irrespective of fixture-stored slack between unrelated branches. */
  function computeCriticalPath(leaves, programmeStartISO /* unused, kept for API */) {
    void programmeStartISO;
    if (!leaves || !leaves.length) return [];

    var sort = topoSort(leaves);
    if (sort.hasCycle) {
      console.warn('[programme-schedule] cycle detected; critical path = []');
      return [];
    }
    var topo = sort.topo;
    var byId = {};
    leaves.forEach(function (t) { byId[t.task_id] = t; });

    var ES = {}, EF = {};
    topo.forEach(function (id) {
      var t = byId[id];
      var deps = (t.depends_on || []).filter(function (d) { return byId[d]; });
      ES[id] = deps.length === 0
        ? 0
        : Math.max.apply(null, deps.map(function (d) { return EF[d] + 1; }));
      EF[id] = ES[id] + (t.duration_days || 1) - 1;
    });

    /* Identify leaf-end tasks (no successor) for project finish + LF anchor. */
    var hasSucc = {};
    leaves.forEach(function (t) {
      (t.depends_on || []).forEach(function (d) { hasSucc[d] = true; });
    });
    var projectFinish = Math.max.apply(null,
      topo.filter(function (id) { return !hasSucc[id]; })
          .map(function (id) { return EF[id]; })
    );

    /* Successor lookup. */
    var successors = {};
    leaves.forEach(function (t) {
      (t.depends_on || []).forEach(function (d) {
        (successors[d] = successors[d] || []).push(t.task_id);
      });
    });

    var LS = {}, LF = {};
    topo.slice().reverse().forEach(function (id) {
      var t = byId[id];
      var succ = successors[id] || [];
      LF[id] = succ.length === 0
        ? projectFinish
        : Math.min.apply(null, succ.map(function (s) { return LS[s] - 1; }));
      LS[id] = LF[id] - (t.duration_days || 1) + 1;
    });

    return topo.filter(function (id) { return ES[id] === LS[id]; });
  }

  /* ---------- computeFloats (Sprint 8.3.1) -------------------------------- */

  /* Re-runs the CPM forward + backward pass and returns a plain object
     { [task_id]: totalFloat } where totalFloat = LS - ES (days).
     Zero float = critical. Shares the same algorithm as
     computeCriticalPath but also exposes the non-zero values. */
  function computeFloats(leaves) {
    if (!leaves || !leaves.length) return {};

    var sort = topoSort(leaves);
    if (sort.hasCycle) {
      console.warn('[programme-schedule] cycle detected; floats skipped');
      return {};
    }
    var topo = sort.topo;
    var byId = {};
    leaves.forEach(function (t) { byId[t.task_id] = t; });

    var ES = {}, EF = {};
    topo.forEach(function (id) {
      var t    = byId[id];
      var deps = (t.depends_on || []).filter(function (d) { return byId[d]; });
      ES[id] = deps.length === 0
        ? 0
        : Math.max.apply(null, deps.map(function (d) { return EF[d] + 1; }));
      EF[id] = ES[id] + (t.duration_days || 1) - 1;
    });

    var hasSucc = {};
    leaves.forEach(function (t) {
      (t.depends_on || []).forEach(function (d) { hasSucc[d] = true; });
    });
    var projectFinish = Math.max.apply(null,
      topo.filter(function (id) { return !hasSucc[id]; })
          .map(function (id) { return EF[id]; })
    );

    var successors = {};
    leaves.forEach(function (t) {
      (t.depends_on || []).forEach(function (d) {
        (successors[d] = successors[d] || []).push(t.task_id);
      });
    });

    var LS = {}, LF = {};
    topo.slice().reverse().forEach(function (id) {
      var t    = byId[id];
      var succ = successors[id] || [];
      LF[id] = succ.length === 0
        ? projectFinish
        : Math.min.apply(null, succ.map(function (s) { return LS[s] - 1; }));
      LS[id] = LF[id] - (t.duration_days || 1) + 1;
    });

    var floats = {};
    topo.forEach(function (id) {
      floats[id] = Math.max(0, LS[id] - ES[id]);
    });
    return floats;
  }

  /* ---------- detectOverAllocations (Sprint 8.3.2) ----------------------- */

  /* Returns { [userId]: [dateISO, ...] } for every user+date pair where
     more than one in-progress task is assigned to the same person on the
     same calendar day (regardless of weekend / holidays — prototype
     scope). Only leaf tasks (status !== 'group') are considered. */
  function detectOverAllocations(tasks) {
    var leafTasks = (tasks || []).filter(function (t) {
      return t.status !== 'group' && t.start && t.end && t.status !== 'completed';
    });

    /* userDates: { userId: { dateISO: count } } */
    var userDates = {};

    leafTasks.forEach(function (t) {
      var assignees = t.assignees || [];
      if (!assignees.length) return;

      var cur = t.start;
      /* Limit enumeration to protect against runaway loops on bad data */
      var safety = 0;
      while (cur <= t.end && safety < 1000) {
        safety++;
        assignees.forEach(function (userId) {
          if (!userDates[userId]) userDates[userId] = {};
          userDates[userId][cur] = (userDates[userId][cur] || 0) + 1;
        });
        var d = new Date(cur + 'T00:00:00Z');
        d.setUTCDate(d.getUTCDate() + 1);
        cur = d.toISOString().slice(0, 10);
      }
    });

    var result = {};
    Object.keys(userDates).forEach(function (userId) {
      var overloaded = Object.keys(userDates[userId]).filter(function (date) {
        return userDates[userId][date] > 1;
      }).sort();
      if (overloaded.length > 0) result[userId] = overloaded;
    });
    return result;
  }

  /* ---------- Register --------------------------------------------------- */

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.programmeSchedule = {
    cascadeFromTask:        cascadeFromTask,
    computeCriticalPath:    computeCriticalPath,
    computeFloats:          computeFloats,
    detectOverAllocations:  detectOverAllocations,
    addDaysISO:             addDaysISO,
    diffDaysISO:            diffDaysISO,
  };
})();
