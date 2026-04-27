/* ==========================================================================
   FieldSight Badge + NotificationDot — Layer 4 display atoms
   --------------------------------------------------------------------------
   Tonal-not-loud by default. Subtle variant uses soft backgrounds (100
   shade) with dark text (700/800 shade). Solid variant (500/600 bg +
   white text) is reserved for critical/urgent states.

   Two distinct dot patterns:
     - dot={true}        → standalone label-less circle (for indicators)
     - prefixDot={true}  → leading dot BEFORE label (for status badges)

   API:
     <Badge tone leftIcon> children </Badge>
     <Badge dot tone />                               (no children)
     <Badge prefixDot tone> Active </Badge>
     <NotificationDot count={number} />

   Exported to:
     window.FieldSight.Badge
     window.FieldSight.NotificationDot
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function classNames() {
    return Array.prototype.slice.call(arguments).filter(Boolean).join(' ');
  }

  var ICON_SIZE_BY_BADGE_SIZE = { sm: 12, md: 14, lg: 16 };

  /* ---------- Badge -------------------------------------------------------- */
  function Badge(props) {
    var tone       = props.tone       || 'neutral';   // neutral | accent | success | warning | danger | info
    var variant    = props.variant    || 'subtle';    // subtle | solid | outline
    var size       = props.size       || 'md';        // sm | md | lg
    var pill       = props.pill       || false;
    var dot        = props.dot        || false;       // standalone label-less circle
    var prefixDot  = props.prefixDot  || false;       // leading dot BEFORE label
    var leftIcon   = props.leftIcon;
    var className  = props.className;
    var style      = props.style;
    var children   = props.children;

    var known = ['tone','variant','size','pill','dot','prefixDot',
                 'leftIcon','className','style','children'];
    var rest = {};
    Object.keys(props).forEach(function(k) {
      if (known.indexOf(k) === -1) rest[k] = props[k];
    });

    var NavIcon = window.FieldSight && window.FieldSight.NavIcon;

    var cls = classNames(
      'fs-badge',
      'fs-badge--' + tone,
      'fs-badge--' + variant,
      'fs-badge--' + size,
      pill && 'fs-badge--pill',
      dot && 'fs-badge--dot',
      className
    );

    /* Dot mode — standalone colored circle, no children, no padding */
    if (dot) {
      return React.createElement('span',
        Object.assign({
          className: cls,
          style: style,
          'aria-hidden': 'true',
        }, rest)
      );
    }

    var iconSize = ICON_SIZE_BY_BADGE_SIZE[size] || 14;

    return React.createElement('span',
      Object.assign({ className: cls, style: style }, rest),

      prefixDot ? React.createElement('span', {
        className: 'fs-badge__prefix-dot',
        'aria-hidden': 'true',
      }) : null,

      leftIcon && NavIcon ? React.createElement(NavIcon, {
        name: leftIcon,
        size: iconSize,
        style: { flexShrink: 0 },
      }) : null,

      children != null ? React.createElement('span', {
        className: 'fs-badge__label',
      }, children) : null,
    );
  }

  /* ---------- NotificationDot --------------------------------------------- */
  /* Thin wrapper over Badge. Three modes:
       - count omitted / 0  → small empty dot (just an indicator)
       - count > 0          → pill with number, "99+" if over max
       - tone defaults to 'danger' (platform convention) */
  function NotificationDot(props) {
    var count     = props.count;
    var max       = props.max  || 99;
    var tone      = props.tone || 'danger';
    var size      = props.size || 'md';
    var className = props.className;
    var style     = props.style;

    var known = ['count','max','tone','size','className','style'];
    var rest = {};
    Object.keys(props).forEach(function(k) {
      if (known.indexOf(k) === -1) rest[k] = props[k];
    });

    /* No count or zero → render as dot */
    if (count == null || count === 0) {
      return React.createElement(Badge, Object.assign({
        tone: tone, variant: 'solid', size: size, dot: true,
        className: classNames('fs-notification-dot', className),
        style: style,
      }, rest));
    }

    /* With count → render as solid pill with number */
    var label = count > max ? (max + '+') : String(count);

    return React.createElement(Badge, Object.assign({
      tone: tone, variant: 'solid', size: size, pill: true,
      className: classNames('fs-notification-dot',
                            'fs-notification-dot--with-count', className),
      style: style,
      'aria-label': label + ' notifications',
    }, rest), label);
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.Badge            = Badge;
  window.FieldSight.NotificationDot  = NotificationDot;

})();
