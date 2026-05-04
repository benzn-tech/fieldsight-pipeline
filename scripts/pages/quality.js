/* ==========================================================================
   FieldSight Quality Page — Sprint 6.3 (middle) / 6.4 (right detail)
   --------------------------------------------------------------------------
   /quality — cross-day rollup of quality_and_compliance items + topics
   tagged category==='quality'. Reads via the Sprint 6.0 compliance
   aggregator. Mirrors /safety (Sprint 6.1/6.2) — same provider shape,
   same range toolbar, same KPI strip + grouped list pattern.

   Differences from /safety:
     • Items have a real `status` field from the fixture
       ('completed', 'concern', 'observed', etc) — no synthetic 'open'
     • `follow_up_needed` flag drives the warning KPI bucket
     • Rows render as plain Cards (no SafetyFlagRow equivalent — the
       quality item shape is simpler: title + details + status badge)

   Registers as window.FieldSight.PAGES['/quality']
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  var DEFAULT_DAYS = 7;

  /* ---------- Helpers --------------------------------------------------- */

  function fmtDate(yyyymmdd) {
    if (!yyyymmdd) return '';
    var p = String(yyyymmdd).split('-').map(Number);
    if (p.length !== 3) return yyyymmdd;
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return p[2] + ' ' + months[p[1] - 1] + ' ' + p[0];
  }

  function fmtDateLong(yyyymmdd) {
    if (!yyyymmdd) return '';
    var p = String(yyyymmdd).split('-').map(Number);
    if (p.length !== 3) return yyyymmdd;
    var months = ['January','February','March','April','May','June',
                  'July','August','September','October','November','December'];
    var days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    var d = new Date(Date.UTC(p[0], p[1] - 1, p[2]));
    return days[d.getUTCDay()] + ', ' + p[2] + ' ' + months[p[1] - 1] + ' ' + p[0];
  }

  function groupByDate(rows) {
    var byDate = {};
    rows.forEach(function (r) {
      if (!byDate[r.date]) byDate[r.date] = [];
      byDate[r.date].push(r);
    });
    return Object.keys(byDate).sort().reverse().map(function (date) {
      return { date: date, rows: byDate[date] };
    });
  }

  /* Maps the fixture's status string → a Badge tone. Keep the set tight
     so we pick a deliberate tone on every entry; unknown shapes fall
     through to neutral. */
  function statusTone(status) {
    switch ((status || '').toLowerCase()) {
      case 'completed': return 'success';
      case 'pass':      return 'success';
      case 'concern':   return 'warning';
      case 'fail':      return 'danger';
      case 'blocked':   return 'danger';
      case 'observed':  return 'info';
      default:          return 'neutral';
    }
  }

  function totalsFromRows(rows) {
    var sites = {};
    var followUp = 0, completed = 0;
    rows.forEach(function (r) {
      if (r.site) sites[r.site] = true;
      if (r.follow_up_needed) followUp += 1;
      if ((r.status || '').toLowerCase() === 'completed' ||
          (r.status || '').toLowerCase() === 'pass') completed += 1;
    });
    return {
      total:     rows.length,
      followUp:  followUp,
      sites:     Object.keys(sites).length,
      completed: completed,
    };
  }

  /* ---------- QualityContext ------------------------------------------- */

  var QualityContext = React.createContext(null);

  function QualityProvider(props) {
    var caller = (window.AuthMock && window.AuthMock.currentUser) || {};
    var depKey = (caller.name || '') + '|' + (caller.role || '') + '|' + (caller.isAdmin ? 'admin' : '');

    var refMode = React.useState('week');
    var mode    = refMode[0];
    var setMode = refMode[1];

    var refDay = React.useState(window.FS.api.todayNZDT());
    var day    = refDay[0];
    var setDay = refDay[1];

    var refState = React.useState({ status: 'loading' });
    var state    = refState[0];
    var setState = refState[1];

    var retryRef   = React.useState(0);
    var retryCount = retryRef[0];
    var setRetry   = retryRef[1];

    var today = window.FS.api.todayNZDT();
    var range;
    if (mode === 'week') {
      range = { from: window.FS.api.addDaysISO(today, -(DEFAULT_DAYS - 1)), to: today };
    } else if (mode === 'today') {
      range = { from: today, to: today };
    } else {
      range = { from: day, to: day };
    }

    React.useEffect(function () {
      var cancelled = false;
      setState({ status: 'loading' });

      window.FS.api.compliance.getQualityRange({
        from: range.from, to: range.to,
      }).then(function (res) {
        if (cancelled) return;
        if (res && res._accessDenied) {
          setState({ status: 'access_denied', message: res.error });
          return;
        }
        var rows = (res && res.rows) || [];
        setState({
          status: 'ok',
          rows:   rows,
          from:   range.from,
          to:     range.to,
          totals: totalsFromRows(rows),
          groups: groupByDate(rows),
          dates:  (res && res.dates) || [],
          user:   res.user || null,
        });
      }).catch(function (err) {
        if (cancelled) return;
        setState({ status: 'error', error: { code: (err && err.status) || 0, message: (err && err.message) || 'Could not load quality data', retryable: true }, retry: function () { setRetry(function (n) { return n + 1; }); } });
      });

      return function () { cancelled = true; };
    }, [depKey, mode, day, retryCount]);

    var refSel = React.useState(null);
    var sel    = refSel[0];
    var setSel = refSel[1];

    var refCreate  = React.useState(false);
    var showCreate = refCreate[0];
    var setShowCreate = refCreate[1];

    var ctx = {
      state:         state,
      setState:      setState,
      mode:          mode,
      day:           day,
      setMode:       function (m) { setSel(null); setMode(m); },
      setDay:        function (d) { setSel(null); setDay(d); setMode('day'); },
      selectedItem:  sel,
      setSelected:   setSel,
      showCreate:    showCreate,
      setShowCreate: setShowCreate,
      caller:        caller,
    };
    return React.createElement(QualityContext.Provider, { value: ctx },
      props.children);
  }

  /* ---------- Range toolbar -------------------------------------------- */

  function RangeToolbar(props) {
    var DatePicker = window.FieldSight.DatePicker;
    var ctx = props.ctx;
    var refOpen = React.useState(false);
    var open    = refOpen[0];
    var setOpen = refOpen[1];

    function chip(key, label, isActive) {
      return React.createElement('button', {
        key:       key,
        type:      'button',
        className: 'fs-quality__chip' + (isActive ? ' fs-quality__chip--active' : ''),
        onClick:   function () {
          if (key === 'pick') { setOpen(!open); return; }
          ctx.setMode(key);
          setOpen(false);
        },
      }, label);
    }

    return React.createElement('div', { className: 'fs-quality__toolbar' },
      React.createElement('div', { className: 'fs-quality__chips' },
        chip('today', 'Today',         ctx.mode === 'today'),
        chip('week',  'Last 7 days',   ctx.mode === 'week'),
        chip('pick',  ctx.mode === 'day' ? fmtDate(ctx.day) : 'Pick date…',
             ctx.mode === 'day'),
      ),
      open && DatePicker
        ? React.createElement('div', { className: 'fs-quality__picker-wrap' },
            React.createElement(DatePicker, {
              date:        ctx.day,
              onChange:    function (d) {
                ctx.setDay(d);
                setOpen(false);
              },
              monthsRange: 3,
              inline:      true,
            }))
        : null,
    );
  }

  /* ---------- Middle column -------------------------------------------- */

  function QualityMiddleColumn(props) {
    var fs                  = window.FieldSight;
    var KpiStrip            = fs.KpiStrip;
    var StatCard            = fs.StatCard;
    var Badge               = fs.Badge;
    var AccessDenied        = fs.AccessDenied;
    var QualityCreateModal  = fs.QualityCreateModal;
    var Button              = fs.Button;

    var ctx = React.useContext(QualityContext);
    if (!ctx) {
      console.warn('[QualityMiddleColumn] QualityContext missing');
      return null;
    }
    var state    = ctx.state;
    var onSelect = props.onSelect || function () {};

    var caller    = ctx.caller || {};
    var canCreate = !!(window.FS && window.FS.can &&
      (window.FS.can(caller, 'quality:manage') ||
       window.FS.can(caller, 'site:manage') ||
       (caller.isAdmin)));

    var logBtn = (canCreate && QualityCreateModal)
      ? React.createElement('button', {
          type:      'button',
          className: 'fs-quality__log-btn',
          onClick:   function () { ctx.setShowCreate(true); },
        }, '+ Log Item')
      : null;

    var header = React.createElement('div', { className: 'fs-quality__header' },
      React.createElement('div', { className: 'fs-quality__header-top' },
        React.createElement('div', null,
          React.createElement('h2', { className: 'fs-quality__title' }, 'Quality'),
          React.createElement('div', { className: 'fs-quality__subtitle' },
            'Quality & compliance items across your accessible reports'),
        ),
        logBtn,
      ),
    );
    var toolbar = React.createElement(RangeToolbar, { ctx: ctx });

    if (state.status === 'loading') {
      return React.createElement('div', { className: 'fs-quality' },
        header, toolbar,
        React.createElement('div', { className: 'fs-quality__loading' },
          'Loading quality data…'),
      );
    }
    if (state.status === 'error') {
      var ErrorBanner = window.FieldSight.ErrorBanner;
      return React.createElement('div', { className: 'fs-quality' },
        header, toolbar,
        ErrorBanner
          ? React.createElement(ErrorBanner, {
              message:   (state.error && state.error.message) || 'Could not load quality data',
              retryable: true,
              onRetry:   state.retry,
            })
          : React.createElement('div', { className: 'fs-quality__empty' },
              (state.error && state.error.message) || 'Could not load quality data'),
      );
    }
    if (state.status === 'access_denied') {
      return React.createElement('div', { className: 'fs-quality' },
        header,
        AccessDenied
          ? React.createElement(AccessDenied, {
              scope:   'quality data',
              message: state.message,
            })
          : React.createElement('div', null, 'Access denied.'),
      );
    }

    var totals = state.totals || { total: 0, followUp: 0, sites: 0, completed: 0 };
    var groups = state.groups || [];
    var rangeLabel = state.from === state.to
      ? fmtDate(state.from)
      : fmtDate(state.from) + ' → ' + fmtDate(state.to);

    function handleNewItem(newItem) {
      ctx.setShowCreate(false);
      if (ctx.setState && newItem) {
        ctx.setState(function (s) {
          if (s.status !== 'ok') return s;
          var updatedRows = [newItem].concat(s.rows || []);
          return Object.assign({}, s, {
            rows:   updatedRows,
            totals: totalsFromRows(updatedRows),
            groups: groupByDate(updatedRows),
          });
        });
      }
    }

    return React.createElement('div', { className: 'fs-quality' },
      header,
      toolbar,

      /* Create item modal (Sprint 8.1.3) */
      ctx.showCreate && QualityCreateModal
        ? React.createElement(QualityCreateModal, {
            siteId:    state.user || '',
            onSuccess: handleNewItem,
            onCancel:  function () { ctx.setShowCreate(false); },
          })
        : null,

      React.createElement('div', { className: 'fs-quality__meta' },
        totals.total + (totals.total === 1 ? ' item · ' : ' items · ') + rangeLabel),

      React.createElement(KpiStrip, null,
        React.createElement(StatCard, { value: totals.total,    label: 'Total items' }),
        React.createElement(StatCard, {
          value: totals.followUp, label: 'Follow-up',
          tone:  totals.followUp > 0 ? 'warning' : 'neutral',
        }),
        React.createElement(StatCard, { value: totals.sites,     label: 'Sites' }),
        React.createElement(StatCard, {
          value: totals.completed, label: 'Completed',
          tone:  totals.completed > 0 ? 'success' : 'neutral',
        }),
      ),

      groups.length === 0
        ? React.createElement('div', { className: 'fs-quality__empty' },
            'No quality items in this window.')
        : React.createElement('div', { className: 'fs-quality__groups' },
            groups.map(function (g) {
              return React.createElement('div', { key: g.date, className: 'fs-quality__group' },
                React.createElement('div', { className: 'fs-quality__group-header' },
                  React.createElement('span', { className: 'fs-quality__group-date' },
                    fmtDateLong(g.date)),
                  React.createElement('span', { className: 'fs-quality__group-count' },
                    g.rows.length + (g.rows.length === 1 ? ' item' : ' items')),
                ),
                React.createElement('div', { className: 'fs-quality__group-rows' },
                  g.rows.map(function (row) {
                    var isSel = ctx.selectedItem && ctx.selectedItem.id === row.id;
                    return React.createElement('button', {
                      key:       row.id,
                      type:      'button',
                      className: 'fs-quality__row-btn'
                        + (isSel ? ' fs-quality__row-btn--active' : ''),
                      onClick:   function () {
                        ctx.setSelected(row);
                        onSelect({ kind: 'quality_item', id: row.id, row: row });
                      },
                    },
                      React.createElement('div', { className: 'fs-quality__row' },
                        React.createElement('div', { className: 'fs-quality__row-main' },
                          React.createElement('div', { className: 'fs-quality__row-title' },
                            row.item),
                          row.details
                            ? React.createElement('div', { className: 'fs-quality__row-details' },
                                row.details)
                            : null,
                        ),
                        React.createElement('div', { className: 'fs-quality__row-status' },
                          React.createElement(Badge, {
                            tone:    statusTone(row.status), size: 'sm',
                            variant: 'subtle',
                          }, (row.status || '').charAt(0).toUpperCase() +
                             (row.status || '').slice(1) || 'Unknown'),
                          row.follow_up_needed
                            ? React.createElement(Badge, {
                                tone: 'warning', size: 'sm', variant: 'outline',
                              }, 'Follow-up')
                            : null,
                        ),
                      ),
                    );
                  }),
                ),
              );
            }),
          ),
    );
  }

  /* ---------- Right detail (Sprint 6.4) -------------------------------- */

  function DetailRow(props) {
    return React.createElement('div', { className: 'fs-quality-detail__row' },
      React.createElement('div', { className: 'fs-quality-detail__row-label' },
        props.label),
      React.createElement('div', { className: 'fs-quality-detail__row-value' },
        props.value),
    );
  }

  function QualityRightDetail(props) {
    var fs       = window.FieldSight;
    var Card     = fs.Card;
    var Badge    = fs.Badge;
    var Button   = fs.Button;
    var IconBtn  = fs.IconButton;

    var ctx = React.useContext(QualityContext);
    var sel = ctx && ctx.selectedItem;

    /* Lazy-fetch related action_items from the source topic — only
       applies when the row was sourced from a quality-tagged topic
       (topic_id >= 0). Report-level qc_items have topic_id = -1. */
    var refLinks = React.useState({ status: 'idle', items: [] });
    var linksS   = refLinks[0];
    var setLinks = refLinks[1];

    React.useEffect(function () {
      if (!sel || sel.topic_id == null || sel.topic_id < 0 || !sel.date) {
        setLinks({ status: 'ok', items: [] });
        return undefined;
      }
      var cancelled = false;
      setLinks({ status: 'loading', items: [] });

      window.FS.api.timeline.getTimeline({ date: sel.date, user: sel.user_folder })
        .then(function (r) {
          if (cancelled) return;
          if (!r || r._notFound || r.available_users) {
            setLinks({ status: 'ok', items: [] });
            return;
          }
          var topic = (r.topics || []).filter(function (t) {
            return t.topic_id === sel.topic_id;
          })[0];
          var actions = topic ? (topic.action_items || []) : [];
          setLinks({
            status: 'ok',
            items:  actions.map(function (a, idx) {
              return {
                action_index: idx,
                text:         a.action,
                responsible:  a.responsible || null,
                priority:     a.priority || null,
              };
            }),
          });
        })
        .catch(function () {
          if (!cancelled) setLinks({ status: 'error', items: [] });
        });

      return function () { cancelled = true; };
    }, [sel && sel.id]);

    if (!sel) {
      return React.createElement('div', { className: 'fs-quality-detail__placeholder' },
        React.createElement('div', { className: 'fs-quality-detail__placeholder-title' },
          'Select an item'),
        React.createElement('div', { className: 'fs-quality-detail__placeholder-body' },
          'Pick any quality item in the list to see its full detail and source report.'),
      );
    }

    function onOpenInTimeline() {
      /* Sprint 6.6.4 — append &topic=N for topic-source rows so the
         timeline page lands in focus mode. qc_item rows are
         report-level and skip the topic param. */
      var qs = '?date=' + encodeURIComponent(sel.date);
      if (sel.user_folder) qs += '&user=' + encodeURIComponent(sel.user_folder);
      if (sel.topic_id != null && sel.topic_id >= 0) {
        qs += '&topic=' + encodeURIComponent(sel.topic_id);
      }
      window.FS.Router.navigate('/timeline' + qs);
    }

    var statusBadge = React.createElement(Badge, {
      tone: statusTone(sel.status), size: 'sm', prefixDot: true,
    }, (sel.status || '').charAt(0).toUpperCase() + (sel.status || '').slice(1) || 'Unknown');

    var followUpBadge = sel.follow_up_needed
      ? React.createElement(Badge, {
          tone: 'warning', size: 'sm', variant: 'outline',
        }, 'Follow-up needed')
      : null;

    var sourceLabel = sel.source === 'qc_item'
      ? 'Report-level Q&C item'
      : 'Quality-tagged topic';

    var rows = [];
    if (sel.details) {
      rows.push(React.createElement(DetailRow, {
        key: 'details', label: 'Details', value: sel.details,
      }));
    }
    rows.push(React.createElement(DetailRow, {
      key: 'date', label: 'Date', value: fmtDateLong(sel.date),
    }));
    if (sel.topic_id >= 0) {
      rows.push(React.createElement(DetailRow, {
        key: 'topic', label: 'From topic', value: sel.topic_title,
      }));
    }
    if (sel.user_name) {
      rows.push(React.createElement(DetailRow, {
        key: 'reporter', label: 'Reporter', value: sel.user_name,
      }));
    }
    if (sel.who_raised && sel.who_raised !== sel.user_name) {
      rows.push(React.createElement(DetailRow, {
        key: 'who', label: 'Raised by', value: sel.who_raised,
      }));
    }
    if (sel.site) {
      rows.push(React.createElement(DetailRow, {
        key: 'site', label: 'Site', value: sel.site,
      }));
    }
    rows.push(React.createElement(DetailRow, {
      key: 'source', label: 'Source', value: sourceLabel,
    }));

    /* Sprint 6.6.3 — photos block. Topic-quality rows carry
       related_photos from the aggregator; report-level qc_items don't
       (no topic to lift them from). */
    var photosBlock = null;
    var PhotoGrid   = fs.PhotoGrid;
    var photos      = (sel.related_photos || []);
    if (photos.length > 0 && PhotoGrid) {
      photosBlock = React.createElement('div', { className: 'fs-quality-detail__photos' },
        React.createElement('div', { className: 'fs-quality-detail__photos-label' },
          'Photos · ' + photos.length),
        React.createElement(PhotoGrid, {
          photos:           photos,
          userDisplayName:  sel.user_name,
          date:             sel.date,
          variant:          'carousel',
        }),
      );
    }

    var linkedBlock = null;
    if (sel.topic_id >= 0) {
      if (linksS.status === 'loading') {
        linkedBlock = React.createElement('div', { className: 'fs-quality-detail__linked' },
          React.createElement('div', { className: 'fs-quality-detail__linked-label' },
            'Related actions'),
          React.createElement('div', { className: 'fs-quality-detail__linked-loading' },
            'Loading…'),
        );
      } else if (linksS.items.length > 0) {
        linkedBlock = React.createElement('div', { className: 'fs-quality-detail__linked' },
          React.createElement('div', { className: 'fs-quality-detail__linked-label' },
            'Related actions in this topic'),
          React.createElement('div', { className: 'fs-quality-detail__linked-items' },
            linksS.items.map(function (it) {
              return React.createElement('div', {
                key:       it.action_index,
                className: 'fs-quality-detail__linked-chip',
              },
                React.createElement('div', { className: 'fs-quality-detail__linked-text' },
                  it.text),
                it.responsible
                  ? React.createElement('div', { className: 'fs-quality-detail__linked-meta' },
                      it.responsible + (it.priority ? ' · ' + it.priority : ''))
                  : null,
              );
            }),
          ),
        );
      }
    }

    return React.createElement('div', { className: 'fs-quality-detail' },

      React.createElement('div', { className: 'fs-quality-detail__header' },
        React.createElement('div', { className: 'fs-quality-detail__header-main' },
          React.createElement('h2', { className: 'fs-quality-detail__title' },
            sel.item),
          React.createElement('div', { className: 'fs-quality-detail__metaline' },
            statusBadge, followUpBadge,
          ),
        ),
        IconBtn ? React.createElement(IconBtn, {
          icon: 'x', ariaLabel: 'Close detail', size: 'sm',
          onClick: function () {
            if (ctx && ctx.setSelected) ctx.setSelected(null);
            if (props.onClose) props.onClose();
          },
        }) : null,
      ),

      React.createElement('div', { className: 'fs-quality-detail__rows' }, rows),

      /* Photos (Sprint 6.6.3) */
      photosBlock,

      linkedBlock,

      React.createElement('div', { className: 'fs-quality-detail__actions' },
        React.createElement(Button, {
          variant: 'secondary', size: 'sm', rightIcon: 'arrow-right',
          onClick: onOpenInTimeline,
        }, 'Open source report'),
      ),
    );
  }

  /* ---------- Register --------------------------------------------------- */

  if (!window.FieldSight) window.FieldSight = {};
  if (!window.FieldSight.PAGES) window.FieldSight.PAGES = {};
  window.FieldSight.PAGES['/quality'] = {
    Middle:   QualityMiddleColumn,
    Right:    QualityRightDetail,
    Provider: QualityProvider,
  };

})();
