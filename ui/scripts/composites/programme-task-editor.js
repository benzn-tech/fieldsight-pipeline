/* ==========================================================================
   FieldSight ProgrammeTaskEditor — Layer 5 composite (Sprint 5.1 / 5.2)
   --------------------------------------------------------------------------
   Modal form for editing OR creating a Programme leaf task.

   mode='edit' (default, Sprint 5.1):
     Triggered by the Edit button in ProgrammeRightDetail. Commits via
     `ProgrammeProvider.editTask({task_id, patch})`.

   mode='create' (Sprint 5.2):
     Triggered by the "+ Add task" button in ProgrammeMiddleColumn header.
     Shows a WBS-group selector (parent_id) plus the same fields as edit.
     Commits via onSubmit({parentId, name, status, progress_pct, start,
     end, assignees, tags, depends_on}) — caller mints task_id and WBS
     via ProgrammeProvider.addTask.

   Form fields (both modes):
     • parent_id             (Select; create mode only)
     • name                  (Input)
     • status                (Select; 5 enum values)
     • progress_pct          (Input type=number, 0–100)
     • start / end           (Input type=date; required in create mode)
     • assignees             (Input, comma-separated underscore folders)
     • tags                  (Input, comma-separated)
     • depends_on            (checkbox grid grouped by WBS parent)

   Why a plain `Input type="date"` rather than `DatePicker`: the existing
   DatePicker is wired to the report-day fixture (it queries
   /api/dates and only enables days that have reports). Programme task
   dates are independent of report days — using DatePicker would
   surface the wrong availability hints. Native date input is correct
   and lighter-weight here. (5.4 import flow can revisit if needed.)

   Pure validatePatch(patch, taskId) — exported for node tests:
     - name non-empty
     - start <= end
     - depends_on must not contain taskId (1-step cycle; full DAG
       check lives in 5.6 cascade engine)
     - progress_pct in [0, 100]

   Exported to:
     window.FieldSight.ProgrammeTaskEditor
     window.FieldSight._programmeEditor.validatePatch     (testing hook)
     window.FieldSight._programmeEditor.diffDays          (testing hook)
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  var STATUS_OPTIONS = [
    { value: 'not_started', label: 'Not started' },
    { value: 'in_progress', label: 'In progress' },
    { value: 'completed',   label: 'Done' },
    { value: 'blocked',     label: 'Blocked' },
    { value: 'delayed',     label: 'Delayed' },
  ];

  /* ---------- Pure helpers (node-testable) ----------------------------- */

  function diffDays(a, b) {
    var ms = new Date(b + 'T00:00:00Z').getTime() - new Date(a + 'T00:00:00Z').getTime();
    return Math.round(ms / 86400000);
  }

  function validatePatch(patch, taskId) {
    var errors = {};
    if (patch.name !== undefined && !String(patch.name).trim()) {
      errors.name = 'Name is required.';
    }
    if (patch.start && patch.end && patch.start > patch.end) {
      errors.end = 'End must be on or after start.';
    }
    if (patch.depends_on && taskId && patch.depends_on.indexOf(taskId) !== -1) {
      errors.depends_on = 'A task cannot depend on itself.';
    }
    if (patch.progress_pct !== undefined) {
      var n = Number(patch.progress_pct);
      if (Number.isNaN(n) || n < 0 || n > 100) {
        errors.progress_pct = 'Progress must be 0–100.';
      }
    }
    if (Object.keys(errors).length === 0) {
      var normalized = Object.assign({}, patch);
      if (patch.start && patch.end) {
        normalized.duration_days = diffDays(patch.start, patch.end) + 1;
      }
      return { ok: true, errors: {}, normalized: normalized };
    }
    return { ok: false, errors: errors, normalized: null };
  }

  /* ---------- ChipInput (Sprint 5.7.1) --------------------------------- */
  /* Combobox-style multi-value field used by Assignees and Tags. Renders
     existing values as removable chips + a single text input bound to a
     <datalist> that autocompletes from a provided option pool. Commits
     a chip on Enter, comma, or blur; backspace on an empty draft pops
     the last chip. Free input is allowed — the datalist is a hint, not
     a constraint. The form holds these fields as `string[]` directly. */
  function ChipInput(props) {
    var values   = props.values || [];
    var options  = props.options || [];
    var label    = props.label || '';
    var hint     = props.hint || '';
    var onChange = props.onChange || function () {};

    var refDraft = React.useState('');
    var draft    = refDraft[0];
    var setDraft = refDraft[1];

    var listId = React.useMemo(function () {
      return 'fs-chip-list-' + Math.random().toString(36).slice(2, 8);
    }, []);

    function commit(raw) {
      var v = String(raw || '').trim().replace(/,$/, '');
      if (!v) { setDraft(''); return; }
      if (values.indexOf(v) === -1) onChange(values.concat([v]));
      setDraft('');
    }
    function remove(v) { onChange(values.filter(function (x) { return x !== v; })); }
    function onKeyDown(e) {
      if (e.key === 'Enter' || e.key === ',') {
        e.preventDefault();
        commit(draft);
      } else if (e.key === 'Backspace' && !draft && values.length) {
        onChange(values.slice(0, -1));
      }
    }

    /* Filter out already-selected values so the dropdown doesn't dupe. */
    var dataOptions = options.filter(function (o) { return values.indexOf(o) === -1; });

    return React.createElement('div', { className: 'fs-chip-input' },
      label
        ? React.createElement('label', { className: 'fs-chip-input__label' }, label)
        : null,
      React.createElement('div', { className: 'fs-chip-input__field' },
        values.map(function (v) {
          return React.createElement('span', {
            key: v, className: 'fs-chip-input__chip',
          },
            React.createElement('span', { className: 'fs-chip-input__chip-text' }, v),
            React.createElement('button', {
              type:        'button',
              className:   'fs-chip-input__chip-x',
              'aria-label': 'Remove ' + v,
              onClick:     function () { remove(v); },
            }, '×'),
          );
        }),
        React.createElement('input', {
          type:       'text',
          className:  'fs-chip-input__draft',
          list:       listId,
          value:      draft,
          onChange:   function (e) {
            var v = e.target.value;
            /* Trailing comma → commit the token before the comma. */
            if (v.slice(-1) === ',') { commit(v); return; }
            setDraft(v);
          },
          onKeyDown:  onKeyDown,
          onBlur:     function () { if (draft) commit(draft); },
          placeholder: values.length === 0 ? (props.placeholder || 'Type and press Enter') : '',
        }),
        React.createElement('datalist', { id: listId },
          dataOptions.map(function (o) {
            return React.createElement('option', { key: o, value: o });
          }),
        ),
      ),
      hint
        ? React.createElement('div', { className: 'fs-chip-input__hint' }, hint)
        : null,
    );
  }

  /* ---------- ProgrammeTaskEditor -------------------------------------- */

  function ProgrammeTaskEditor(props) {
    var fs           = window.FieldSight;
    var Modal        = fs.ModalOverlay;
    var Input        = fs.Input;
    var Select       = fs.Select;
    var Button       = fs.Button;

    var open     = !!props.open;
    var mode     = props.mode || 'edit';
    var task     = props.task || null;
    var leaves   = props.leaves || [];
    var parents  = props.parents || [];
    var onClose  = props.onClose  || function () {};
    var onSubmit = props.onSubmit || function () {};
    var onDelete = props.onDelete || null;

    /* Sprint 5.7.1 — auto-pool option lists for the Assignees + Tags
       chip dropdowns from the live `leaves[]`. New typing is still
       allowed (the datalist is a hint, not a constraint). */
    var assigneeOptions = React.useMemo(function () {
      var set = {};
      leaves.forEach(function (t) {
        (t.assignees || []).forEach(function (a) { if (a) set[a] = true; });
      });
      return Object.keys(set).sort();
    }, [leaves]);
    var tagOptions = React.useMemo(function () {
      var set = {};
      leaves.forEach(function (t) {
        (t.tags || []).forEach(function (a) { if (a) set[a] = true; });
      });
      return Object.keys(set).sort();
    }, [leaves]);

    /* Form state — assignees/tags now live as string[] arrays directly,
       not joined CSV strings (5.7.1 chip refactor). */
    var initial = React.useMemo(function () {
      if (mode === 'create') {
        return {
          parent_id:    parents.length > 0 ? parents[0].task_id : '',
          name:         '',
          status:       'not_started',
          progress_pct: '0',
          start:        '',
          end:          '',
          assignees:    [],
          tags:         [],
          depends_on:   [],
        };
      }
      if (!task) return null;
      return {
        name:         task.name || '',
        status:       task.status || 'not_started',
        progress_pct: String(task.progress_pct == null ? 0 : task.progress_pct),
        start:        task.start || '',
        end:          task.end   || '',
        assignees:    (task.assignees || []).slice(),
        tags:         (task.tags || []).slice(),
        depends_on:   (task.depends_on || []).slice(),
      };
    }, [mode, task && task.task_id, open]);

    var refForm = React.useState(initial);
    var form    = refForm[0];
    var setForm = refForm[1];

    var refErrors = React.useState({});
    var errors    = refErrors[0];
    var setErrors = refErrors[1];

    /* Sprint 5.3 — two-click delete confirmation.
       Resets to false whenever the modal opens or closes (via initial dep). */
    var refDeleteConfirm = React.useState(false);
    var deleteConfirm    = refDeleteConfirm[0];
    var setDeleteConfirm = refDeleteConfirm[1];

    React.useEffect(function () {
      setForm(initial);
      setErrors({});
      setDeleteConfirm(false);
    }, [initial]);

    /* In edit mode we require a task; in create mode we don't. */
    if (!open || (mode !== 'create' && !task) || !form) {
      if (Modal) {
        return React.createElement(Modal, {
          open: false, onClose: onClose,
          title: mode === 'create' ? 'Add task' : 'Edit task',
        });
      }
      return null;
    }

    function set(field, value) {
      setForm(Object.assign({}, form, (function () { var o = {}; o[field] = value; return o; })()));
    }

    function toggleDep(depId) {
      var has = form.depends_on.indexOf(depId) !== -1;
      var next = has
        ? form.depends_on.filter(function (x) { return x !== depId; })
        : form.depends_on.concat([depId]);
      set('depends_on', next);
    }

    function commit() {
      var patch = {
        name:         form.name.trim(),
        status:       form.status,
        progress_pct: Number(form.progress_pct),
        start:        form.start,
        end:          form.end,
        assignees:    form.assignees.slice(),
        tags:         form.tags.slice(),
        depends_on:   form.depends_on.slice(),
      };

      if (mode === 'create') {
        var errs = {};
        if (!patch.name)  errs.name  = 'Name is required.';
        if (!patch.start) errs.start = 'Start date is required.';
        if (!patch.end)   errs.end   = 'End date is required.';
        if (patch.start && patch.end && patch.start > patch.end) {
          errs.end = 'End must be on or after start.';
        }
        var pct = patch.progress_pct;
        if (Number.isNaN(pct) || pct < 0 || pct > 100) {
          errs.progress_pct = 'Progress must be 0–100.';
        }
        if (Object.keys(errs).length > 0) { setErrors(errs); return; }
        onSubmit({
          parentId:     form.parent_id,
          name:         patch.name,
          status:       patch.status,
          progress_pct: patch.progress_pct,
          start:        patch.start,
          end:          patch.end,
          assignees:    patch.assignees,
          tags:         patch.tags,
          depends_on:   patch.depends_on,
        });
        return;
      }

      var result = validatePatch(patch, task.task_id);
      if (!result.ok) { setErrors(result.errors); return; }
      onSubmit({ task_id: task.task_id, patch: result.normalized });
    }

    /* Group leaves by parent WBS for the depends_on checkbox grid.
       In create mode task is null, so skip the self-exclusion filter. */
    var currentTaskId = task ? task.task_id : null;
    var depGroups = parents.map(function (p) {
      return {
        parent: p,
        items:  leaves.filter(function (t) {
          return t.parent_id === p.task_id
            && (currentTaskId ? t.task_id !== currentTaskId : true);
        }),
      };
    }).filter(function (g) { return g.items.length > 0; });

    var modalTitle = mode === 'create' ? 'Add task' : ('Edit task · ' + task.task_id);

    var parentOptions = parents.map(function (p) {
      return { value: p.task_id, label: p.wbs + ' · ' + p.name };
    });

    return React.createElement(Modal, {
      open:            true,
      onClose:         onClose,
      title:           modalTitle,
      size:            'lg',
      closeOnBackdrop: false,           /* protect unsaved input */
    },
      React.createElement('div', { className: 'fs-task-editor' },

        /* WBS group selector — create mode only */
        mode === 'create' && parentOptions.length > 0
          ? React.createElement('div', { className: 'fs-task-editor__row' },
              React.createElement(Select, {
                label:    'WBS Group',
                value:    form.parent_id,
                options:  parentOptions,
                onChange: function (e) { set('parent_id', e.target.value); },
              }),
            )
          : null,

        React.createElement('div', { className: 'fs-task-editor__row' },
          React.createElement(Input, {
            label:    'Name', required: true, fullWidth: true,
            value:    form.name,
            error:    errors.name,
            onChange: function (e) { set('name', e.target.value); },
          }),
        ),

        React.createElement('div', { className: 'fs-task-editor__row fs-task-editor__row--split' },
          React.createElement(Select, {
            label:    'Status',
            value:    form.status,
            options:  STATUS_OPTIONS,
            onChange: function (e) { set('status', e.target.value); },
          }),
          React.createElement(Input, {
            label:    'Progress (%)', type: 'number',
            value:    form.progress_pct,
            error:    errors.progress_pct,
            onChange: function (e) { set('progress_pct', e.target.value); },
          }),
        ),

        React.createElement('div', { className: 'fs-task-editor__row fs-task-editor__row--split' },
          React.createElement(Input, {
            label:    'Start', type: 'date',
            value:    form.start,
            error:    errors.start,
            /* Sprint 5.7.1 — force English locale on the native date
               picker so the format stays "yyyy-mm-dd" / "dd Mmm yyyy"
               regardless of the operating system's display language
               (Chrome/Edge follow the input's `lang` for date format
               when set explicitly). */
            lang:     'en-NZ',
            onChange: function (e) { set('start', e.target.value); },
          }),
          React.createElement(Input, {
            label:    'End', type: 'date',
            value:    form.end,
            error:    errors.end,
            lang:     'en-NZ',
            onChange: function (e) { set('end', e.target.value); },
          }),
        ),

        React.createElement('div', { className: 'fs-task-editor__row' },
          React.createElement(ChipInput, {
            label:       'Assignees',
            hint:        'Type to add (existing names auto-suggest). Press Enter or comma to commit.',
            placeholder: 'e.g. Jarley_Trainor',
            values:      form.assignees,
            options:     assigneeOptions,
            onChange:    function (next) { set('assignees', next); },
          }),
        ),
        React.createElement('div', { className: 'fs-task-editor__row' },
          React.createElement(ChipInput, {
            label:       'Tags',
            hint:        'Type to add (existing tags auto-suggest). Press Enter or comma to commit.',
            placeholder: 'e.g. safety_critical',
            values:      form.tags,
            options:     tagOptions,
            onChange:    function (next) { set('tags', next); },
          }),
        ),

        /* Depends-on checkbox grid */
        React.createElement('div', { className: 'fs-task-editor__deps' },
          React.createElement('div', { className: 'fs-task-editor__deps-label' },
            'Depends on'),
          errors.depends_on
            ? React.createElement('div', { className: 'fs-task-editor__deps-error' },
                errors.depends_on)
            : null,
          depGroups.length === 0
            ? React.createElement('div', { className: 'fs-task-editor__deps-empty' },
                'No other tasks available.')
            : depGroups.map(function (g) {
                return React.createElement('div', {
                  key:       g.parent.task_id,
                  className: 'fs-task-editor__deps-group',
                },
                  React.createElement('div', { className: 'fs-task-editor__deps-group-label' },
                    g.parent.wbs + ' · ' + g.parent.name),
                  React.createElement('div', { className: 'fs-task-editor__deps-grid' },
                    g.items.map(function (t) {
                      var checked = form.depends_on.indexOf(t.task_id) !== -1;
                      return React.createElement('label', {
                        key:       t.task_id,
                        className: 'fs-task-editor__dep' + (checked ? ' fs-task-editor__dep--on' : ''),
                      },
                        React.createElement('input', {
                          type:     'checkbox',
                          checked:  checked,
                          onChange: function () { toggleDep(t.task_id); },
                        }),
                        React.createElement('span', { className: 'fs-task-editor__dep-id' },
                          t.task_id),
                        React.createElement('span', { className: 'fs-task-editor__dep-name' },
                          t.name),
                      );
                    }),
                  ),
                );
              }),
        ),

        /* Footer — delete is left-anchored (edit mode + onDelete prop only);
           Cancel/Save stay right-anchored. marginRight:auto on the delete
           wrapper pushes the remaining buttons to the right end of the
           flex row without any new CSS. */
        React.createElement('div', { className: 'fs-task-editor__footer' },
          mode === 'edit' && onDelete
            ? React.createElement('div', { style: { marginRight: 'auto' } },
                deleteConfirm
                  ? React.createElement(Button, {
                      variant: 'danger',
                      onClick: function () { onDelete(task.task_id); onClose(); },
                    }, 'Confirm delete?')
                  : React.createElement(Button, {
                      variant: 'ghost',
                      style:   { color: 'var(--color-danger-600)' },
                      onClick: function () { setDeleteConfirm(true); },
                    }, 'Delete'),
              )
            : null,
          React.createElement(Button, {
            variant: 'ghost',
            onClick: function () { setDeleteConfirm(false); onClose(); },
          }, 'Cancel'),
          React.createElement(Button, {
            variant: 'primary', onClick: commit,
          }, mode === 'create' ? 'Add task' : 'Save'),
        ),
      ),
    );
  }

  function splitCsv(s) {
    return String(s || '')
      .split(',')
      .map(function (x) { return x.trim(); })
      .filter(Boolean);
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.ProgrammeTaskEditor = ProgrammeTaskEditor;
  window.FieldSight._programmeEditor = {
    validatePatch: validatePatch,
    diffDays:      diffDays,
  };
})();
