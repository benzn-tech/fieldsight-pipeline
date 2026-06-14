/* ==========================================================================
   FieldSight Button + IconButton — Layer 4 base components
   --------------------------------------------------------------------------
   Class-based styling via styles/components.css. JSX composes className.

   Exported to:
     window.FieldSight.Button
     window.FieldSight.IconButton
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  const ICON_SIZE_BY_BTN_SIZE = { sm: 14, md: 16, lg: 18 };

  function classNames() {
    var parts = Array.prototype.slice.call(arguments);
    return parts.filter(Boolean).join(' ');
  }

  /* ---------- Spinner ----------------------------------------------------- */
  function Spinner({ size }) {
    const px = ICON_SIZE_BY_BTN_SIZE[size] || 16;
    return React.createElement('span', {
      className: 'fs-btn__spinner',
      'aria-hidden': 'true',
      style: { width: px + 'px', height: px + 'px' },
    });
  }

  /* ---------- Button ------------------------------------------------------ */
  function Button(props) {
    const variant   = props.variant   || 'primary';
    const size      = props.size      || 'md';
    const type      = props.type      || 'button';
    const disabled  = props.disabled  || false;
    const loading   = props.loading   || false;
    const leftIcon  = props.leftIcon;
    const rightIcon = props.rightIcon;
    const fullWidth = props.fullWidth || false;
    const className = props.className;
    const style     = props.style;
    const children  = props.children;
    const onClick   = props.onClick;

    // Collect rest props (exclude known ones)
    const known = ['variant','size','type','disabled','loading','leftIcon','rightIcon','fullWidth','className','style','children','onClick'];
    const rest = {};
    Object.keys(props).forEach(function(k) {
      if (known.indexOf(k) === -1) rest[k] = props[k];
    });

    const NavIcon = window.FieldSight && window.FieldSight.NavIcon;

    const cls = classNames(
      'fs-btn',
      'fs-btn--' + variant,
      'fs-btn--' + size,
      fullWidth && 'fs-btn--full-width',
      loading && 'fs-btn--loading',
      className
    );

    function handleClick(e) {
      if (disabled || loading) {
        e.preventDefault();
        e.stopPropagation();
        return;
      }
      if (onClick) onClick(e);
    }

    const iconSize = ICON_SIZE_BY_BTN_SIZE[size] || 16;

    const btnProps = Object.assign({}, rest, {
      type: type,
      className: cls,
      style: style,
      disabled: disabled || undefined,
      'aria-disabled': (disabled || loading) ? true : undefined,
      'aria-busy': loading ? true : undefined,
      onClick: handleClick,
    });

    return React.createElement('button', btnProps,
      loading
        ? React.createElement(Spinner, { size: size })
        : (leftIcon && NavIcon
            ? React.createElement(NavIcon, { name: leftIcon, size: iconSize, style: { flexShrink: 0 } })
            : null),
      React.createElement('span', { className: 'fs-btn__label' }, children),
      (rightIcon && NavIcon)
        ? React.createElement(NavIcon, { name: rightIcon, size: iconSize, style: { flexShrink: 0 } })
        : null,
    );
  }

  /* ---------- IconButton -------------------------------------------------- */
  function IconButton(props) {
    const icon      = props.icon;
    const ariaLabel = props.ariaLabel;
    const tooltip   = props.tooltip;
    const variant   = props.variant  || 'ghost';
    const size      = props.size     || 'md';
    const disabled  = props.disabled || false;
    const loading   = props.loading  || false;
    const className = props.className;
    const style     = props.style;
    const onClick   = props.onClick;

    const known = ['icon','ariaLabel','tooltip','variant','size','disabled','loading','className','style','onClick'];
    const rest = {};
    Object.keys(props).forEach(function(k) {
      if (known.indexOf(k) === -1) rest[k] = props[k];
    });

    if (!ariaLabel && typeof console !== 'undefined') {
      console.warn('[IconButton] `ariaLabel` is required for accessibility.');
    }

    const NavIcon = window.FieldSight && window.FieldSight.NavIcon;

    const cls = classNames(
      'fs-btn',
      'fs-btn--icon-only',
      'fs-btn--' + variant,
      'fs-btn--' + size,
      loading && 'fs-btn--loading',
      className
    );

    function handleClick(e) {
      if (disabled || loading) {
        e.preventDefault();
        e.stopPropagation();
        return;
      }
      if (onClick) onClick(e);
    }

    const iconSize = ICON_SIZE_BY_BTN_SIZE[size] || 16;

    const btnProps = Object.assign({}, rest, {
      type: 'button',
      className: cls,
      style: style,
      disabled: disabled || undefined,
      'aria-label': ariaLabel,
      'aria-disabled': (disabled || loading) ? true : undefined,
      'aria-busy': loading ? true : undefined,
      title: tooltip || ariaLabel,
      onClick: handleClick,
    });

    return React.createElement('button', btnProps,
      loading
        ? React.createElement(Spinner, { size: size })
        : (NavIcon && icon
            ? React.createElement(NavIcon, { name: icon, size: iconSize })
            : null),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.Button     = Button;
  window.FieldSight.IconButton = IconButton;

})();
