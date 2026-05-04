/* ==========================================================================
   FieldSight · SafetyCreateModal — Sprint 8.1.2
   --------------------------------------------------------------------------
   Modal form for raising a new safety observation. Opened by the
   "+ Raise Observation" button on /safety (gated: hse_manager or
   site_manager).

   Props:
     siteId    string    — current site context (passed by safety page)
     onSuccess fn(newFlag) — called after a successful POST; caller
                            prepends newFlag to the SafetyProvider list
     onCancel  fn        — called when user dismisses without submitting

   Fields:
     observation         Textarea, required
     risk_level          Select: low | medium | high, required
     recommended_action  Textarea, optional
     location            Input, optional
     photos              file input, multiple, accept image/*, max 5

   Real-backend path (useMocks=false):
     POST /api/safety-observations
     body: { site_id, observation, risk_level, recommended_action, location, photo_keys[] }
     photo_keys come from presigned PUT uploads via FS.api.media.presignedPut().

   Mock path: builds a local flag object and calls onSuccess immediately.

   CSS: .fs-safety-create-modal in styles/composites.css

   Exported to: window.FieldSight.SafetyCreateModal
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  var RISK_LEVELS = ['low', 'medium', 'high'];

  function SafetyCreateModal(props) {
    var fs          = window.FieldSight;
    var ModalOverlay = fs.ModalOverlay;
    var Button      = fs.Button;
    var Input       = fs.Input;

    var siteId    = props.siteId    || '';
    var onSuccess = props.onSuccess || function () {};
    var onCancel  = props.onCancel  || function () {};

    var refForm = React.useState({
      observation:        '',
      risk_level:         'medium',
      recommended_action: '',
      location:           '',
    });
    var form    = refForm[0];
    var setForm = refForm[1];

    var refPhotos = React.useState([]);
    var photos    = refPhotos[0];
    var setPhotos = refPhotos[1];

    var refStatus = React.useState('idle');  /* 'idle' | 'submitting' | 'error' */
    var status    = refStatus[0];
    var setStatus = refStatus[1];

    var refError = React.useState('');
    var errorMsg = refError[0];
    var setError = refError[1];

    function set(field, value) {
      setForm(function (f) { return Object.assign({}, f, { [field]: value }); });
    }

    function onPhotoChange(e) {
      var files = Array.from(e.target.files || []).slice(0, 5);
      setPhotos(files);
    }

    function validate() {
      if (!form.observation.trim()) return 'Observation is required.';
      if (!form.risk_level) return 'Risk level is required.';
      return null;
    }

    async function handleSubmit(e) {
      e.preventDefault();
      var err = validate();
      if (err) { setError(err); return; }

      setStatus('submitting');
      setError('');

      try {
        var newFlag;

        if (!window.FS.api.useMocks) {
          /* Upload photos first (if any). */
          var photoKeys = [];
          if (photos.length > 0 && window.FS.api.media && window.FS.api.media.presignedPut) {
            photoKeys = await Promise.all(photos.map(async function (file) {
              var key = await window.FS.api.media.presignedPut(file.name, file.type);
              return key;
            }));
          }

          newFlag = await window.FS.api.request('/safety-observations', {
            method: 'POST',
            body: {
              site_id:            siteId,
              observation:        form.observation.trim(),
              risk_level:         form.risk_level,
              recommended_action: form.recommended_action.trim() || null,
              location:           form.location.trim() || null,
              photo_keys:         photoKeys,
            },
          });
        } else {
          /* Mock path — simulate network delay and build a local flag. */
          await window.FS.api.delay(400);
          var who = (window.AuthMock && window.AuthMock.currentUser && window.AuthMock.currentUser.name) || 'Unknown';
          newFlag = {
            id:                 'obs_' + Date.now(),
            date:               window.FS.api.todayNZDT(),
            observation:        form.observation.trim(),
            risk_level:         form.risk_level,
            recommended_action: form.recommended_action.trim() || null,
            location:           form.location.trim() || null,
            status:             'open',
            who_raised:         who,
            source:             'observation',
            topic_id:           -1,
            topic_title:        'Site safety observations',
            site:               siteId,
          };
        }

        /* Toast success. */
        var toast = window.FS && window.FS.toast;
        if (toast) toast.show({ message: 'Safety observation raised.', tone: 'success' });

        onSuccess(newFlag);
      } catch (fetchErr) {
        setStatus('error');
        setError((fetchErr && fetchErr.message) || 'Failed to raise observation. Please try again.');
        var toast2 = window.FS && window.FS.toast;
        if (toast2) {
          toast2.show({
            message: 'Failed to raise observation',
            tone:    'error',
          });
        }
      }
    }

    var isSubmitting = status === 'submitting';

    var content = React.createElement('form', {
      className: 'fs-safety-create-modal',
      onSubmit:  handleSubmit,
    },
      React.createElement('h2', { className: 'fs-safety-create-modal__title' },
        'Raise Safety Observation'),

      /* Observation */
      React.createElement('label', { className: 'fs-safety-create-modal__field' },
        React.createElement('span', { className: 'fs-safety-create-modal__label' },
          'Observation ', React.createElement('span', { className: 'fs-safety-create-modal__required' }, '*')),
        React.createElement('textarea', {
          className:   'fs-safety-create-modal__textarea',
          value:       form.observation,
          onChange:    function (e) { set('observation', e.target.value); },
          rows:        4,
          placeholder: 'Describe the safety concern…',
          required:    true,
          disabled:    isSubmitting,
        }),
      ),

      /* Risk level */
      React.createElement('label', { className: 'fs-safety-create-modal__field' },
        React.createElement('span', { className: 'fs-safety-create-modal__label' },
          'Risk level ', React.createElement('span', { className: 'fs-safety-create-modal__required' }, '*')),
        React.createElement('select', {
          className: 'fs-safety-create-modal__select',
          value:     form.risk_level,
          onChange:  function (e) { set('risk_level', e.target.value); },
          required:  true,
          disabled:  isSubmitting,
        },
          RISK_LEVELS.map(function (lvl) {
            return React.createElement('option', { key: lvl, value: lvl },
              lvl.charAt(0).toUpperCase() + lvl.slice(1));
          }),
        ),
      ),

      /* Recommended action */
      React.createElement('label', { className: 'fs-safety-create-modal__field' },
        React.createElement('span', { className: 'fs-safety-create-modal__label' },
          'Recommended action'),
        React.createElement('textarea', {
          className:   'fs-safety-create-modal__textarea',
          value:       form.recommended_action,
          onChange:    function (e) { set('recommended_action', e.target.value); },
          rows:        2,
          placeholder: 'Suggested corrective measure (optional)…',
          disabled:    isSubmitting,
        }),
      ),

      /* Location */
      React.createElement('label', { className: 'fs-safety-create-modal__field' },
        React.createElement('span', { className: 'fs-safety-create-modal__label' }, 'Location'),
        React.createElement('input', {
          type:        'text',
          className:   'fs-safety-create-modal__input',
          value:       form.location,
          onChange:    function (e) { set('location', e.target.value); },
          placeholder: 'e.g. Level 3, Grid B-4 (optional)',
          disabled:    isSubmitting,
        }),
      ),

      /* Photos */
      React.createElement('label', { className: 'fs-safety-create-modal__field' },
        React.createElement('span', { className: 'fs-safety-create-modal__label' },
          'Photos ', React.createElement('span', { className: 'fs-safety-create-modal__hint' }, '(max 5)')),
        React.createElement('input', {
          type:     'file',
          className: 'fs-safety-create-modal__file',
          accept:   'image/*',
          multiple: true,
          onChange: onPhotoChange,
          disabled: isSubmitting,
        }),
        photos.length > 0
          ? React.createElement('div', { className: 'fs-safety-create-modal__file-count' },
              photos.length + (photos.length === 1 ? ' photo selected' : ' photos selected'))
          : null,
      ),

      /* Validation error */
      errorMsg
        ? React.createElement('div', { className: 'fs-safety-create-modal__error', role: 'alert' },
            errorMsg)
        : null,

      /* Actions */
      React.createElement('div', { className: 'fs-safety-create-modal__actions' },
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
            }, isSubmitting ? 'Raising…' : 'Raise Observation')
          : React.createElement('button', { type: 'submit', disabled: isSubmitting },
              isSubmitting ? 'Raising…' : 'Raise Observation'),
      ),
    );

    if (ModalOverlay) {
      return React.createElement(ModalOverlay, { onClose: onCancel }, content);
    }
    /* Fallback when ModalOverlay isn't loaded yet. */
    return React.createElement('div', { className: 'fs-modal-overlay__backdrop' }, content);
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.SafetyCreateModal = SafetyCreateModal;

})();
