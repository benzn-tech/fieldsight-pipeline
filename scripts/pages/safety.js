/* ==========================================================================
   FieldSight Safety Page — Sprint 6.1 (middle column) / 6.2 (right detail)
   --------------------------------------------------------------------------
   /safety — cross-day rollup of safety_observations + topic-level
   safety_flags. Reads via the Sprint 6.0 compliance aggregator.

   Middle column:
     • Header — title + context line (range + row count)
     • Range toolbar — Today | Last 7 days | Pick date (single-day mode
       opens DatePicker)
     • KpiStrip — total flags · high-risk · sites affected · open vs
       closed
     • List — rows grouped by date desc, each item is a SafetyFlagRow.
       Click → set selectedFlag in SafetyContext.

   Right detail:
     • Sprint 6.1 ships a placeholder ('Select a flag…' message).
     • Sprint 6.2 replaces it with full-context inspection (status
       badge, observation, action, location, source-report link).

   Architecture mirrors /tasks (Sprint 4.2):
     SafetyProvider holds { status, mode, date, fromTo, rows, totals,
                            selectedFlag } via SafetyContext.

   Registers as window.FieldSight.PAGES['/safety']
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
    var days   = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
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

  function totalsFromRows(rows) {
    var sites = {};
    var high = 0, openCt = 0, closedCt = 0;
    rows.forEach(function (r) {
      if (r.site) sites[r.site] = true;
      if (r.risk_level === 'high') high += 1;
      if (r.status === 'resolved') closedCt += 1;
      else openCt += 1;
    });
    return {
      total: rows.length,
      high:  high,
      sites: Object.keys(sites).length,
      open:  openCt,
      closed: closedCt,
    };
  }

  /* ---------- SafetyContext --------------------------------------------- */

  var SafetyContext = React.createContext(null);

  function SafetyProvider(props) {
    var caller = (window.AuthMock && window.AuthMock.currentUser) || {};
    var depKey = (caller.name || '') + '|' + (caller.role || '') + '|' + (caller.isAdmin ? 'admin' : '');

    /* mode: 'week' (last 7 days incl. today) or 'day' (single date). */
    var refMode = React.useState('week');
    var mode    = refMode[0];
    var setMode = refMode[1];

    /* When in 'day' mode, this is the picked date. Initialised to
       today; user clicks the picker chip to enter day mode. */
    var refDay = React.useState(window.FS.api.todayNZDT());
    var day    = refDay[0];
    var setDay = refDay[1];

    var refState = React.useState({ status: 'loading' });
    var state    = refState[0];
    var setState = refState[1];

    var retryRef   = React.useState(0);
    var retryCount = retryRef[0];
    var setRetry   = retryRef[1];

    /* Compute the from/to range for the current mode + day. */
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

      window.FS.api.compliance.getSafetyRange({
        from: range.from, to: range.to,
      }).then(function (res) {
        if (cancelled) return;
        if (res && res._accessDenied) {
          setState({ status: 'access_denied', message: res.error });
          return;
        }
        var rows = (res && res.rows) || [];
        setState({
          status:  'ok',
          rows:    rows,
          from:    range.from,
          to:      range.to,
          totals:  totalsFromRows(rows),
          groups:  groupByDate(rows),
          dates:   (res && res.dates) || [],
          user:    res.user || null,
        });
      }).catch(function (err) {
        if (cancelled) return;
        setState({ status: 'error', error: { code: (err && err.status) || 0, message: (err && err.message) || 'Could not load safety data', retryable: true }, retry: function () { setRetry(function (n) { return n + 1; }); } });
      });

      return function () { cancelled = true; };
    }, [depKey, mode, day, retryCount]);

    var refSel = React.useState(null);
    var sel    = refSel[0];
    var setSel = refSel[1];

    var refCreate = React.useState(false);
    var showCreate = refCreate[0];
    var setShowCreate = refCreate[1];

    var ctx = {
      state:         state,
      setState:      setState,
      mode:          mode,
      day:           day,
      setMode:       function (m) { setSel(null); setMode(m); },
      setDay:        function (d) { setSel(null); setDay(d); setMode('day'); },
      selectedFlag:  sel,
      setSelected:   setSel,
      showCreate:    showCreate,
      setShowCreate: setShowCreate,
      caller:        caller,
    };
    return React.createElement(SafetyContext.Provider, { value: ctx },
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
        className: 'fs-safety__chip' + (isActive ? ' fs-safety__chip--active' : ''),
        onClick:   function () {
          if (key === 'pick') { setOpen(!open); return; }
          ctx.setMode(key);
          setOpen(false);
        },
      }, label);
    }

    return React.createElement('div', { className: 'fs-safety__toolbar' },
      React.createElement('div', { className: 'fs-safety__chips' },
        chip('today',  'Today',         ctx.mode === 'today'),
        chip('week',   'Last 7 days',   ctx.mode === 'week'),
        chip('pick',   ctx.mode === 'day' ? fmtDate(ctx.day) : 'Pick date…',
             ctx.mode === 'day'),
      ),
      open && DatePicker
        ? React.createElement('div', { className: 'fs-safety__picker-wrap' },
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

  function SafetyMiddleColumn(props) {
    var fs                 = window.FieldSight;
    var KpiStrip           = fs.KpiStrip;
    var StatCard           = fs.StatCard;
    var SafetyFlagRow      = fs.SafetyFlagRow;
    var SafetyCreateModal  = fs.SafetyCreateModal;
    var Badge              = fs.Badge;
    var AccessDenied       = fs.AccessDenied;
    var Button             = fs.Button;

    var ctx = React.useContext(SafetyContext);
    if (!ctx) {
      console.warn('[SafetyMiddleColumn] SafetyContext missing');
      return null;
    }
    var state    = ctx.state;
    var onSelect = props.onSelect || function () {};

    /* Gate: only hse_manager or site_manager (or admin) can raise new observations. */
    var caller  = ctx.caller || {};
    var canCreate = !!(window.FS && window.FS.can &&
      (window.FS.can(caller, 'safety:manage') ||
       window.FS.can(caller, 'site:manage') ||
       (caller.isAdmin)));

    /* Header is always visible — toolbar should be reachable even
       during loading/empty states. */
    var raiseBtn = (canCreate && SafetyCreateModal)
      ? React.createElement('button', {
          type:      'button',
          className: 'fs-safety__raise-btn',
          onClick:   function () { ctx.setShowCreate(true); },
        }, '+ Raise Observation')
      : null;

    var header = React.createElement('div', { className: 'fs-safety__header' },
      React.createElement('div', { className: 'fs-safety__header-top' },
        React.createElement('div', null,
          React.createElement('h2', { className: 'fs-safety__title' }, 'Safety'),
          React.createElement('div', { className: 'fs-safety__subtitle' },
            'Flags and observations across your accessible reports'),
        ),
        raiseBtn,
      ),
    );
    var toolbar = React.createElement(RangeToolbar, { ctx: ctx });

    if (state.status === 'loading') {
      return React.createElement('div', { className: 'fs-safety' },
        header, toolbar,
        React.createElement('div', { className: 'fs-safety__loading' },
          'Loading safety data…'),
      );
    }
    if (state.status === 'error') {
      var ErrorBanner = window.FieldSight.ErrorBanner;
      return React.createElement('div', { className: 'fs-safety' },
        header, toolbar,
        ErrorBanner
          ? React.createElement(ErrorBanner, {
              message:   (state.error && state.error.message) || 'Could not load safety data',
              retryable: true,
              onRetry:   state.retry,
            })
          : React.createElement('div', { className: 'fs-safety__empty' },
              (state.error && state.error.message) || 'Could not load safety data'),
      );
    }
    if (state.status === 'access_denied') {
      return React.createElement('div', { className: 'fs-safety' },
        header,
        AccessDenied
          ? React.createElement(AccessDenied, {
              scope:   'safety data',
              message: state.message,
            })
          : React.createElement('div', null, 'Access denied.'),
      );
    }

    var totals = state.totals || { total: 0, high: 0, sites: 0, open: 0, closed: 0 };
    var groups = state.groups || [];
    var rangeLabel = state.from === state.to
      ? fmtDate(state.from)
      : fmtDate(state.from) + ' → ' + fmtDate(state.to);

    /* Callback when a new observation is successfully created: prepend
       to the provider's list so it appears immediately without a reload. */
    function handleNewFlag(newFlag) {
      ctx.setShowCreate(false);
      if (ctx.setState && newFlag) {
        ctx.setState(function (s) {
          if (s.status !== 'ok') return s;
          var updatedRows = [newFlag].concat(s.rows || []);
          return Object.assign({}, s, {
            rows:   updatedRows,
            totals: totalsFromRows(updatedRows),
            groups: groupByDate(updatedRows),
          });
        });
      }
    }

    return React.createElement('div', { className: 'fs-safety' },
      header,
      toolbar,

      /* Create observation modal (Sprint 8.1.2) */
      ctx.showCreate && SafetyCreateModal
        ? React.createElement(SafetyCreateModal, {
            siteId:    state.user || '',
            onSuccess: handleNewFlag,
            onCancel:  function () { ctx.setShowCreate(false); },
          })
        : null,

      /* Meta line */
      React.createElement('div', { className: 'fs-safety__meta' },
        totals.total + (totals.total === 1 ? ' flag · ' : ' flags · ') + rangeLabel),

      /* KPI strip */
      React.createElement(KpiStrip, null,
        React.createElement(StatCard, { value: totals.total, label: 'Total flags' }),
        React.createElement(StatCard, {
          value: totals.high, label: 'High risk',
          tone:  totals.high > 0 ? 'danger' : 'neutral',
        }),
        React.createElement(StatCard, { value: totals.sites, label: 'Sites affected' }),
        React.createElement(StatCard, {
          /* Sprint 6.6.2 — visual order: closed first (resolved/safe
             reads as the desirable end-state). Tone still keyed on
             `open` since "open issues" is the alarm signal. */
          value: totals.closed + ' / ' + totals.open,
          label: 'Closed / open',
          tone:  totals.open > 0 ? 'warning' : 'success',
        }),
      ),

      /* Grouped list */
      groups.length === 0
        ? React.createElement('div', { className: 'fs-safety__empty' },
            'No safety flags in this window.')
        : React.createElement('div', { className: 'fs-safety__groups' },
            groups.map(function (g) {
              return React.createElement('div', { key: g.date, className: 'fs-safety__group' },
                React.createElement('div', { className: 'fs-safety__group-header' },
                  React.createElement('span', { className: 'fs-safety__group-date' },
                    fmtDateLong(g.date)),
                  React.createElement('span', { className: 'fs-safety__group-count' },
                    g.rows.length + (g.rows.length === 1 ? ' flag' : ' flags')),
                ),
                React.createElement('div', { className: 'fs-safety__group-rows' },
                  g.rows.map(function (row) {
                    var isSel = ctx.selectedFlag && ctx.selectedFlag.id === row.id;
                    return React.createElement('button', {
                      key:       row.id,
                      type:      'button',
                      className: 'fs-safety__row-btn'
                        + (isSel ? ' fs-safety__row-btn--active' : ''),
                      onClick:   function () {
                        ctx.setSelected(row);
                        onSelect({ kind: 'safety_flag', id: row.id, row: row });
                      },
                    },
                      React.createElement(SafetyFlagRow, {
                        flag: {
                          observation:        row.observation,
                          risk_level:         row.risk_level,
                          recommended_action: row.recommended_action,
                          location:           row.location,
                          who_raised:         row.who_raised,
                        },
                        dense: true,
                      }),
                      React.createElement('div', { className: 'fs-safety__row-meta' },
                        row.topic_title !== 'Site safety observations'
                          ? React.createElement('span', { className: 'fs-safety__row-topic' },
                              'From: ' + row.topic_title)
                          : null,
                      ),
                    );
                  }),
                ),
              );
            }),
          ),
    );
  }

  /* ---------- Right detail (Sprint 6.2 — full inspection panel) -------- */

  var RISK_TONE   = { high: 'danger', medium: 'warning', low: 'neutral' };
  var STATUS_TONE = { open: 'warning', resolved: 'success' };

  function DetailRow(props) {
    return React.createElement('div', { className: 'fs-safety-detail__row' },
      React.createElement('div', { className: 'fs-safety-detail__row-label' },
        props.label),
      React.createElement('div', { className: 'fs-safety-detail__row-value' },
        props.value),
    );
  }

  function SafetyRightDetail(props) {
    var fs       = window.FieldSight;
    var Card     = fs.Card;
    var Badge    = fs.Badge;
    var Button   = fs.Button;
    var IconBtn  = fs.IconButton;

    var ctx = React.useContext(SafetyContext);
    var sel = ctx && ctx.selectedFlag;

    /* Lazy-fetch related action_items from the source topic. Mirrors
       the linked-actions lazy-fetch from programme.js:805-881. The
       source topic carries N action_items — we surface them as
       click-through chips so the field user can jump from a flag to
       any related corrective action in one tap. */
    var refLinks = React.useState({ status: 'idle', items: [] });
    var linksS   = refLinks[0];
    var setLinks = refLinks[1];

    React.useEffect(function () {
      /* Skip lookup for report-level safety_observations — those don't
         carry a topic_id (we set it to -1 in the aggregator). */
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
      return React.createElement('div', { className: 'fs-safety-detail__placeholder' },
        React.createElement('div', { className: 'fs-safety-detail__placeholder-title' },
          'Select a flag'),
        React.createElement('div', { className: 'fs-safety-detail__placeholder-body' },
          'Pick any flag in the list to see its full detail and source report.'),
      );
    }

    function onOpenInTimeline() {
      /* Sprint 6.6.4 — append &topic=N for topic-source rows so the
         timeline page lands in focus mode (target topic auto-opens
         and flashes; others force-collapse). Observation rows skip
         the topic param since they're report-level — the user lands
         on the daily report's overview without a focal point.

         Sprint 6.7.2 — for topic_flag source, also append &flag=<idx>
         so the precision spotlight lands on the specific flag inside
         the topic's safety_flags[] (not just the whole topic card).
         Flag idx is the trailing number in the row id, format
         '<date>_<topic_id>_flag_<idx>'. */
      var qs = '?date=' + encodeURIComponent(sel.date);
      if (sel.user_folder) qs += '&user=' + encodeURIComponent(sel.user_folder);
      if (sel.topic_id != null && sel.topic_id >= 0) {
        qs += '&topic=' + encodeURIComponent(sel.topic_id);
        if (sel.source === 'topic_flag') {
          var m = String(sel.id || '').match(/_flag_(\d+)$/);
          if (m) qs += '&flag=' + encodeURIComponent(m[1]);
        }
      }
      window.FS.Router.navigate('/timeline' + qs);
    }

    var risk = (sel.risk_level || 'medium').toLowerCase();
    var riskBadge = React.createElement(Badge, {
      tone: RISK_TONE[risk] || 'neutral', size: 'sm', prefixDot: true,
    }, risk.charAt(0).toUpperCase() + risk.slice(1) + ' risk');

    var statusBadge = React.createElement(Badge, {
      tone: STATUS_TONE[sel.status] || 'neutral', size: 'sm', variant: 'outline',
    }, (sel.status || 'open').charAt(0).toUpperCase() + (sel.status || 'open').slice(1));

    var sourceLabel = sel.source === 'observation'
      ? 'Site-level observation'
      : 'Topic safety flag';

    /* Build the field rows — skip rows whose value is null, since the
       two source shapes carry different fields. */
    var rows = [];
    if (sel.recommended_action) {
      rows.push(React.createElement(DetailRow, {
        key: 'action', label: 'Action', value: sel.recommended_action,
      }));
    }
    if (sel.location) {
      rows.push(React.createElement(DetailRow, {
        key: 'location', label: 'Location', value: sel.location,
      }));
    }
    if (sel.who_raised) {
      rows.push(React.createElement(DetailRow, {
        key: 'who', label: 'Raised by', value: sel.who_raised,
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
    if (sel.site) {
      rows.push(React.createElement(DetailRow, {
        key: 'site', label: 'Site', value: sel.site,
      }));
    }
    rows.push(React.createElement(DetailRow, {
      key: 'source', label: 'Source', value: sourceLabel,
    }));

    /* Sprint 6.6.3 — photos block, rendered between field rows and
       linked actions. Topic-flag rows carry related_photos from the
       aggregator; observation rows are report-level and don't have a
       specific topic to lift photos from. */
    var photosBlock = null;
    var PhotoGrid   = fs.PhotoGrid;
    var photos      = (sel.related_photos || []);
    if (photos.length > 0 && PhotoGrid) {
      photosBlock = React.createElement('div', { className: 'fs-safety-detail__photos' },
        React.createElement('div', { className: 'fs-safety-detail__photos-label' },
          'Photos · ' + photos.length),
        React.createElement(PhotoGrid, {
          photos:           photos,
          userDisplayName:  sel.user_name,
          date:             sel.date,
          variant:          'carousel',
        }),
      );
    }

    /* Linked-actions block — only shown for topic-flag rows (since
       observation rows don't have a topic to lift action_items from). */
    var linkedBlock = null;
    if (sel.topic_id >= 0) {
      if (linksS.status === 'loading') {
        linkedBlock = React.createElement('div', { className: 'fs-safety-detail__linked' },
          React.createElement('div', { className: 'fs-safety-detail__linked-label' },
            'Related actions'),
          React.createElement('div', { className: 'fs-safety-detail__linked-loading' },
            'Loading…'),
        );
      } else if (linksS.items.length > 0) {
        linkedBlock = React.createElement('div', { className: 'fs-safety-detail__linked' },
          React.createElement('div', { className: 'fs-safety-detail__linked-label' },
            'Related actions in this topic'),
          React.createElement('div', { className: 'fs-safety-detail__linked-items' },
            linksS.items.map(function (it) {
              return React.createElement('div', {
                key:       it.action_index,
                className: 'fs-safety-detail__linked-chip',
              },
                React.createElement('div', { className: 'fs-safety-detail__linked-text' },
                  it.text),
                it.responsible
                  ? React.createElement('div', { className: 'fs-safety-detail__linked-meta' },
                      it.responsible + (it.priority ? ' · ' + it.priority : ''))
                  : null,
              );
            }),
          ),
        );
      }
    }

    return React.createElement('div', { className: 'fs-safety-detail' },

      /* Header */
      React.createElement('div', { className: 'fs-safety-detail__header' },
        React.createElement('div', { className: 'fs-safety-detail__header-main' },
          React.createElement('h2', { className: 'fs-safety-detail__title' },
            sel.observation),
          React.createElement('div', { className: 'fs-safety-detail__metaline' },
            riskBadge, statusBadge,
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

      /* Field rows */
      React.createElement('div', { className: 'fs-safety-detail__rows' }, rows),

      /* Photos (Sprint 6.6.3) */
      photosBlock,

      /* Linked actions */
      linkedBlock,

      /* Footer actions */
      React.createElement('div', { className: 'fs-safety-detail__actions' },
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
  window.FieldSight.PAGES['/safety'] = {
    Middle:   SafetyMiddleColumn,
    Right:    SafetyRightDetail,
    Provider: SafetyProvider,
  };

})();
