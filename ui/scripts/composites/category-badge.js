/* ==========================================================================
   FieldSight CategoryBadge — Layer 5 composite
   --------------------------------------------------------------------------
   Opinionated wrapper over the Layer 4 Badge atom that maps a Daily Report
   topic.category (BACKEND-CONTEXT §5.1) onto a tone:

     safety   → danger   (status colour: blocked vs overdue, see CLAUDE.md)
     progress → info
     quality  → success
     <other>  → neutral

   Props:
     category  string — 'safety' | 'progress' | 'quality' | unknown
     size      'sm' | 'md' (default 'sm')

   Exported to:
     window.FieldSight.CategoryBadge
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  var TONE_BY_CATEGORY = {
    safety:   'danger',
    progress: 'info',
    quality:  'success',
  };

  function CategoryBadge(props) {
    var Badge    = window.FieldSight.Badge;
    var category = (props.category || 'general').toLowerCase();
    var tone     = TONE_BY_CATEGORY[category] || 'neutral';
    var size     = props.size || 'sm';

    var label = category.charAt(0).toUpperCase() + category.slice(1);

    return React.createElement(Badge, {
      tone:      tone,
      size:      size,
      variant:   'subtle',
      prefixDot: true,
      className: 'fs-category-badge fs-category-badge--' + category,
    }, label);
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.CategoryBadge = CategoryBadge;
})();
