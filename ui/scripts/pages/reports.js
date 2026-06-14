/* ==========================================================================
   FieldSight Reports Page — Sprint 2.6 (PLAN.md Phase F)
   --------------------------------------------------------------------------
   /reports — historical archive of daily / weekly / monthly Word
   reports. Reads /api/reports/history (BACKEND-CONTEXT §4.11) and
   downloads each .docx via /api/media/presigned-url.

   Middle column:
     • Type filter chips (All · Daily · Weekly · Monthly)
     • Optional "Generate report" panel (admin/pm only — gated via
       FS.canDo('report:create')). Workers can technically trigger
       their own report (BACKEND-CONTEXT §4.11) but the UI hides
       the affordance for view-only roles.
     • Reverse-chronological list of report rows: type · date · size,
       click to open in right detail.

   Right detail:
     • Type badge + date + author/site + generated-at + file size.
     • Download button — fetches a fresh presigned URL on click and
       starts the download (BACKEND-CONTEXT §7: 15-min expiry, no
       localStorage caching).
     • Regenerate button (gated) with inline confirm.

   Registers as window.FieldSight.PAGES['/reports']
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  var TYPE_LABEL = { daily: 'Daily', weekly: 'Weekly', monthly: 'Monthly' };
  var TYPE_TONE  = { daily: 'info', weekly: 'success', monthly: 'accent' };

  function fmtSize(bytes) {
    if (bytes == null) return '';
    if (bytes < 1024)        return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + ' KB';
    return (bytes / 1024 / 1024).toFixed(1) + ' MB';
  }

  function fmtDate(yyyymmdd) {
    if (!yyyymmdd) return '';
    var p = yyyymmdd.split('-').map(Number);
    var d = new Date(Date.UTC(p[0], p[1] - 1, p[2]));
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return d.getUTCDate() + ' ' + months[d.getUTCMonth()] + ' ' + p[0];
  }

  function fmtGeneratedAt(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toISOString().replace('T', ' ').slice(0, 16) + ' UTC';
  }

  /* Trigger a download for a presigned URL. We must NOT cache the URL
     (15-min expiry, BACKEND-CONTEXT §7) — fetch fresh on every click. */
  async function downloadReport(report) {
    try {
      var res = await window.FS.api.media.presignedUrl(report.key);
      var a = document.createElement('a');
      a.href = res.url;
      a.target = '_blank';
      a.rel = 'noopener';
      /* download attr makes browsers prefer the filename in the URL or
         the last path segment when same-origin; the API serves with
         Content-Disposition so the real backend handles the filename. */
      a.download = '';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    } catch (err) {
      console.error('[reports] download failed', err);
    }
  }

  /* Sprint 8.10.3 — Batch export.
     Iterates the current month's reports, fetches presigned URLs, and
     triggers each download with a short stagger so the browser doesn't
     drop concurrent navigations. Caps at MAX_BATCH; otherwise we'd
     hammer the presign endpoint. */
  var MAX_BATCH = 10;
  async function batchExport(rows) {
    if (!rows || !rows.length) return;
    var capped = rows.slice(0, MAX_BATCH);
    if (rows.length > MAX_BATCH && window.FS && window.FS.toast) {
      window.FS.toast.show({
        message: 'Exporting first ' + MAX_BATCH + ' of ' + rows.length + ' reports',
        tone:    'warning',
      });
    } else if (window.FS && window.FS.toast) {
      window.FS.toast.show({
        message: 'Exporting ' + capped.length + ' report' + (capped.length === 1 ? '' : 's') + '…',
        tone:    'info',
      });
    }
    for (var i = 0; i < capped.length; i++) {
      try {
        await downloadReport(capped[i]);
      } catch (e) { /* individual failure already logged in downloadReport */ }
      /* 250 ms stagger keeps Chrome/Safari happy with many a.click()s */
      await new Promise(function (resolve) { setTimeout(resolve, 250); });
    }
    if (window.FS && window.FS.toast) {
      window.FS.toast.show({ message: 'Export complete', tone: 'success' });
    }
  }

  /* Filter rows down to the current calendar month (YYYY-MM). */
  function thisMonthRows(rows) {
    var prefix = (new Date()).toISOString().slice(0, 7);   /* e.g. "2026-05" */
    return rows.filter(function (r) {
      return r.date && r.date.slice(0, 7) === prefix;
    });
  }

  /* =====================================================================
     B.6 — TemplateFormatSelector
     Shows personal templates first, then org; defaults to the active one.
     Returns null when no templates exist (keeps the generate card clean).
     ===================================================================== */
  function TemplateFormatSelector(props) {
    /* props: onChange(templateId|null) */
    var loadRef  = React.useState({ status: 'loading', personal: [], org: [] });
    var load     = loadRef[0]; var setLoad = loadRef[1];

    var selRef   = React.useState('');
    var selId    = selRef[0]; var setSelId = selRef[1];

    React.useEffect(function () {
      if (!window.FS || !window.FS.api || !window.FS.api.templates) {
        setLoad({ status: 'ok', personal: [], org: [] });
        return;
      }
      setLoad({ status: 'loading', personal: [], org: [] });
      window.FS.api.templates.list().then(function (res) {
        var all = (res.templates || []);
        function sortActive(arr) {
          return arr.slice().sort(function (a, b) { return (b.active ? 1 : 0) - (a.active ? 1 : 0); });
        }
        var personal = sortActive(all.filter(function (t) { return t.scope === 'personal'; }));
        var org      = sortActive(all.filter(function (t) { return t.scope === 'org'; }));
        setLoad({ status: 'ok', personal: personal, org: org });
        /* Default: active personal first, then active org */
        var activePers = personal.find(function (t) { return t.active; });
        var activeOrg  = org.find(function (t) { return t.active; });
        var def = (activePers || activeOrg || {}).id || '';
        setSelId(def);
        if (props.onChange) props.onChange(def || null);
      }).catch(function () {
        setLoad({ status: 'ok', personal: [], org: [] });
      });
    }, []);

    function handleChange(e) {
      var val = e.target.value;
      setSelId(val);
      if (props.onChange) props.onChange(val || null);
    }

    if (load.status === 'loading') return null;
    if (!load.personal.length && !load.org.length) return null;

    return React.createElement('div', { className: 'fs-reports__format' },
      React.createElement('label', {
        className: 'fs-reports__format-label',
        htmlFor:   'rpt-format-sel',
      }, 'Output format'),
      React.createElement('select', {
        id:        'rpt-format-sel',
        className: 'fs-reports__format-select',
        value:     selId,
        onChange:  handleChange,
      },
        React.createElement('option', { value: '' }, '— Standard (FieldSight default) —'),
        load.personal.length
          ? React.createElement('optgroup', { label: 'My templates' },
              load.personal.map(function (t) {
                return React.createElement('option', { key: t.id, value: t.id },
                  t.title + (t.active ? ' ✓' : '') + '  (' + (TYPE_LABEL[t.report_type] || t.report_type) + ')');
              }),
            )
          : null,
        load.org.length
          ? React.createElement('optgroup', { label: 'Org templates' },
              load.org.map(function (t) {
                return React.createElement('option', { key: t.id, value: t.id },
                  t.title + (t.active ? ' ✓' : '') + '  (' + (TYPE_LABEL[t.report_type] || t.report_type) + ')');
              }),
            )
          : null,
      ),
    );
  }

  /* =====================================================================
     ReportsMiddleColumn
     ===================================================================== */
  function ReportsMiddleColumn(props) {
    var fs = window.FieldSight;
    var Button = fs.Button;
    var Badge  = fs.Badge;
    var Card   = fs.Card;

    var caller = (window.AuthMock && window.AuthMock.currentUser) || {};
    var canRegenerate = window.FS.can(caller, window.FS.P('report','create'));

    var refState = React.useState({ status: 'loading', rows: [] });
    var state    = refState[0];
    var setState = refState[1];

    var retryRef   = React.useState(0);
    var retryCount = retryRef[0];
    var setRetry   = retryRef[1];

    var refFilter = React.useState('all');
    var filter    = refFilter[0];
    var setFilter = refFilter[1];

    /* Inline regenerate panel: 'closed' | 'pick' | 'submitting' | 'done' */
    var refReg = React.useState({ phase: 'closed' });
    var reg    = refReg[0];
    var setReg = refReg[1];

    /* B.6 — selected output-format template ID (null = standard) */
    var refTpl   = React.useState(null);
    var selTplId = refTpl[0];
    var setSelTplId = refTpl[1];

    React.useEffect(function () {
      var cancelled = false;
      window.FS.api.reports.getReportsHistory(50).then(function (res) {
        if (cancelled) return;
        /* P-12 — surface 403 as an empathetic state instead of a
           generic error. The reports archive is broadly readable for
           admin/pm/site_manager but a worker / viewer can be denied. */
        if (res && res._accessDenied) {
          setState({ status: 'access_denied', message: res.error, rows: [] });
          return;
        }
        var sorted = (res.reports || []).slice().sort(function (a, b) {
          return (b.generated_at || '').localeCompare(a.generated_at || '');
        });
        setState({ status: 'ok', rows: sorted });
      }).catch(function (err) {
        if (cancelled) return;
        setState({ status: 'error', error: { code: (err && err.status) || 0, message: (err && err.message) || 'Could not load reports', retryable: true }, retry: function () { setRetry(function (n) { return n + 1; }); }, rows: [] });
      });
      return function () { cancelled = true; };
    }, [retryCount]);

    function regenerate(type) {
      setReg({ phase: 'submitting', type: type });
      var payload = { report_type: type, force: true };
      if (selTplId) payload.template_id = selTplId;
      window.FS.api.reports.regenerate(payload).then(function (res) {
        setReg({ phase: 'done', type: type, message: res.message });
        /* Clear the success message after a moment without dismissing
           the panel — gives the user a beat to see the confirmation. */
        setTimeout(function () { setReg({ phase: 'closed' }); }, 2400);
      }).catch(function (err) {
        setReg({ phase: 'error', type: type, error: err });
      });
    }

    var filtered = filter === 'all'
      ? state.rows
      : state.rows.filter(function (r) { return r.type === filter; });

    var counts = {
      all:     state.rows.length,
      daily:   state.rows.filter(function (r) { return r.type === 'daily';   }).length,
      weekly:  state.rows.filter(function (r) { return r.type === 'weekly';  }).length,
      monthly: state.rows.filter(function (r) { return r.type === 'monthly'; }).length,
    };

    function FilterChip(props) {
      var active = filter === props.value;
      return React.createElement('button', {
        type: 'button',
        className: 'fs-reports__chip' + (active ? ' fs-reports__chip--active' : ''),
        onClick: function () { setFilter(props.value); },
        'aria-pressed': active,
      },
        props.label,
        React.createElement('span', { className: 'fs-reports__chip-count' },
          counts[props.value] || 0),
      );
    }

    var selectedKey = props.selectedItem && props.selectedItem.kind === 'report'
      ? props.selectedItem.key
      : null;

    /* Sprint 8.10.3 — batch export state */
    var refExport = React.useState({ phase: 'idle' });
    var exp = refExport[0];
    var setExp = refExport[1];

    var monthRows = thisMonthRows(state.rows || []);

    function onBatchExport() {
      if (exp.phase === 'submitting') return;
      setExp({ phase: 'submitting' });
      batchExport(monthRows).finally(function () {
        setExp({ phase: 'idle' });
      });
    }

    return React.createElement('div', { className: 'fs-reports' },

      /* Header */
      React.createElement('div', { className: 'fs-reports__header' },
        React.createElement('div', { className: 'fs-reports__header-main' },
          React.createElement('h2', { className: 'fs-reports__title' }, 'Reports archive'),
          React.createElement('div', { className: 'fs-reports__subtitle' },
            'Daily, weekly and monthly report history. Click a row to download.'),
        ),
        /* Sprint 8.10.3 — batch export trigger */
        monthRows.length > 0
          ? React.createElement(Button, {
              size:      'sm',
              variant:   'secondary',
              leftIcon:  'download',
              onClick:   onBatchExport,
              disabled:  exp.phase === 'submitting',
            }, exp.phase === 'submitting'
                ? 'Exporting…'
                : 'Export all (this month, ' + monthRows.length + ')')
          : null,
      ),

      /* Filter chips */
      React.createElement('div', { className: 'fs-reports__chips' },
        React.createElement(FilterChip, { value: 'all',     label: 'All' }),
        React.createElement(FilterChip, { value: 'daily',   label: 'Daily' }),
        React.createElement(FilterChip, { value: 'weekly',  label: 'Weekly' }),
        React.createElement(FilterChip, { value: 'monthly', label: 'Monthly' }),
      ),

      /* Generate panel (gated) */
      canRegenerate ? React.createElement(Card, {
        padding: 'md', className: 'fs-reports__regen',
      },
        React.createElement(Card.Body, null,
          React.createElement('div', { className: 'fs-reports__regen-row' },
            React.createElement('div', { className: 'fs-reports__regen-main' },
              React.createElement('div', { className: 'fs-reports__regen-title' },
                'Generate report'),
              React.createElement('div', { className: 'fs-reports__regen-body' },
                'Queue a fresh report; existing copies are overwritten.'),
            ),
            reg.phase === 'closed' ? React.createElement('div', { className: 'fs-reports__regen-actions' },
              React.createElement(Button, {
                size: 'sm', variant: 'secondary',
                onClick: function () { regenerate('daily'); },
              }, 'Daily'),
              React.createElement(Button, {
                size: 'sm', variant: 'secondary',
                onClick: function () { regenerate('weekly'); },
              }, 'Weekly'),
              React.createElement(Button, {
                size: 'sm', variant: 'secondary',
                onClick: function () { regenerate('monthly'); },
              }, 'Monthly'),
            ) : null,

            reg.phase === 'submitting' ? React.createElement('div', {
              className: 'fs-reports__regen-msg',
            }, 'Queueing ' + reg.type + '…') : null,

            reg.phase === 'done' ? React.createElement('div', {
              className: 'fs-reports__regen-msg fs-reports__regen-msg--ok',
            }, '✓ ' + reg.message) : null,

            reg.phase === 'error' ? React.createElement('div', {
              className: 'fs-reports__regen-msg fs-reports__regen-msg--err',
            }, 'Failed: ' + (reg.error && reg.error.message || 'unknown')) : null,
          ),
          /* B.6 output-format selector */
          React.createElement(TemplateFormatSelector, { onChange: setSelTplId }),
        ),
      ) : null,

      /* List */
      state.status === 'loading'
        ? React.createElement('div', { className: 'fs-reports__loading' }, 'Loading reports…')
        : state.status === 'error'
        ? (window.FieldSight.ErrorBanner
            ? React.createElement(window.FieldSight.ErrorBanner, {
                message:   (state.error && state.error.message) || 'Could not load reports',
                retryable: true,
                onRetry:   state.retry,
              })
            : React.createElement('div', { className: 'fs-reports__empty' }, 'Could not load reports.'))
        : state.status === 'access_denied'
        ? (window.FieldSight.AccessDenied
            ? React.createElement(window.FieldSight.AccessDenied, {
                scope:   'the report archive',
                message: state.message,
              })
            : React.createElement('div', { className: 'fs-reports__empty' },
                'Access denied.'))
        : filtered.length === 0
        ? React.createElement('div', { className: 'fs-reports__empty' },
            'No ' + (filter === 'all' ? '' : filter + ' ') + 'reports yet.')
        : React.createElement('div', { className: 'fs-reports__list' },
            filtered.map(function (r) {
              var selected = selectedKey === r.key;
              return React.createElement('button', {
                key:       r.key,
                type:      'button',
                className: 'fs-reports__row' + (selected ? ' fs-reports__row--selected' : ''),
                onClick:   function () {
                  if (props.onSelect) {
                    props.onSelect({
                      kind:         'report',
                      id:           r.key,
                      key:          r.key,
                      type:         r.type,
                      date:         r.date,
                      generated_at: r.generated_at,
                      size:         r.size,
                      author:       r.author,
                      site:         r.site,
                    });
                  }
                },
              },
                React.createElement(Badge, {
                  tone:    TYPE_TONE[r.type] || 'neutral',
                  size:    'sm',
                  variant: 'subtle',
                  className: 'fs-reports__row-type',
                }, TYPE_LABEL[r.type] || r.type),

                React.createElement('div', { className: 'fs-reports__row-main' },
                  React.createElement('div', { className: 'fs-reports__row-date' },
                    fmtDate(r.date)),
                  React.createElement('div', { className: 'fs-reports__row-meta' },
                    (r.author || '—') + ' · ' + fmtGeneratedAt(r.generated_at)),
                ),

                React.createElement('div', { className: 'fs-reports__row-size' },
                  fmtSize(r.size)),
              );
            }),
          ),
    );
  }

  /* =====================================================================
     ReportsRightDetail
     ===================================================================== */
  function ReportsRightDetail(props) {
    var fs       = window.FieldSight;
    var Button   = fs.Button;
    var Badge    = fs.Badge;
    var IconBtn  = fs.IconButton;

    var sel = props.selectedItem;
    var caller = (window.AuthMock && window.AuthMock.currentUser) || {};
    var canRegenerate = window.FS.can(caller, window.FS.P('report','create'));

    var refConfirm = React.useState({ phase: 'idle' });
    var conf = refConfirm[0];
    var setConf = refConfirm[1];

    /* Reset the confirm state whenever a new report is selected. */
    React.useEffect(function () {
      setConf({ phase: 'idle' });
    }, [sel && sel.id]);

    if (!sel || sel.kind !== 'report') {
      return React.createElement('div', { className: 'fs-reports-detail__placeholder' },
        React.createElement('div', { className: 'fs-reports-detail__placeholder-title' },
          'Select a report'),
        React.createElement('div', { className: 'fs-reports-detail__placeholder-body' },
          'Pick any row to download or regenerate.'),
      );
    }

    function onDownload() {
      downloadReport(sel);
    }

    function onConfirmRegenerate() {
      setConf({ phase: 'submitting' });
      window.FS.api.reports.regenerate({
        report_type: sel.type, date: sel.date, force: true,
      }).then(function (res) {
        setConf({ phase: 'done', message: res.message });
      }).catch(function (err) {
        setConf({ phase: 'error', error: err });
      });
    }

    return React.createElement('div', { className: 'fs-reports-detail' },

      React.createElement('div', { className: 'fs-reports-detail__header' },
        React.createElement('div', { className: 'fs-reports-detail__header-main' },
          React.createElement('div', { className: 'fs-reports-detail__date' },
            fmtDate(sel.date)),
          React.createElement('div', { className: 'fs-reports-detail__metaline' },
            React.createElement(Badge, {
              tone:    TYPE_TONE[sel.type] || 'neutral',
              size:    'sm',
              variant: 'subtle',
            }, TYPE_LABEL[sel.type] || sel.type),
            sel.site ? React.createElement('span', {
              className: 'fs-reports-detail__site',
            }, sel.site) : null,
          ),
        ),
        IconBtn ? React.createElement(IconBtn, {
          icon: 'x', ariaLabel: 'Close detail', size: 'sm',
          onClick: function () { if (props.onClose) props.onClose(); },
        }) : null,
      ),

      React.createElement('div', { className: 'fs-reports-detail__rows' },
        React.createElement(DetailRow, {
          label: 'Generated', value: fmtGeneratedAt(sel.generated_at),
        }),
        React.createElement(DetailRow, {
          label: 'Author',    value: sel.author || '—',
        }),
        React.createElement(DetailRow, {
          label: 'File',
          value: sel.key.split('/').pop(),
          mono:  true,
        }),
        React.createElement(DetailRow, {
          label: 'Size',      value: fmtSize(sel.size),
        }),
      ),

      /* Action row */
      React.createElement('div', { className: 'fs-reports-detail__actions' },
        React.createElement(Button, {
          leftIcon: 'download', size: 'sm',
          onClick: onDownload,
        }, 'Download .docx'),

        canRegenerate
          ? (conf.phase === 'idle'
              ? React.createElement(Button, {
                  variant: 'secondary', size: 'sm',
                  onClick: function () { setConf({ phase: 'confirm' }); },
                }, 'Regenerate')
              : conf.phase === 'confirm'
              ? React.createElement('div', { className: 'fs-reports-detail__confirm' },
                  React.createElement('span', null, 'Overwrite this report?'),
                  React.createElement(Button, {
                    size: 'sm', variant: 'danger',
                    onClick: onConfirmRegenerate,
                  }, 'Yes, regenerate'),
                  React.createElement(Button, {
                    size: 'sm', variant: 'ghost',
                    onClick: function () { setConf({ phase: 'idle' }); },
                  }, 'Cancel'),
                )
              : conf.phase === 'submitting'
              ? React.createElement('span', { className: 'fs-reports-detail__msg' },
                  'Queueing…')
              : conf.phase === 'done'
              ? React.createElement('span', {
                  className: 'fs-reports-detail__msg fs-reports-detail__msg--ok',
                }, '✓ ' + conf.message)
              : React.createElement('span', {
                  className: 'fs-reports-detail__msg fs-reports-detail__msg--err',
                }, 'Failed: ' + (conf.error && conf.error.message || 'unknown')))
          : null,
      ),
    );
  }

  function DetailRow(props) {
    return React.createElement('div', { className: 'fs-reports-detail__row' },
      React.createElement('div', { className: 'fs-reports-detail__row-label' },
        props.label),
      React.createElement('div', {
        className: 'fs-reports-detail__row-value' + (props.mono ? ' fs-reports-detail__row-value--mono' : ''),
      }, props.value),
    );
  }

  /* ---------- Register --------------------------------------------------- */
  if (!window.FieldSight) window.FieldSight = {};
  if (!window.FieldSight.PAGES) window.FieldSight.PAGES = {};
  window.FieldSight.PAGES['/reports'] = {
    Middle: ReportsMiddleColumn,
    Right:  ReportsRightDetail,
  };

})();
