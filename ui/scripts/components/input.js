/* ==========================================================================
   FieldSight Input + Textarea + Select — Layer 4 form primitives
   --------------------------------------------------------------------------
   Class-based styling via styles/components.css. JSX composes className.

   Exported to:
     window.FieldSight.Input
     window.FieldSight.Textarea
     window.FieldSight.Select
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function classNames() {
    return Array.prototype.slice.call(arguments).filter(Boolean).join(' ');
  }

  /* Auto-generated id counter for label-control linking when no id provided */
  var _idCounter = 0;
  function useFieldId(provided) {
    var ref = React.useRef(null);
    if (!ref.current) {
      ref.current = provided || ('fs-field-' + (++_idCounter));
    } else if (provided && provided !== ref.current) {
      ref.current = provided;
    }
    return ref.current;
  }

  /* ---------- Shared field shell ----------------------------------------- */
  function FieldShell(props) {
    var id       = props.id;
    var size     = props.size;
    var label    = props.label;
    var required = props.required;
    var hint     = props.hint;
    var error    = props.error;
    var disabled = props.disabled;
    var fullWidth = props.fullWidth;
    var className = props.className;
    var children  = props.children;

    var cls = classNames(
      'fs-field',
      'fs-field--' + size,
      error    && 'fs-field--error',
      disabled && 'fs-field--disabled',
      fullWidth && 'fs-field--full-width',
      className
    );

    var hintId = (hint || error) ? (id + '-hint') : undefined;

    return React.createElement('div', { className: cls },
      label ? React.createElement('label', {
        className: 'fs-field__label',
        htmlFor: id,
      },
        label,
        required ? React.createElement('span', {
          className: 'fs-field__required',
          'aria-hidden': 'true',
        }, ' *') : null,
      ) : null,

      React.createElement('div', { className: 'fs-field__control-wrap' },
        children
      ),

      (error || hint) ? React.createElement('div', {
        id: hintId,
        className: 'fs-field__hint' + (error ? ' fs-field__hint--error' : ''),
        role: error ? 'alert' : undefined,
      }, error || hint) : null,
    );
  }

  /* ---------- Input ------------------------------------------------------- */
  function Input(props) {
    var type          = props.type          || 'text';
    var size          = props.size          || 'md';
    var label         = props.label;
    var required      = props.required;
    var hint          = props.hint;
    var error         = props.error;
    var leftIcon      = props.leftIcon;
    var rightAddon    = props.rightAddon;
    var fullWidth     = props.fullWidth !== false; // default true
    var disabled      = props.disabled;
    var readOnly      = props.readOnly;
    var value         = props.value;
    var defaultValue  = props.defaultValue;
    var onChange      = props.onChange;
    var placeholder   = props.placeholder;
    var providedId    = props.id;
    var className     = props.className;
    var inputClassName = props.inputClassName;

    var known = ['type','size','label','required','hint','error','leftIcon','rightAddon',
                 'fullWidth','disabled','readOnly','value','defaultValue','onChange',
                 'placeholder','id','className','inputClassName'];
    var rest = {};
    Object.keys(props).forEach(function(k) {
      if (known.indexOf(k) === -1) rest[k] = props[k];
    });

    var id = useFieldId(providedId);
    var NavIcon = window.FieldSight && window.FieldSight.NavIcon;
    var iconPx = size === 'sm' ? 14 : size === 'lg' ? 18 : 16;

    var inputCls = classNames(
      'fs-field__control',
      leftIcon   && 'fs-field__control--has-left-icon',
      rightAddon && 'fs-field__control--has-right-addon',
      inputClassName
    );

    var inputProps = Object.assign({}, rest, {
      id: id,
      type: type,
      className: inputCls,
      disabled: disabled || undefined,
      readOnly: readOnly || undefined,
      'aria-invalid': error ? true : undefined,
      'aria-describedby': (hint || error) ? (id + '-hint') : undefined,
      placeholder: placeholder,
      onChange: onChange,
    });
    if (value !== undefined)        inputProps.value = value;
    else if (defaultValue !== undefined) inputProps.defaultValue = defaultValue;

    return React.createElement(FieldShell, {
      id: id, size: size, label: label, required: required,
      hint: hint, error: error, disabled: disabled,
      fullWidth: fullWidth, className: className,
    },
      leftIcon && NavIcon ? React.createElement('span', {
        className: 'fs-field__left-icon',
        'aria-hidden': 'true',
      }, React.createElement(NavIcon, { name: leftIcon, size: iconPx })) : null,

      React.createElement('input', inputProps),

      rightAddon ? React.createElement('span', {
        className: 'fs-field__right-addon',
      }, rightAddon) : null,
    );
  }

  /* ---------- Textarea ---------------------------------------------------- */
  function Textarea(props) {
    var size          = props.size          || 'md';
    var rows          = props.rows          || 3;
    var resize        = props.resize        || 'vertical';
    var label         = props.label;
    var required      = props.required;
    var hint          = props.hint;
    var error         = props.error;
    var fullWidth     = props.fullWidth !== false;
    var disabled      = props.disabled;
    var readOnly      = props.readOnly;
    var value         = props.value;
    var defaultValue  = props.defaultValue;
    var onChange      = props.onChange;
    var placeholder   = props.placeholder;
    var providedId    = props.id;
    var className     = props.className;
    var inputClassName = props.inputClassName;

    var known = ['size','rows','resize','label','required','hint','error','fullWidth',
                 'disabled','readOnly','value','defaultValue','onChange','placeholder',
                 'id','className','inputClassName','style'];
    var rest = {};
    Object.keys(props).forEach(function(k) {
      if (known.indexOf(k) === -1) rest[k] = props[k];
    });

    var id = useFieldId(providedId);

    var taCls = classNames(
      'fs-field__control',
      'fs-field__control--textarea',
      inputClassName
    );

    var taProps = Object.assign({}, rest, {
      id: id,
      rows: rows,
      className: taCls,
      style: Object.assign({ resize: resize }, props.style),
      disabled: disabled || undefined,
      readOnly: readOnly || undefined,
      'aria-invalid': error ? true : undefined,
      'aria-describedby': (hint || error) ? (id + '-hint') : undefined,
      placeholder: placeholder,
      onChange: onChange,
    });
    if (value !== undefined)        taProps.value = value;
    else if (defaultValue !== undefined) taProps.defaultValue = defaultValue;

    return React.createElement(FieldShell, {
      id: id, size: size, label: label, required: required,
      hint: hint, error: error, disabled: disabled,
      fullWidth: fullWidth, className: className,
    },
      React.createElement('textarea', taProps),
    );
  }

  /* ---------- Select ------------------------------------------------------ */
  function Select(props) {
    var size          = props.size          || 'md';
    var label         = props.label;
    var required      = props.required;
    var hint          = props.hint;
    var error         = props.error;
    var fullWidth     = props.fullWidth !== false;
    var disabled      = props.disabled;
    var value         = props.value;
    var defaultValue  = props.defaultValue;
    var onChange      = props.onChange;
    var options       = props.options       || [];
    var placeholder   = props.placeholder;
    var providedId    = props.id;
    var className     = props.className;
    var inputClassName = props.inputClassName;

    var known = ['size','label','required','hint','error','fullWidth','disabled',
                 'value','defaultValue','onChange','options','placeholder',
                 'id','className','inputClassName'];
    var rest = {};
    Object.keys(props).forEach(function(k) {
      if (known.indexOf(k) === -1) rest[k] = props[k];
    });

    var id = useFieldId(providedId);
    var NavIcon = window.FieldSight && window.FieldSight.NavIcon;
    var iconPx = size === 'sm' ? 14 : size === 'lg' ? 18 : 16;

    var selCls = classNames(
      'fs-field__control',
      'fs-field__control--select',
      inputClassName
    );

    var selProps = Object.assign({}, rest, {
      id: id,
      className: selCls,
      disabled: disabled || undefined,
      'aria-invalid': error ? true : undefined,
      'aria-describedby': (hint || error) ? (id + '-hint') : undefined,
      onChange: onChange,
    });
    if (value !== undefined)        selProps.value = value;
    else if (defaultValue !== undefined) selProps.defaultValue = defaultValue;

    var optionEls = [];
    if (placeholder) {
      optionEls.push(React.createElement('option', {
        key: '__placeholder', value: '', disabled: true,
      }, placeholder));
    }
    options.forEach(function(opt) {
      optionEls.push(React.createElement('option', {
        key: opt.value,
        value: opt.value,
        disabled: opt.disabled || undefined,
      }, opt.label));
    });

    return React.createElement(FieldShell, {
      id: id, size: size, label: label, required: required,
      hint: hint, error: error, disabled: disabled,
      fullWidth: fullWidth, className: className,
    },
      React.createElement('select', selProps, optionEls),

      React.createElement('span', {
        className: 'fs-field__select-chevron',
        'aria-hidden': 'true',
      },
        NavIcon ? React.createElement(NavIcon, { name: 'chevron-down', size: iconPx }) : null,
      ),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.Input    = Input;
  window.FieldSight.Textarea = Textarea;
  window.FieldSight.Select   = Select;

})();
