/* ==========================================================================
   FieldSight — ProgrammeImportModal  (Sprint 5.4)
   --------------------------------------------------------------------------
   File-picker modal for CSV programme imports. Opens via an "Import…"
   button in the Programme header; wraps ModalOverlay and the native
   programme-import parser (window.FS.api.programmeImport.parseCSV).

   Flow:
     pick  →  preview (validation report + task table)  →  confirm
                                                     ↓
                              calls onImport(parents, leaves) on success

   Props:
     open       boolean
     onClose    () => void
     onImport   (parents, leaves) => void   — called only if no errors

   Exported to:  window.FieldSight.ProgrammeImportModal
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  var STATUS_LABELS = {
    not_started: 'Not started',
    in_progress:  'In progress',
    completed:    'Completed',
    delayed:      'Delayed',
    blocked:      'Blocked',
    group:        'Group',
  };

  /* ---- helpers ------------------------------------------------------------ */

  function pluralise(n, singular, plural) {
    return n === 1 ? n + ' ' + singular : n + ' ' + (plural || singular + 's');
  }

  function fmtDate(s) {
    if (!s) return '—';
    var d = new Date(s + 'T00:00:00Z');
    return d.toLocaleDateString('en-NZ', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
  }

  /* ---- DropZone ----------------------------------------------------------- */

  function DropZone(props) {
    var onFile = props.onFile || function () {};
    var refDrag = React.useState(false);
    var dragOver = refDrag[0];
    var setDragOver = refDrag[1];

    var inputRef = React.useRef(null);

    function handleFiles(fileList) {
      var f = fileList && fileList[0];
      if (!f) return;
      var ext = f.name.split('.').pop().toLowerCase();
      if (ext !== 'csv' && ext !== 'xml' && ext !== 'xlsx' && ext !== 'xls') {
        alert('Unsupported file type. Please select a .csv, .xml (MS Project), .xlsx, or .xls file.');
        return;
      }
      onFile(f);
    }

    function onDragOver(e) {
      e.preventDefault();
      setDragOver(true);
    }
    function onDragLeave() { setDragOver(false); }
    function onDrop(e) {
      e.preventDefault();
      setDragOver(false);
      handleFiles(e.dataTransfer && e.dataTransfer.files);
    }
    function onInputChange(e) {
      handleFiles(e.target.files);
      /* reset so re-picking the same file fires again */
      e.target.value = '';
    }

    return React.createElement('div', {
      className: 'fs-prog-import__drop' + (dragOver ? ' fs-prog-import__drop--over' : ''),
      onDragOver:  onDragOver,
      onDragLeave: onDragLeave,
      onDrop:      onDrop,
      onClick:     function () { inputRef.current && inputRef.current.click(); },
      role:        'button',
      tabIndex:    0,
      'aria-label': 'Select a CSV or MS Project XML file to import',
      onKeyDown: function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); inputRef.current && inputRef.current.click(); }
      },
    },
      React.createElement('input', {
        ref:      inputRef,
        type:     'file',
        accept:   '.csv,text/csv,.xml,text/xml,application/xml,.xlsx,.xls,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.ms-excel',
        style:    { display: 'none' },
        onChange: onInputChange,
      }),
      React.createElement('div', { className: 'fs-prog-import__drop-icon' }, '📂'),
      React.createElement('div', { className: 'fs-prog-import__drop-title' },
        dragOver ? 'Drop to import' : 'Drag & drop a file here'),
      React.createElement('div', { className: 'fs-prog-import__drop-sub' },
        'or click to browse · .csv · .xml (MS Project) · .xlsx / .xls'),
      React.createElement('div', { className: 'fs-prog-import__drop-formats' },
        React.createElement('span', { className: 'fs-prog-import__drop-format-label' }, 'CSV / XLSX columns:'),
        React.createElement('code', null, 'task_id, wbs, parent_id, name, start, end, progress_pct, status, depends_on, assignees'),
        React.createElement('span', { className: 'fs-prog-import__drop-format-label' }, 'MS Project XML:'),
        'File → Save As → XML Format in MS Project 2016+',
      ),
    );
  }

  /* ---- ColumnMapper ------------------------------------------------------- */
  /* Shown when an XLSX file's first-row headers don't match the CSV contract. */

  var CSV_COLUMNS = ['task_id', 'wbs', 'parent_id', 'name', 'start', 'end',
                     'progress_pct', 'status', 'depends_on', 'assignees'];
  var REQUIRED_MAP_COLS = ['task_id', 'wbs', 'name', 'start', 'end', 'status'];

  function ColumnMapper(props) {
    var headers   = props.headers  || [];
    var onConfirm = props.onConfirm || function () {};
    var onBack    = props.onBack    || function () {};

    var Button = window.FieldSight && window.FieldSight.Button;

    /* mapping: { xlsxHeader → csvColumn | '' } */
    var initialMap = {};
    headers.forEach(function (h) {
      /* Auto-match exact names */
      initialMap[h] = CSV_COLUMNS.indexOf(h) !== -1 ? h : '';
    });

    var refMap = React.useState(initialMap);
    var mapping = refMap[0];
    var setMapping = refMap[1];

    function setCol(header, csvCol) {
      setMapping(function (prev) {
        var next = Object.assign({}, prev);
        next[header] = csvCol;
        return next;
      });
    }

    var missingRequired = REQUIRED_MAP_COLS.filter(function (col) {
      return Object.values(mapping).indexOf(col) === -1;
    });

    function handleConfirm() {
      /* Build columnMap: only include mappings with a destination */
      var columnMap = {};
      Object.keys(mapping).forEach(function (h) {
        if (mapping[h]) columnMap[h] = mapping[h];
      });
      onConfirm(columnMap);
    }

    return React.createElement('div', { className: 'fs-prog-import__mapper' },
      React.createElement('div', { className: 'fs-prog-import__mapper-header' },
        React.createElement('strong', null, 'Map columns'),
        React.createElement('p', { className: 'fs-prog-import__mapper-sub' },
          'The file headers don\'t exactly match the expected column names. Map each source column to a FieldSight field.'),
      ),
      React.createElement('table', { className: 'fs-prog-import__mapper-table' },
        React.createElement('thead', null,
          React.createElement('tr', null,
            React.createElement('th', null, 'File column'),
            React.createElement('th', null, 'Maps to'),
          ),
        ),
        React.createElement('tbody', null,
          headers.map(function (h) {
            return React.createElement('tr', { key: h },
              React.createElement('td', { className: 'fs-prog-import__td-mono' }, h),
              React.createElement('td', null,
                React.createElement('select', {
                  className: 'fs-prog-import__mapper-select',
                  value:     mapping[h] || '',
                  onChange:  function (e) { setCol(h, e.target.value); },
                },
                  React.createElement('option', { value: '' }, '— ignore —'),
                  CSV_COLUMNS.map(function (col) {
                    return React.createElement('option', { key: col, value: col }, col);
                  }),
                ),
              ),
            );
          }),
        ),
      ),
      /* Sprint 11 B.1 — partial mapping allowed (Q-S11-2 default).
         Required columns left unmapped still let the user proceed;
         downstream `parseXLSXWithMap` fills nulls + the existing
         ValidationReport flags the resulting rows. The warning copy
         is now non-blocking — it informs rather than gates. */
      missingRequired.length > 0 && React.createElement('div', {
        className: 'fs-prog-import__mapper-warn',
        role:      'note',
      },
        React.createElement('strong', null, 'Heads-up · ' + missingRequired.length + ' required column' + (missingRequired.length === 1 ? '' : 's') + ' unmapped: '),
        missingRequired.join(', '),
        '. You can still proceed — those fields will be empty on every row, and the next step will flag them as warnings.',
      ),
      React.createElement('div', { className: 'fs-prog-import__footer' },
        Button
          ? React.createElement(Button, { variant: 'secondary', size: 'sm', onClick: onBack }, '← Back')
          : React.createElement('button', { type: 'button', onClick: onBack }, 'Back'),
        Button
          ? React.createElement(Button, {
              variant: 'primary', size: 'sm',
              onClick:  handleConfirm,
            }, missingRequired.length > 0
                ? 'Apply mapping (' + missingRequired.length + ' field' + (missingRequired.length === 1 ? '' : 's') + ' skipped)'
                : 'Apply mapping')
          : React.createElement('button', {
              type: 'button',
              onClick: handleConfirm,
            }, 'Apply mapping'),
      ),
    );
  }

  /* ---- ValidationReport -------------------------------------------------- */

  function ValidationReport(props) {
    var errors   = props.errors   || [];
    var warnings = props.warnings || [];
    if (errors.length === 0 && warnings.length === 0) return null;

    return React.createElement('div', { className: 'fs-prog-import__report' },
      errors.length > 0 && React.createElement('div', { className: 'fs-prog-import__report-section fs-prog-import__report-section--error' },
        React.createElement('div', { className: 'fs-prog-import__report-header' },
          React.createElement('span', { className: 'fs-prog-import__report-badge fs-prog-import__report-badge--error' },
            pluralise(errors.length, 'error')),
          ' — import blocked until resolved'),
        React.createElement('ul', { className: 'fs-prog-import__report-list' },
          errors.map(function (e, i) {
            return React.createElement('li', { key: i }, e.message);
          }),
        ),
      ),
      warnings.length > 0 && React.createElement('div', { className: 'fs-prog-import__report-section fs-prog-import__report-section--warn' },
        React.createElement('div', { className: 'fs-prog-import__report-header' },
          React.createElement('span', { className: 'fs-prog-import__report-badge fs-prog-import__report-badge--warn' },
            pluralise(warnings.length, 'warning')),
          ' — import allowed, review before confirming'),
        React.createElement('ul', { className: 'fs-prog-import__report-list' },
          warnings.map(function (w, i) {
            return React.createElement('li', { key: i }, w.message);
          }),
        ),
      ),
    );
  }

  /* ---- PreviewTable ------------------------------------------------------- */

  function PreviewTable(props) {
    var parents = props.parents || [];
    var leaves  = props.leaves  || [];
    var MAX_ROWS = 20;

    var allRows = [];
    /* Interleave: each parent, then its children — matches Gantt WBS order */
    parents.forEach(function (p) {
      allRows.push({ isGroup: true, task: p });
      leaves
        .filter(function (l) { return l.parent_id === p.task_id; })
        .forEach(function (l) { allRows.push({ isGroup: false, task: l }); });
    });
    /* Orphaned leaves (unknown parent_id) go at the end */
    var parentIds = parents.map(function (p) { return p.task_id; });
    leaves
      .filter(function (l) { return parentIds.indexOf(l.parent_id) === -1; })
      .forEach(function (l) { allRows.push({ isGroup: false, task: l }); });

    var shown    = allRows.slice(0, MAX_ROWS);
    var overflow = allRows.length - shown.length;

    return React.createElement('div', { className: 'fs-prog-import__table-wrap' },
      React.createElement('table', { className: 'fs-prog-import__table' },
        React.createElement('thead', null,
          React.createElement('tr', null,
            React.createElement('th', null, 'WBS'),
            React.createElement('th', null, 'Task ID'),
            React.createElement('th', null, 'Name'),
            React.createElement('th', null, 'Start'),
            React.createElement('th', null, 'End'),
            React.createElement('th', null, 'Status'),
            React.createElement('th', null, '%'),
          ),
        ),
        React.createElement('tbody', null,
          shown.map(function (row, i) {
            var t = row.task;
            return React.createElement('tr', {
              key: t.task_id || i,
              className: row.isGroup ? 'fs-prog-import__table-group' : '',
            },
              React.createElement('td', { className: 'fs-prog-import__td-mono' }, t.wbs || '—'),
              React.createElement('td', { className: 'fs-prog-import__td-mono' }, t.task_id),
              React.createElement('td', { style: row.isGroup ? { fontWeight: 600 } : {} }, t.name),
              React.createElement('td', null, row.isGroup ? '—' : fmtDate(t.start)),
              React.createElement('td', null, row.isGroup ? '—' : fmtDate(t.end)),
              React.createElement('td', null,
                React.createElement('span', {
                  className: 'fs-prog-import__status-pill fs-prog-import__status-pill--' + t.status,
                }, STATUS_LABELS[t.status] || t.status),
              ),
              React.createElement('td', null, row.isGroup ? '—' : (t.progress_pct + '%')),
            );
          }),
          overflow > 0 && React.createElement('tr', { key: '__overflow' },
            React.createElement('td', { colSpan: 7, className: 'fs-prog-import__table-overflow' },
              '+ ' + overflow + ' more rows not shown'),
          ),
        ),
      ),
    );
  }

  /* ---- ProgrammeImportModal ----------------------------------------------- */

  function ProgrammeImportModal(props) {
    var open     = !!props.open;
    var onClose  = props.onClose  || function () {};
    var onImport = props.onImport || function () {};

    var ModalOverlay = window.FieldSight && window.FieldSight.ModalOverlay;
    var Button       = window.FieldSight && window.FieldSight.Button;

    /* phase: 'pick' | 'mapping' | 'preview' */
    var phaseHook  = React.useState('pick');
    var phase      = phaseHook[0];
    var setPhase   = phaseHook[1];

    var fileNameHook = React.useState('');
    var fileName     = fileNameHook[0];
    var setFileName  = fileNameHook[1];

    /* pendingXlsx holds { file, headers, rows } while column-mapper is shown */
    var pendingHook = React.useState(null);
    var pending     = pendingHook[0];
    var setPending  = pendingHook[1];

    var resultHook = React.useState(null);
    var result     = resultHook[0];
    var setResult  = resultHook[1];

    /* Reset state when modal is opened fresh */
    React.useEffect(function () {
      if (open) { setPhase('pick'); setFileName(''); setResult(null); setPending(null); }
    }, [open]);

    function handleFile(f) {
      setFileName(f.name);
      var ext = f.name.split('.').pop().toLowerCase();

      if (ext === 'xlsx' || ext === 'xls') {
        window.FS.api.programmeImport.parseXLSX(f).then(function (parsed) {
          if (parsed.needsMapping) {
            setPending({ file: f, headers: parsed.headers, rows: parsed.rows });
            setPhase('mapping');
          } else {
            setResult(parsed);
            setPhase('preview');
          }
        });
        return;
      }

      var isXML = /\.xml$/i.test(f.name);
      var reader = new FileReader();
      reader.onload = function (e) {
        var text   = e.target.result;
        var parsed = isXML
          ? window.FS.api.programmeImport.parseMSProjectXML(text)
          : window.FS.api.programmeImport.parseCSV(text);
        setResult(parsed);
        setPhase('preview');
      };
      reader.readAsText(f, 'utf-8');
    }

    function handleColumnMap(columnMap) {
      if (!pending) return;
      window.FS.api.programmeImport.parseXLSXWithMap(pending.file, columnMap).then(function (parsed) {
        setResult(parsed);
        setPending(null);
        setPhase('preview');
      });
    }

    function handleConfirm() {
      if (result && result.errors.length === 0) {
        onImport(result.parents, result.leaves);
        onClose();
      }
    }

    var hasErrors = result && result.errors.length > 0;
    var taskCount = result ? (result.parents.length + result.leaves.length) : 0;

    var summary = result && phase === 'preview'
      ? pluralise(result.parents.length, 'group') + ', ' + pluralise(result.leaves.length, 'task')
      : '';

    var modalTitle = phase === 'pick'    ? 'Import programme — CSV · XML · XLSX'
                   : phase === 'mapping' ? 'Map columns — ' + fileName
                   :                       'Preview import — ' + fileName;

    return ModalOverlay
      ? React.createElement(ModalOverlay, {
          open:            open,
          onClose:         onClose,
          title:           modalTitle,
          size:            'lg',
          closeOnBackdrop: phase === 'pick',
        },
          phase === 'pick'
            ? React.createElement(DropZone, { onFile: handleFile })
          : phase === 'mapping' && pending
            ? React.createElement(ColumnMapper, {
                headers:   pending.headers,
                onConfirm: handleColumnMap,
                onBack:    function () { setPhase('pick'); setPending(null); },
              })
            : React.createElement('div', { className: 'fs-prog-import__preview' },

                /* Summary bar */
                React.createElement('div', { className: 'fs-prog-import__summary' },
                  React.createElement('span', { className: 'fs-prog-import__summary-count' }, taskCount),
                  ' rows parsed — ',
                  summary,

                  /* Pick a different file */
                  React.createElement('button', {
                    type:      'button',
                    className: 'fs-prog-import__summary-change',
                    onClick:   function () { setPhase('pick'); setResult(null); },
                  }, 'Change file'),
                ),

                /* Errors + warnings */
                React.createElement(ValidationReport, {
                  errors:   result.errors,
                  warnings: result.warnings,
                }),

                /* Preview table */
                result.parents.length + result.leaves.length > 0
                  ? React.createElement(PreviewTable, {
                      parents: result.parents,
                      leaves:  result.leaves,
                    })
                  : null,

                /* Warning about full replace */
                !hasErrors && React.createElement('p', { className: 'fs-prog-import__replace-note' },
                  'Confirming will replace the entire programme with the imported data. ' +
                  'This cannot be undone (mock mode — reload resets to fixture).'),

                /* Footer actions */
                React.createElement('div', { className: 'fs-prog-import__footer' },
                  Button
                    ? React.createElement(Button, {
                        variant: 'secondary',
                        size:    'sm',
                        onClick: onClose,
                      }, 'Cancel')
                    : React.createElement('button', { type: 'button', onClick: onClose }, 'Cancel'),
                  Button
                    ? React.createElement(Button, {
                        variant:  'primary',
                        size:     'sm',
                        disabled: !!hasErrors || taskCount === 0,
                        onClick:  handleConfirm,
                      }, hasErrors ? 'Fix errors to import' : 'Import ' + taskCount + ' rows')
                    : React.createElement('button', {
                        type:     'button',
                        disabled: !!hasErrors || taskCount === 0,
                        onClick:  handleConfirm,
                      }, 'Import'),
                ),
              ),
        )
      : null; /* ModalOverlay not yet loaded */
  }

  /* --- export -------------------------------------------------------------- */
  window.FieldSight = window.FieldSight || {};
  window.FieldSight.ProgrammeImportModal = ProgrammeImportModal;

}());
