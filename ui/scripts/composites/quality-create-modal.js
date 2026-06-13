/* ==========================================================================
   FieldSight · QualityCreateModal — Sprint 8.1.3
   --------------------------------------------------------------------------
   Modal form for logging a new quality item. Opened by the
   "+ Log Item" button on /quality (gated: quality_manager or site_manager).

   Props:
     siteId    string    — current site context
     onSuccess fn(newItem) — called after successful POST; caller prepends
                             newItem to the QualityProvider list
     onCancel  fn        — called when user dismisses without submitting

   Fields:
     observation        Textarea, required
     category           Select: quality | compliance | workmanship, required
     follow_up_required Checkbox, default true
     deadline           Input[type=date], optional
     location           Input, optional

   Real-backend path (useMocks=false):
     POST /api/quality-items
     body: { site_id, observation, category, follow_up_required, deadline, location }

   Mock path: builds a local item object and calls onSuccess immediately.

   CSS: .fs-quality-create-modal in styles/composites.css

   Exported to: window.FieldSight.QualityCreateModal
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  var CATEGORIES = ['quality', 'compliance', 'workmanship'];

  function QualityCreateModal(props) {
    var fs           = window.FieldSight;
    var ModalOverlay = fs.ModalOverlay;
    var Button       = fs.Button;

    var siteId    = props.siteId    || '';
    var onSuccess = props.onSuccess || function () {};
    var onCancel  = props.onCancel  || function () {};

    var refForm = React.useState({
      observation:        '',
      category:           'quality',
      follow_up_required: true,
      deadline:           '',
      location:           '',
    });
    var form    = refForm[0];
    var setForm = refForm[1];

    var refStatus = React.useState('idle');
    var status    = refStatus[0];
    var setStatus = refStatus[1];

    var refError = React.useState('');
    var errorMsg = refError[0];
    var setError = refError[1];

    function set(field, value) {
      setForm(function (f) { return Object.assign({}, f, { [field]: value }); });
    }

    function validate() {
      if (!form.observation.trim()) return 'Observation is required.';
      if (!form.category) return 'Category is required.';
      return null;
    }

    async function handleSubmit(e) {
      e.preventDefault();
      var err = validate();
      if (err) { setError(err); return; }

      setStatus('submitting');
      setError('');

      try {
        var newItem;

        if (!window.FS.api.useMocks) {
          newItem = await window.FS.api.request('/quality-items', {
            method: 'POST',
            body: {
              site_id:            siteId,
              observation:        form.observation.trim(),
              category:           form.category,
              follow_up_required: form.follow_up_required,
              deadline:           form.deadline || null,
              location:           form.location.trim() || null,
            },
          });
        } else {
          await window.FS.api.delay(400);
          var who = (window.AuthMock && window.AuthMock.currentUser && window.AuthMock.currentUser.name) || 'Unknown';
          newItem = {
            id:                'qc_' + Date.now(),
            date:              window.FS.api.todayNZDT(),
            item:              form.observation.trim(),
            details:           null,
            category:          form.category,
            status:            'observed',
            follow_up_needed:  form.follow_up_required,
            who_raised:        who,
            source:            'qc_item',
            topic_id:          -1,
            topic_title:       null,
            site:              siteId,
          };
        }

        var toast = window.FS && window.FS.toast;
        if (toast) toast.show({ message: 'Quality item logged.', tone: 'success' });

        onSuccess(newItem);
      } catch (fetchErr) {
        setStatus('error');
        setError((fetchErr && fetchErr.message) || 'Failed to log item. Please try again.');
        var toast2 = window.FS && window.FS.toast;
        if (toast2) toast2.show({ message: 'Failed to log quality item', tone: 'error' });
      }
    }

    var isSubmitting = status === 'submitting';

    var content = React.createElement('form', {
      className: 'fs-quality-create-modal',
      onSubmit:  handleSubmit,
    },
      React.createElement('h2', { className: 'fs-quality-create-modal__title' },
        'Log Quality Item'),

      /* Observation */
      React.createElement('label', { className: 'fs-quality-create-modal__field' },
        React.createElement('span', { className: 'fs-quality-create-modal__label' },
          'Observation ', React.createElement('span', { className: 'fs-quality-create-modal__required' }, '*')),
        React.createElement('textarea', {
          className:   'fs-quality-create-modal__textarea',
          value:       form.observation,
          onChange:    function (e) { set('observation', e.target.value); },
          rows:        3,
          placeholder: 'Describe the quality concern…',
          required:    true,
          disabled:    isSubmitting,
        }),
      ),

      /* Category */
      React.createElement('label', { className: 'fs-quality-create-modal__field' },
        React.createElement('span', { className: 'fs-quality-create-modal__label' },
          'Category ', React.createElement('span', { className: 'fs-quality-create-modal__required' }, '*')),
        React.createElement('select', {
          className: 'fs-quality-create-modal__select',
          value:     form.category,
          onChange:  function (e) { set('category', e.target.value); },
          required:  true,
          disabled:  isSubmitting,
        },
          CATEGORIES.map(function (cat) {
            return React.createElement('option', { key: cat, value: cat },
              cat.charAt(0).toUpperCase() + cat.slice(1));
          }),
        ),
      ),

      /* Follow-up required */
      React.createElement('label', { className: 'fs-quality-create-modal__field fs-quality-create-modal__field--inline' },
        React.createElement('input', {
          type:     'checkbox',
          className: 'fs-quality-create-modal__checkbox',
          checked:  form.follow_up_required,
          onChange: function (e) { set('follow_up_required', e.target.checked); },
          disabled: isSubmitting,
        }),
        React.createElement('span', { className: 'fs-quality-create-modal__label' },
          'Follow-up required'),
      ),

      /* Deadline */
      React.createElement('label', { className: 'fs-quality-create-modal__field' },
        React.createElement('span', { className: 'fs-quality-create-modal__label' }, 'Deadline'),
        React.createElement('input', {
          type:      'date',
          className: 'fs-quality-create-modal__input',
          value:     form.deadline,
          onChange:  function (e) { set('deadline', e.target.value); },
          disabled:  isSubmitting,
        }),
      ),

      /* Location */
      React.createElement('label', { className: 'fs-quality-create-modal__field' },
        React.createElement('span', { className: 'fs-quality-create-modal__label' }, 'Location'),
        React.createElement('input', {
          type:        'text',
          className:   'fs-quality-create-modal__input',
          value:       form.location,
          onChange:    function (e) { set('location', e.target.value); },
          placeholder: 'e.g. Level 2, Zone A (optional)',
          disabled:    isSubmitting,
        }),
      ),

      errorMsg
        ? React.createElement('div', { className: 'fs-quality-create-modal__error', role: 'alert' },
            errorMsg)
        : null,

      React.createElement('div', { className: 'fs-quality-create-modal__actions' },
        Button
          ? React.createElement(Button, {
              type:     'button',
              variant:  'secondary',
              size:     'md',
              disabled: isSubmitting,
              onClick:  onCancel,
            }, 'Cancel')
          : React.createElement('button', { type: 'button', onClick: onCancel }, 'Cancel'),
        Button
          ? React.createElement(Button, {
              type:     'submit',
              variant:  'primary',
              size:     'md',
              disabled: isSubmitting,
            }, isSubmitting ? 'Logging…' : 'Log Item')
          : React.createElement('button', { type: 'submit', disabled: isSubmitting },
              isSubmitting ? 'Logging…' : 'Log Item'),
      ),
    );

    if (ModalOverlay) {
      return React.createElement(ModalOverlay, { onClose: onCancel }, content);
    }
    return React.createElement('div', { className: 'fs-modal-overlay__backdrop' }, content);
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.QualityCreateModal = QualityCreateModal;

})();
