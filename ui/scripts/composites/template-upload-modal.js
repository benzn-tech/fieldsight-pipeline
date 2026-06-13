/* ==========================================================================
   FieldSight · TemplateUploadModal — Sprint 10 B.2
   --------------------------------------------------------------------------
   Drag-drop upload modal for adding a new report template to the library.
   On submit it calls FS.api.templates.create() which immediately returns
   a stub with _status:'extracting' and fires the real schema extraction
   (simulated) in the background.

   Props:
     scope       'org' | 'personal'   — which library to add to
     onComplete  fn(stub)             — called once create() resolves
     onCancel    fn                   — dismiss without uploading

   Phases: idle → uploading → extracting → done | error

   Accepted formats: .pdf .docx .md .png .jpg .jpeg .tiff  (max 50 MB)

   CSS: §TEMPLATE-UPLOAD in styles/composites.css

   Exported to: window.FieldSight.TemplateUploadModal
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  var ACCEPTED_EXTS = ['.pdf', '.docx', '.md', '.png', '.jpg', '.jpeg', '.tiff'];
  var MAX_SIZE_MB   = 50;
  var REPORT_TYPES  = ['daily', 'weekly', 'monthly', 'incident'];

  function fileExt(name) {
    var dot = name.lastIndexOf('.');
    return dot > 0 ? name.slice(dot).toLowerCase() : '';
  }

  function validateFile(f) {
    if (!f) return 'Please choose a file.';
    if (!ACCEPTED_EXTS.includes(fileExt(f.name))) return 'Unsupported file type. Use PDF, DOCX, MD, or an image.';
    if (f.size > MAX_SIZE_MB * 1024 * 1024)       return 'File is too large (max ' + MAX_SIZE_MB + ' MB).';
    return null;
  }

  function delay(ms) {
    return new Promise(function (r) { setTimeout(r, ms); });
  }

  /* ── Component ────────────────────────────────────────────────────── */

  function TemplateUploadModal(props) {
    var fs           = window.FieldSight;
    var ModalOverlay = fs.ModalOverlay;
    var Button       = fs.Button;

    var scope      = props.scope      || 'personal';
    var onComplete = props.onComplete || function () {};
    var onCancel   = props.onCancel   || function () {};

    var caller = (window.AuthMock && window.AuthMock.currentUser) || {};

    /* phase: idle | dragging | uploading | extracting | done | error */
    var phaseRef = React.useState('idle');
    var phase    = phaseRef[0]; var setPhase = phaseRef[1];

    var fileRef  = React.useState(null);
    var file     = fileRef[0];  var setFile  = fileRef[1];

    var errRef   = React.useState('');
    var errMsg   = errRef[0];   var setErr   = errRef[1];

    var titleRef = React.useState('');
    var title    = titleRef[0]; var setTitle = titleRef[1];

    var descRef  = React.useState('');
    var desc     = descRef[0];  var setDesc  = descRef[1];

    var rtRef    = React.useState('daily');
    var rt       = rtRef[0];    var setRt    = rtRef[1];

    function pickFile(f) {
      var err = validateFile(f);
      if (err) { setErr(err); return; }
      setFile(f);
      setErr('');
      if (!title) setTitle(f.name.replace(/\.[^.]+$/, '').replace(/[-_]/g, ' '));
    }

    function handleDrop(e) {
      e.preventDefault();
      setPhase('idle');
      var f = e.dataTransfer.files[0];
      if (f) pickFile(f);
    }

    function handleFileInput(e) {
      var f = e.target.files[0];
      if (f) pickFile(f);
    }

    async function handleUpload() {
      var ferr = validateFile(file);
      if (ferr) { setErr(ferr); return; }
      if (!title.trim()) { setErr('Please enter a template title.'); return; }
      setErr('');
      setPhase('uploading');
      await delay(600);
      setPhase('extracting');
      try {
        var result = await window.FS.api.templates.create({
          scope:          scope,
          report_type:    rt,
          title:          title.trim(),
          description:    desc.trim(),
          owner_user_id:  caller.device_id || caller.sub || null,
        });
        setPhase('done');
        onComplete(result);
      } catch (e) {
        setPhase('error');
        setErr((e && e.message) || 'Upload failed — please try again.');
      }
    }

    /* ── Render ── */

    var inputId = 'tpl-file-input-' + scope;

    if (phase === 'uploading' || phase === 'extracting') {
      return React.createElement(ModalOverlay, { onClose: onCancel },
        React.createElement('div', { className: 'fs-tpl-upload' },
          React.createElement('h2', { className: 'fs-tpl-upload__title' }, 'Upload template'),
          React.createElement('div', { className: 'fs-tpl-upload__progress' },
            React.createElement('div', { className: 'fs-tpl-upload__spinner' }),
            React.createElement('p', { className: 'fs-tpl-upload__phase-label' },
              phase === 'uploading' ? 'Uploading file…' : 'AI is extracting schema — this takes a moment…',
            ),
            React.createElement('p', { className: 'fs-tpl-upload__phase-sub' },
              'You can close this and come back. We\'ll update the library when it\'s ready.',
            ),
          ),
          React.createElement('div', { className: 'fs-tpl-upload__footer' },
            React.createElement(Button, { variant: 'ghost', onClick: onCancel }, 'Close'),
          ),
        ),
      );
    }

    if (phase === 'done') {
      return React.createElement(ModalOverlay, { onClose: onCancel },
        React.createElement('div', { className: 'fs-tpl-upload' },
          React.createElement('h2', { className: 'fs-tpl-upload__title' }, 'Upload template'),
          React.createElement('div', { className: 'fs-tpl-upload__progress' },
            React.createElement('div', { className: 'fs-tpl-upload__check' }, '✓'),
            React.createElement('p', { className: 'fs-tpl-upload__phase-label' }, 'Extracting in the background…'),
            React.createElement('p', { className: 'fs-tpl-upload__phase-sub' },
              'Your template will appear in the library in a few seconds.',
            ),
          ),
          React.createElement('div', { className: 'fs-tpl-upload__footer' },
            React.createElement(Button, { variant: 'primary', onClick: onCancel }, 'View library'),
          ),
        ),
      );
    }

    if (phase === 'error') {
      return React.createElement(ModalOverlay, { onClose: onCancel },
        React.createElement('div', { className: 'fs-tpl-upload' },
          React.createElement('h2', { className: 'fs-tpl-upload__title' }, 'Upload template'),
          React.createElement('p', { className: 'fs-tpl-upload__error' }, errMsg || 'Something went wrong.'),
          React.createElement('div', { className: 'fs-tpl-upload__footer' },
            React.createElement(Button, { variant: 'ghost', onClick: function () { setPhase('idle'); setErr(''); } }, 'Try again'),
            React.createElement(Button, { variant: 'ghost', onClick: onCancel }, 'Cancel'),
          ),
        ),
      );
    }

    /* idle / dragging */
    return React.createElement(ModalOverlay, { onClose: onCancel },
      React.createElement('div', { className: 'fs-tpl-upload' },
        React.createElement('h2', { className: 'fs-tpl-upload__title' }, 'Upload template'),
        React.createElement('p', { className: 'fs-tpl-upload__subtitle' },
          'Upload a sample report in your preferred format. AI will extract the section structure for you.',
        ),

        /* Drop zone */
        React.createElement('div', {
          className:   'fs-tpl-upload__dropzone' + (phase === 'dragging' ? ' fs-tpl-upload__dropzone--active' : '') + (file ? ' fs-tpl-upload__dropzone--filled' : ''),
          onDragOver:  function (e) { e.preventDefault(); setPhase('dragging'); },
          onDragLeave: function () { setPhase('idle'); },
          onDrop:      handleDrop,
          onClick:     function () { document.getElementById(inputId).click(); },
          role:        'button',
          tabIndex:    0,
          onKeyDown:   function (e) { if (e.key === 'Enter' || e.key === ' ') document.getElementById(inputId).click(); },
        },
          React.createElement('input', {
            id:       inputId,
            type:     'file',
            style:    { display: 'none' },
            accept:   ACCEPTED_EXTS.join(','),
            onChange: handleFileInput,
          }),
          file
            ? React.createElement('div', { className: 'fs-tpl-upload__file-row' },
                React.createElement('span', { className: 'fs-tpl-upload__file-icon' }, '📄'),
                React.createElement('span', { className: 'fs-tpl-upload__file-name' }, file.name),
                React.createElement('button', {
                  className: 'fs-tpl-upload__file-clear',
                  onClick:   function (e) { e.stopPropagation(); setFile(null); setTitle(''); },
                  'aria-label': 'Remove file',
                }, '×'),
              )
            : React.createElement('div', { className: 'fs-tpl-upload__drop-hint' },
                React.createElement('span', { className: 'fs-tpl-upload__drop-icon' }, '⬆'),
                React.createElement('span', null, 'Drop your template file here, or click to browse'),
                React.createElement('span', { className: 'fs-tpl-upload__drop-types' }, 'PDF · DOCX · MD · image — max 50 MB'),
              ),
        ),

        /* Fields */
        React.createElement('div', { className: 'fs-tpl-upload__fields' },
          React.createElement('label', { className: 'fs-tpl-upload__field' },
            React.createElement('span', { className: 'fs-tpl-upload__label' }, 'Title ', React.createElement('span', { 'aria-hidden': 'true', style: { color: 'var(--color-danger-600)' } }, '*')),
            React.createElement('input', {
              className:   'fs-tpl-upload__input',
              value:       title,
              onChange:    function (e) { setTitle(e.target.value); },
              placeholder: 'e.g. Daily Report — Standard',
            }),
          ),
          React.createElement('div', { className: 'fs-tpl-upload__field-row' },
            React.createElement('label', { className: 'fs-tpl-upload__field' },
              React.createElement('span', { className: 'fs-tpl-upload__label' }, 'Report type'),
              React.createElement('select', {
                className: 'fs-tpl-upload__select',
                value:     rt,
                onChange:  function (e) { setRt(e.target.value); },
              },
                REPORT_TYPES.map(function (v) {
                  return React.createElement('option', { key: v, value: v }, v.charAt(0).toUpperCase() + v.slice(1));
                }),
              ),
            ),
          ),
          React.createElement('label', { className: 'fs-tpl-upload__field' },
            React.createElement('span', { className: 'fs-tpl-upload__label' }, 'Description (optional)'),
            React.createElement('textarea', {
              className:   'fs-tpl-upload__textarea',
              rows:        2,
              value:       desc,
              onChange:    function (e) { setDesc(e.target.value); },
              placeholder: 'Brief description of this template\'s use case',
            }),
          ),
        ),

        errMsg && React.createElement('p', { className: 'fs-tpl-upload__error' }, errMsg),

        React.createElement('div', { className: 'fs-tpl-upload__footer' },
          React.createElement(Button, { variant: 'ghost', onClick: onCancel }, 'Cancel'),
          React.createElement(Button, { variant: 'primary', onClick: handleUpload, disabled: !file }, 'Upload & extract'),
        ),
      ),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.TemplateUploadModal = TemplateUploadModal;

})();
