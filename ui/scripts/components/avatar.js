/* ==========================================================================
   FieldSight Avatar + AvatarGroup — Layer 4 display atom
   --------------------------------------------------------------------------
   Initials-first design. Image loads as opportunistic layer over initials,
   so failed images degrade gracefully (no broken-image icon).
   Background color is hashed deterministically from name/colorSeed so
   the same person always renders the same color.

   Exported to:
     window.FieldSight.Avatar
     window.FieldSight.AvatarGroup
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function classNames() {
    return Array.prototype.slice.call(arguments).filter(Boolean).join(' ');
  }

  /* 12-color muted palette, deterministically picked from name hash.
     Avoids brand orange (accent-500) so avatars don't compete with
     primary CTAs. */
  var AVATAR_PALETTE = [
    '#7C3AED', '#0891B2', '#059669', '#65A30D',
    '#CA8A04', '#EA580C', '#DC2626', '#DB2777',
    '#9333EA', '#2563EB', '#0D9488', '#525252',
  ];

  /* djb2 hash → palette index. Stable across page loads. */
  function colorFromString(str) {
    if (!str) return AVATAR_PALETTE[0];
    var h = 5381;
    for (var i = 0; i < str.length; i++) {
      h = ((h << 5) + h + str.charCodeAt(i)) >>> 0;
    }
    return AVATAR_PALETTE[h % AVATAR_PALETTE.length];
  }

  /* "Ben Lin"        → "BL"
     "admin"          → "AD"   (single word: first 2 chars)
     "Mary-Jane Lin"  → "ML"   (split on whitespace, ignore punctuation)
     ""               → "?"  */
  function getInitials(name) {
    if (!name) return '?';
    var clean = String(name).trim();
    if (!clean) return '?';
    var words = clean.split(/\s+/).filter(Boolean);
    if (words.length === 1) {
      return words[0].slice(0, 2).toUpperCase();
    }
    return (words[0].charAt(0) + words[words.length - 1].charAt(0)).toUpperCase();
  }

  /* Sizes (px diameter / font-size).
     Aligns with Card padding rhythm (16px multiples). */
  var SIZE_PX  = { xs: 20, sm: 28, md: 36, lg: 48, xl: 64 };
  var FONT_PX  = { xs: 10, sm: 12, md: 14, lg: 18, xl: 24 };

  /* ---------- Avatar ------------------------------------------------------- */
  function Avatar(props) {
    var name      = props.name      || '';
    var initials  = props.initials;                    // explicit override
    var src       = props.src;
    var alt       = props.alt;
    var size      = props.size      || 'md';
    var shape     = props.shape     || 'circle';      // circle | square
    var colorSeed = props.colorSeed;                  // hash key (default: name)
    var bgColor   = props.bgColor;                    // literal CSS color override
    var textColor = props.textColor || '#fff';
    var className = props.className;
    var style     = props.style;
    var onClick   = props.onClick;

    var known = ['name','initials','src','alt','size','shape','colorSeed',
                 'bgColor','textColor','className','style','onClick'];
    var rest = {};
    Object.keys(props).forEach(function(k) {
      if (known.indexOf(k) === -1) rest[k] = props[k];
    });

    var displayInitials = initials || getInitials(name);
    var resolvedBg = bgColor || colorFromString(colorSeed || name);
    var px       = SIZE_PX[size] || SIZE_PX.md;
    var fontSize = FONT_PX[size] || FONT_PX.md;

    /* Standard React state — no ref+forceUpdate workaround */
    var imgLoadedState = React.useState(false);
    var imgLoaded = imgLoadedState[0];
    var setImgLoaded = imgLoadedState[1];

    var imgErrorState = React.useState(false);
    var imgError = imgErrorState[0];
    var setImgError = imgErrorState[1];

    /* Reset image state when src changes */
    React.useEffect(function() {
      setImgLoaded(false);
      setImgError(false);
    }, [src]);

    var isClickable = !!onClick;
    var tag = isClickable ? 'button' : 'span';

    var cls = classNames(
      'fs-avatar',
      'fs-avatar--' + size,
      shape === 'square' && 'fs-avatar--square',
      isClickable && 'fs-avatar--clickable',
      className
    );

    var elProps = Object.assign({}, rest, {
      className: cls,
      style: Object.assign({
        width: px + 'px',
        height: px + 'px',
        backgroundColor: resolvedBg,
        color: textColor,
        fontSize: fontSize + 'px',
      }, style),
      'aria-label': name || alt || displayInitials,
    });

    if (isClickable) {
      elProps.type = 'button';
      elProps.onClick = onClick;
      elProps.title = name || undefined;
    } else {
      elProps.role = 'img';
    }

    return React.createElement(tag, elProps,
      /* Initials — always rendered, sits underneath image */
      React.createElement('span', {
        className: 'fs-avatar__initials',
        'aria-hidden': src && imgLoaded && !imgError ? 'true' : undefined,
      }, displayInitials),

      /* Image — overlays initials when present and not failed */
      src && !imgError ? React.createElement('img', {
        className: 'fs-avatar__img' + (imgLoaded ? ' fs-avatar__img--loaded' : ''),
        src: src,
        alt: alt || name || '',
        onLoad: function() { setImgLoaded(true); },
        onError: function() { setImgError(true); },
        loading: 'lazy',
      }) : null,
    );
  }

  /* ---------- AvatarGroup -------------------------------------------------- */
  function AvatarGroup(props) {
    var max       = props.max     || 4;
    var size      = props.size    || 'md';
    var spacing   = props.spacing || 'normal';   // normal | tight
    var className = props.className;
    var style     = props.style;
    var children  = props.children;

    var known = ['max','size','spacing','className','style','children'];
    var rest = {};
    Object.keys(props).forEach(function(k) {
      if (known.indexOf(k) === -1) rest[k] = props[k];
    });

    var items = React.Children.toArray(children);
    var total = items.length;
    var visible, overflow;

    if (total <= max) {
      visible = items;
      overflow = 0;
    } else {
      visible = items.slice(0, max - 1);
      overflow = total - (max - 1);
    }

    var px       = SIZE_PX[size] || SIZE_PX.md;
    var fontSize = FONT_PX[size] || FONT_PX.md;

    var cls = classNames(
      'fs-avatar-group',
      'fs-avatar-group--' + size,
      'fs-avatar-group--' + spacing,
      className
    );

    /* Inject `size` into children via cloneElement */
    var visibleEls = visible.map(function(child, i) {
      if (!React.isValidElement(child)) return child;
      return React.cloneElement(child, {
        key: child.key != null ? child.key : i,
        size: child.props.size || size,
      });
    });

    return React.createElement('div',
      Object.assign({
        className: cls,
        style: style,
        role: 'group',
        'aria-label': total + ' people',
      }, rest),

      visibleEls,

      overflow > 0 ? React.createElement('span', {
        className: 'fs-avatar fs-avatar--' + size + ' fs-avatar--overflow',
        style: {
          width: px + 'px',
          height: px + 'px',
          fontSize: (fontSize - 2) + 'px',
        },
        'aria-label': overflow + ' more',
      },
        React.createElement('span', { className: 'fs-avatar__initials' },
          '+' + overflow
        ),
      ) : null,
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.Avatar      = Avatar;
  window.FieldSight.AvatarGroup = AvatarGroup;

})();
