/* ==========================================================================
   FieldSight Card — Layer 4 structural container
   --------------------------------------------------------------------------
   Compound component: Card + Card.Header / Card.Body / Card.Footer
   Class-based styling via styles/components.css. JSX composes className.

   Exported to:
     window.FieldSight.Card        (with .Header, .Body, .Footer attached)
     window.FieldSight.CardHeader
     window.FieldSight.CardBody
     window.FieldSight.CardFooter
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function classNames() {
    return Array.prototype.slice.call(arguments).filter(Boolean).join(' ');
  }

  /* ---------- Card --------------------------------------------------------- */
  function Card(props) {
    var variant   = props.variant   || 'default';   // default | elevated | outline | ghost
    var padding   = props.padding;                    // none | sm | md (default) | lg
    var disabled  = props.disabled  || false;
    var className = props.className;
    var style     = props.style;
    var children  = props.children;
    var onClick   = props.onClick;
    var href      = props.href;
    var as        = props.as;

    // NOTE: `selected` state intentionally not exposed.
    // Selection patterns defer to Layer 5 (list-with-selection).

    var known = ['variant','padding','disabled','className','style',
                 'children','onClick','href','as'];
    var rest = {};
    Object.keys(props).forEach(function(k) {
      if (known.indexOf(k) === -1) rest[k] = props[k];
    });

    /* Interactivity is derived from onClick/href/as — never from variant.
       This keeps appearance and behavior orthogonal: any variant can be
       interactive or not. */
    var isInteractive = !!(onClick || href);

    /* Element decision: explicit `as` wins, then href→a, then onClick→button,
       then default div. Native <button>/<a> give us correct keyboard,
       focus, and assistive-tech behavior for free. */
    var tag = as || (href ? 'a' : (onClick ? 'button' : 'div'));

    var cls = classNames(
      'fs-card',
      'fs-card--' + variant,
      padding && padding !== 'md' && ('fs-card--pad-' + padding),
      isInteractive && 'fs-card--clickable',
      disabled  && 'fs-card--disabled',
      className
    );

    function handleClick(e) {
      if (disabled) {
        e.preventDefault();
        e.stopPropagation();
        return;
      }
      if (onClick) onClick(e);
    }

    var cardProps = Object.assign({}, rest, {
      className: cls,
      style: style,
      onClick: isInteractive ? handleClick : undefined,
      href: href || undefined,
      disabled: (tag === 'button' && disabled) ? true : undefined,
      'aria-disabled': (disabled && tag !== 'button') ? true : undefined,
    });

    // button-specific: strip default border/bg so CSS takes over
    if (tag === 'button') {
      cardProps.type = props.type || 'button';
    }

    return React.createElement(tag, cardProps, children);
  }

  /* ---------- Card.Header -------------------------------------------------- */
  function CardHeader(props) {
    var title     = props.title;
    var subtitle  = props.subtitle;
    var actions   = props.actions;
    var className = props.className;
    var style     = props.style;
    var children  = props.children;

    var cls = classNames('fs-card__header', className);

    // If children are provided, render them directly (full custom header)
    if (children) {
      return React.createElement('div', { className: cls, style: style }, children);
    }

    // Otherwise compose from title / subtitle / actions
    return React.createElement('div', { className: cls, style: style },
      React.createElement('div', { className: 'fs-card__header-text' },
        title ? React.createElement('div', {
          className: 'fs-card__title',
        }, title) : null,
        subtitle ? React.createElement('div', {
          className: 'fs-card__subtitle',
        }, subtitle) : null,
      ),
      actions ? React.createElement('div', {
        className: 'fs-card__header-actions',
      }, actions) : null,
    );
  }

  /* ---------- Card.Body ---------------------------------------------------- */
  function CardBody(props) {
    var className = props.className;
    var style     = props.style;
    var children  = props.children;

    var cls = classNames('fs-card__body', className);

    return React.createElement('div', { className: cls, style: style }, children);
  }

  /* ---------- Card.Footer -------------------------------------------------- */
  function CardFooter(props) {
    var align     = props.align || 'end';   // start | center | end | between
    var className = props.className;
    var style     = props.style;
    var children  = props.children;

    var cls = classNames(
      'fs-card__footer',
      align !== 'end' && ('fs-card__footer--' + align),
      className
    );

    return React.createElement('div', { className: cls, style: style }, children);
  }

  /* ---------- Attach subcomponents ----------------------------------------- */
  Card.Header = CardHeader;
  Card.Body   = CardBody;
  Card.Footer = CardFooter;

  /* ---------- Export ------------------------------------------------------- */
  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.Card       = Card;
  window.FieldSight.CardHeader = CardHeader;
  window.FieldSight.CardBody   = CardBody;
  window.FieldSight.CardFooter = CardFooter;

})();
