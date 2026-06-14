/* ==========================================================================
   FieldSight Insights · Tag vocabulary + inference helpers
   --------------------------------------------------------------------------
   Sprint 9 (Track A.0). The /insights dashboard groups safety / quality
   issues by tag (PPE / fall-from-height / housekeeping / ...). The
   vocabulary is INTENTIONALLY CLOSED at 12 tags so PMs aren't drowning
   in 100+ ad-hoc labels — see PLAN §4 Q-S9-1.

   Two helpers:
     • TAG_VOCAB           — closed list, source of truth for /insights UI
     • inferTagsFromText(s)— heuristic for fixture rows without explicit
                             tags[]; backend will emit real tags eventually
     • resolveSubcontractor(who_raised) — name → subcontractor_id lookup
                             via fixtures.sites.users; used by aggregator

   Exported to:
     window.FS.insights.TAG_VOCAB
     window.FS.insights.inferTagsFromText
     window.FS.insights.resolveSubcontractor
     window.FS.insights.subcontractorById
   ========================================================================== */

(function () {
  'use strict';

  /* The closed 12 — labels are PM-facing strings; slugs are stable
     keys for filtering / persistence / backend handoff.

     Sprint 9.5.4 introduced per-tag colors. Sprint 9.5.6 swapped
     the raw status shades for theme-aware --fs-tag-{slug} tokens
     defined in tokens.css: deeper shades in light mode (less
     saturated → more sophisticated for dashboards) and softer
     pastels in dark mode (less stimulating against near-black
     surfaces). Each tag still has a UNIQUE hue across both
     themes. */
  var TAG_VOCAB = [
    { slug: 'ppe',                  label: 'PPE',                       tone: 'danger',  color: 'var(--fs-tag-ppe)'                  },
    { slug: 'fall_from_height',     label: 'Fall from height',          tone: 'danger',  color: 'var(--fs-tag-fall_from_height)'     },
    { slug: 'electrical',           label: 'Electrical',                tone: 'danger',  color: 'var(--fs-tag-electrical)'           },
    { slug: 'hazardous_substances', label: 'Hazardous substances',      tone: 'danger',  color: 'var(--fs-tag-hazardous_substances)' },
    { slug: 'lockout_tagout',       label: 'Lockout / tagout',          tone: 'danger',  color: 'var(--fs-tag-lockout_tagout)'       },
    { slug: 'housekeeping',         label: 'Housekeeping',              tone: 'warning', color: 'var(--fs-tag-housekeeping)'         },
    { slug: 'plant_machinery',      label: 'Plant & machinery',         tone: 'warning', color: 'var(--fs-tag-plant_machinery)'      },
    { slug: 'lifting',              label: 'Lifting operations',        tone: 'warning', color: 'var(--fs-tag-lifting)'              },
    { slug: 'traffic_pedestrian',   label: 'Traffic / pedestrian',      tone: 'warning', color: 'var(--fs-tag-traffic_pedestrian)'   },
    { slug: 'working_hot',          label: 'Hot works',                 tone: 'warning', color: 'var(--fs-tag-working_hot)'          },
    { slug: 'quality_workmanship',  label: 'Workmanship',               tone: 'info',    color: 'var(--fs-tag-quality_workmanship)'  },
    { slug: 'quality_compliance',   label: 'Compliance / spec',         tone: 'info',    color: 'var(--fs-tag-quality_compliance)'   },
  ];

  /* Lookup table for the inference heuristic. Each row: keyword
     regexes → tag slug. First match wins; we collect multiples too. */
  var INFERENCE_RULES = [
    { tag: 'fall_from_height',     pattern: /\b(scaffold|edge[\s-]?protection|harness|fall\sarrest|height|roof|lev(?:el)?\s?\d+|ladder)\b/i },
    { tag: 'ppe',                  pattern: /\b(ppe|hard\s?hat|hi[\s-]?viz|gloves|boots|goggles|hearing|harness)\b/i },
    { tag: 'lifting',              pattern: /\b(crane|lift|sling|rigging|hoist|telehandler|forklift)\b/i },
    { tag: 'plant_machinery',      pattern: /\b(excavator|loader|digger|dozer|plant|machinery|tractor|skid[\s-]?steer)\b/i },
    { tag: 'electrical',           pattern: /\b(electric|cable|power|rcd|live\s|switchboard|isolat)\b/i },
    { tag: 'hazardous_substances', pattern: /\b(solvent|chemical|hazmat|fume|dust\s|asbestos|silica)\b/i },
    { tag: 'traffic_pedestrian',   pattern: /\b(traffic|pedestrian|gate\s?\d|exclusion\s?zone|vehicle\smovement|walkway|hose|trip)\b/i },
    { tag: 'lockout_tagout',       pattern: /\b(lockout|tagout|isolation|permit[\s-]?to[\s-]?work|energy)\b/i },
    { tag: 'working_hot',          pattern: /\b(weld|cutting|grind|hot\s?work|spark|fire\s?watch)\b/i },
    { tag: 'housekeeping',         pattern: /\b(housekeeping|debris|clutter|loose\s|spill|tidy|stockpile)\b/i },
    { tag: 'quality_workmanship',  pattern: /\b(workmanship|finish|rework|defect|out[\s-]?of[\s-]?spec)\b/i },
    { tag: 'quality_compliance',   pattern: /\b(compliance|sign[\s-]?off|inspection|spec\b|standard|document)\b/i },
  ];

  /* Heuristic: scan observation + recommended_action text against the
     rules; return all matching slugs (deduped, max 3 to keep
     dashboard segments readable). Unmatched rows fall back to
     ['housekeeping'] (the most generic — beats null which would
     appear in the UI as "Untagged"). */
  function inferTagsFromText(observation, recommendedAction) {
    var text = ((observation || '') + ' ' + (recommendedAction || '')).toLowerCase();
    var hits = [];
    for (var i = 0; i < INFERENCE_RULES.length; i++) {
      var rule = INFERENCE_RULES[i];
      if (rule.pattern.test(text)) {
        if (hits.indexOf(rule.tag) < 0) hits.push(rule.tag);
        if (hits.length >= 3) break;
      }
    }
    return hits.length > 0 ? hits : ['housekeeping'];
  }

  /* Build a name → subcontractor_id index lazily from fixtures. */
  var _userIndex = null;
  var _subIndex  = null;

  function buildIndices() {
    var fx = (window.FieldSight && window.FieldSight.fixtures
      && window.FieldSight.fixtures.sites) || {};
    _userIndex = {};
    (fx.users || []).forEach(function (u) {
      if (u.name)        _userIndex[u.name.toLowerCase()] = u.subcontractor_id || null;
      if (u.folder_name) _userIndex[u.folder_name.toLowerCase()] = u.subcontractor_id || null;
    });
    _subIndex = {};
    (fx.subcontractors || []).forEach(function (s) {
      _subIndex[s.id] = s;
    });
  }

  function resolveSubcontractor(whoRaised) {
    if (_userIndex === null) buildIndices();
    if (!whoRaised) return null;
    return _userIndex[whoRaised.toLowerCase()] || null;
  }

  function subcontractorById(id) {
    if (_subIndex === null) buildIndices();
    return id ? (_subIndex[id] || null) : null;
  }

  /* Convenience: full label for a tag slug (used by chart legends). */
  function tagLabel(slug) {
    for (var i = 0; i < TAG_VOCAB.length; i++) {
      if (TAG_VOCAB[i].slug === slug) return TAG_VOCAB[i].label;
    }
    return slug;
  }

  if (!window.FS) window.FS = {};
  window.FS.insights = {
    TAG_VOCAB:             TAG_VOCAB,
    inferTagsFromText:     inferTagsFromText,
    resolveSubcontractor:  resolveSubcontractor,
    subcontractorById:     subcontractorById,
    tagLabel:              tagLabel,
  };

})();
