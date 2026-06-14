/* ==========================================================================
   FieldSight — programme-import.js  (Sprint 5.4)
   --------------------------------------------------------------------------
   Native CSV parser for Programme task import. No external dependencies —
   plain ES2017, safe to load as a plain <script> (no Babel required).

   Exported to: window.FS.api.programmeImport

   Public API:
     parseCSV(text) → { parents, leaves, errors, warnings }

   Column contract (order-independent, case-sensitive header names):
     task_id, wbs, parent_id, name, start, end, progress_pct, status,
     depends_on, assignees

   - depends_on and assignees are pipe-separated lists ("T-001|T-002")
   - parent_id empty or absent → treated as a group (status forced to 'group')
   - Groups come before their leaf children in the returned arrays
   - Errors block import; warnings are informational
   ========================================================================== */

/* global window */

(function () {
  'use strict';

  var REQUIRED_COLS = ['task_id', 'wbs', 'name', 'start', 'end', 'status'];
  var OPTIONAL_COLS = ['parent_id', 'progress_pct', 'depends_on', 'assignees'];
  var ALL_COLS = REQUIRED_COLS.concat(OPTIONAL_COLS);

  var VALID_STATUSES = ['not_started', 'in_progress', 'completed', 'delayed', 'blocked', 'group'];
  var DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

  /* --- helpers ------------------------------------------------------------ */

  function stripBOM(text) {
    return text.charCodeAt(0) === 0xFEFF ? text.slice(1) : text;
  }

  function normaliseLine(line) {
    /* CRLF → LF already done by split; trim trailing CR just in case */
    return line.replace(/\r$/, '').trim();
  }

  /* Minimal RFC-4180-ish CSV tokeniser.
     Handles quoted fields with embedded commas + doubled quotes.
     Does not handle multi-line fields (construction programme CSVs don't). */
  function tokeniseLine(line) {
    var fields = [];
    var i = 0;
    var len = line.length;
    while (i <= len) {
      if (i === len) { fields.push(''); break; }
      if (line[i] === '"') {
        /* quoted field */
        var j = i + 1;
        var buf = '';
        while (j < len) {
          if (line[j] === '"') {
            if (line[j + 1] === '"') { buf += '"'; j += 2; }
            else { j++; break; }
          } else {
            buf += line[j++];
          }
        }
        fields.push(buf);
        /* skip comma or end */
        if (line[j] === ',') j++;
        i = j;
      } else {
        /* unquoted field */
        var k = line.indexOf(',', i);
        if (k === -1) { fields.push(line.slice(i)); break; }
        fields.push(line.slice(i, k));
        i = k + 1;
      }
    }
    return fields;
  }

  function isValidDate(s) {
    if (!DATE_RE.test(s)) return false;
    var d = new Date(s + 'T00:00:00Z');
    return !isNaN(d.getTime());
  }

  function diffDays(a, b) {
    var da = new Date(a + 'T00:00:00Z');
    var db = new Date(b + 'T00:00:00Z');
    return Math.round((db - da) / 86400000);
  }

  function splitPipe(val) {
    if (!val) return [];
    return val.split('|').map(function (s) { return s.trim(); }).filter(Boolean);
  }

  /* --- main parser -------------------------------------------------------- */

  function parseCSV(text) {
    var errors   = [];
    var warnings = [];

    text = stripBOM(text || '');
    var rawLines = text.split('\n');
    var lines = rawLines.map(normaliseLine).filter(function (l) { return l.length > 0; });

    if (lines.length === 0) {
      errors.push({ row: 0, field: null, message: 'File is empty.' });
      return { parents: [], leaves: [], errors: errors, warnings: warnings };
    }

    /* --- header row ------------------------------------------------------- */
    var headerFields = tokeniseLine(lines[0]);
    var colIndex = {};
    headerFields.forEach(function (h, i) { colIndex[h.trim()] = i; });

    var missingCols = REQUIRED_COLS.filter(function (c) { return !(c in colIndex); });
    if (missingCols.length > 0) {
      errors.push({
        row: 1, field: null,
        message: 'Missing required columns: ' + missingCols.join(', ') + '.',
      });
      return { parents: [], leaves: [], errors: errors, warnings: warnings };
    }

    /* Warn about unrecognised columns (they are silently ignored). */
    headerFields.forEach(function (h) {
      var name = h.trim();
      if (name && ALL_COLS.indexOf(name) === -1) {
        warnings.push({ row: 1, field: name, message: 'Unknown column "' + name + '" will be ignored.' });
      }
    });

    function get(fields, col) {
      var idx = colIndex[col];
      return (idx !== undefined && idx < fields.length) ? (fields[idx] || '').trim() : '';
    }

    /* --- data rows -------------------------------------------------------- */
    var seenIds = {};
    var rawRows = [];

    for (var r = 1; r < lines.length; r++) {
      var rowNum = r + 1; /* 1-based, header is row 1 */
      var fields = tokeniseLine(lines[r]);

      var taskId    = get(fields, 'task_id');
      var wbs       = get(fields, 'wbs');
      var parentId  = get(fields, 'parent_id');
      var name      = get(fields, 'name');
      var start     = get(fields, 'start');
      var end       = get(fields, 'end');
      var progRaw   = get(fields, 'progress_pct');
      var status    = get(fields, 'status');
      var depsRaw   = get(fields, 'depends_on');
      var assignRaw = get(fields, 'assignees');

      /* Required fields */
      if (!taskId) { errors.push({ row: rowNum, field: 'task_id', message: 'Row ' + rowNum + ': task_id is required.' }); continue; }
      if (!name)   { errors.push({ row: rowNum, field: 'name',    message: 'Row ' + rowNum + ' (' + taskId + '): name is required.' }); continue; }
      if (!status) { errors.push({ row: rowNum, field: 'status',  message: 'Row ' + rowNum + ' (' + taskId + '): status is required.' }); continue; }

      /* Duplicate ID check */
      if (seenIds[taskId]) {
        errors.push({ row: rowNum, field: 'task_id', message: 'Row ' + rowNum + ': duplicate task_id "' + taskId + '".' });
        continue;
      }
      seenIds[taskId] = rowNum;

      /* Status validation */
      if (VALID_STATUSES.indexOf(status) === -1) {
        errors.push({
          row: rowNum, field: 'status',
          message: 'Row ' + rowNum + ' (' + taskId + '): invalid status "' + status + '". Expected: ' + VALID_STATUSES.join(', ') + '.',
        });
        continue;
      }

      /* Groups: parent_id must be empty; leaves: parent_id required */
      var isGroup = (status === 'group' || !parentId);
      if (isGroup && parentId) {
        warnings.push({ row: rowNum, field: 'parent_id', message: 'Row ' + rowNum + ' (' + taskId + '): group rows should have an empty parent_id (ignored).' });
        parentId = '';
      }

      /* Date validation (only for leaf tasks) */
      if (!isGroup) {
        if (!wbs) {
          errors.push({ row: rowNum, field: 'wbs', message: 'Row ' + rowNum + ' (' + taskId + '): wbs is required for leaf tasks.' });
          continue;
        }
        if (!start || !isValidDate(start)) {
          errors.push({ row: rowNum, field: 'start', message: 'Row ' + rowNum + ' (' + taskId + '): invalid start date "' + start + '". Expected YYYY-MM-DD.' });
          continue;
        }
        if (!end || !isValidDate(end)) {
          errors.push({ row: rowNum, field: 'end', message: 'Row ' + rowNum + ' (' + taskId + '): invalid end date "' + end + '". Expected YYYY-MM-DD.' });
          continue;
        }
        if (end < start) {
          errors.push({ row: rowNum, field: 'end', message: 'Row ' + rowNum + ' (' + taskId + '): end date must be ≥ start date.' });
          continue;
        }
      }

      /* Progress */
      var progressPct = progRaw === '' ? 0 : Number(progRaw);
      if (isNaN(progressPct) || progressPct < 0 || progressPct > 100) {
        errors.push({ row: rowNum, field: 'progress_pct', message: 'Row ' + rowNum + ' (' + taskId + '): progress_pct must be a number 0–100, got "' + progRaw + '".' });
        continue;
      }
      progressPct = Math.round(progressPct);

      var dependsOn = splitPipe(depsRaw);
      var assignees = splitPipe(assignRaw);

      rawRows.push({
        task_id:      taskId,
        wbs:          wbs,
        parent_id:    parentId,
        name:         name,
        start:        start,
        end:          end,
        duration_days: (start && end) ? diffDays(start, end) + 1 : null,
        progress_pct: progressPct,
        status:       isGroup ? 'group' : status,
        depends_on:   dependsOn,
        assignees:    assignees,
        /* Fields the fixture carries; default to empty for imports */
        resource_pool:       [],
        linked_action_items: [],
        tags:                [],
        baseline_start: start,
        baseline_end:   end,
        _isGroup:       isGroup,
      });
    }

    /* --- cross-row validation --------------------------------------------- */

    /* depends_on referencing IDs not present in the file */
    rawRows.forEach(function (row) {
      row.depends_on.forEach(function (depId) {
        if (!seenIds[depId]) {
          warnings.push({
            row: seenIds[row.task_id],
            field: 'depends_on',
            message: row.task_id + ': depends_on references unknown task_id "' + depId + '" (will be kept as-is).',
          });
        }
      });
    });

    /* leaf tasks referencing unknown parent_id */
    rawRows.forEach(function (row) {
      if (!row._isGroup && row.parent_id && !seenIds[row.parent_id]) {
        warnings.push({
          row: seenIds[row.task_id],
          field: 'parent_id',
          message: row.task_id + ': parent_id "' + row.parent_id + '" not found in file (task will still be imported).',
        });
      }
    });

    /* --- split + strip internal flag ------------------------------------- */
    var parents = rawRows.filter(function (r) { return r._isGroup; })
                          .map(function (r) { var o = Object.assign({}, r); delete o._isGroup; return o; });
    var leaves  = rawRows.filter(function (r) { return !r._isGroup; })
                          .map(function (r) { var o = Object.assign({}, r); delete o._isGroup; return o; });

    return { parents: parents, leaves: leaves, errors: errors, warnings: warnings };
  }

  /* =========================================================================
     Sprint 5.5 — MS Project XML import
     =========================================================================
     DOMParser walk of <Project>/<Tasks>/<Task>.
     Field mapping:
       <UID>            → task_id  (prefixed "T-NNN")
       <Name>           → name
       <WBS>            → wbs
       <OutlineLevel>   → 1 = group, >1 = leaf
       <Summary>        → "1" overrides OutlineLevel to force group
       <Start>/<Finish> → start / end  (YYYY-MM-DD prefix only)
       <PercentComplete>→ progress_pct; status derived from it
       <PredecessorLink>→ depends_on  (FS/Type=1 only, no lag)

     Ignored (warned once): calendars, resource assignments, non-FS links,
     lag values.
     ======================================================================== */

  /* --- XML DOM helpers (namespace-agnostic) -------------------------------- */

  function _getTags(node, localName) {
    if (node.getElementsByTagNameNS) {
      return Array.prototype.slice.call(node.getElementsByTagNameNS('*', localName));
    }
    return Array.prototype.slice.call(node.getElementsByTagName(localName));
  }

  function _getTag(node, localName) {
    var els = _getTags(node, localName);
    return els.length > 0 ? els[0] : null;
  }

  function _getText(node, localName) {
    var el = _getTag(node, localName);
    return el ? el.textContent.trim() : '';
  }

  /* Resolve a WBS string's immediate group ancestor in rawNodes[]. */
  function _groupAncestor(wbs, rawNodes, wbsToTaskId) {
    var parts = wbs.split('.');
    for (var i = parts.length - 1; i >= 1; i--) {
      var ancestorWBS = parts.slice(0, i).join('.');
      var ancestorId  = wbsToTaskId[ancestorWBS];
      if (ancestorId) {
        for (var j = 0; j < rawNodes.length; j++) {
          if (rawNodes[j].task_id === ancestorId && rawNodes[j]._isGroup) {
            return ancestorId;
          }
        }
      }
    }
    return '';
  }

  /* --- main XML parser ----------------------------------------------------- */

  function parseMSProjectXML(text) {
    var errors   = [];
    var warnings = [];

    if (typeof DOMParser === 'undefined') {
      errors.push({ row: 0, field: null, message: 'DOMParser is not available in this environment.' });
      return { parents: [], leaves: [], errors: errors, warnings: warnings };
    }

    var doc;
    try {
      doc = (new DOMParser()).parseFromString(text || '', 'text/xml');
    } catch (e) {
      errors.push({ row: 0, field: null, message: 'Failed to parse XML: ' + e.message });
      return { parents: [], leaves: [], errors: errors, warnings: warnings };
    }

    /* DOMParser signals parse failures via <parsererror> in the document. */
    var parseErrEl = _getTag(doc, 'parsererror');
    if (parseErrEl) {
      errors.push({
        row: 0, field: null,
        message: 'XML parse error: ' + parseErrEl.textContent.replace(/\s+/g, ' ').slice(0, 200),
      });
      return { parents: [], leaves: [], errors: errors, warnings: warnings };
    }

    var tasksEl = _getTag(doc, 'Tasks');
    if (!tasksEl) {
      errors.push({
        row: 0, field: null,
        message: 'No <Tasks> element found. Confirm this is an MS Project XML export ' +
                 '(File → Save As → XML Format in MS Project).',
      });
      return { parents: [], leaves: [], errors: errors, warnings: warnings };
    }

    var taskNodes = _getTags(tasksEl, 'Task');
    if (taskNodes.length === 0) {
      errors.push({ row: 0, field: null, message: 'No <Task> elements found inside <Tasks>.' });
      return { parents: [], leaves: [], errors: errors, warnings: warnings };
    }

    /* One-shot ignored-feature warnings. */
    var calendarsEl = _getTag(doc, 'Calendars');
    if (calendarsEl && _getTags(calendarsEl, 'Calendar').length > 0) {
      warnings.push({ row: 0, field: null, message: 'Calendar definitions ignored — tasks imported as calendar-independent.' });
    }
    var resourcesEl = _getTag(doc, 'Resources');
    if (resourcesEl && _getTags(resourcesEl, 'Resource').length > 0) {
      warnings.push({ row: 0, field: null, message: 'Resource assignments ignored — assign team members manually after import.' });
    }

    /* --- First pass: parse each <Task> -------------------------------------- */

    var seenUIDs    = {};   /* uid string → 1-based position */
    var wbsToTaskId = {};   /* wbs string → task_id */
    var rawNodes    = [];
    var warnedNonFS = false;
    var warnedLag   = false;

    taskNodes.forEach(function (node, idx) {
      var uid          = _getText(node, 'UID');
      var name         = _getText(node, 'Name');
      var wbs          = _getText(node, 'WBS') || _getText(node, 'OutlineNumber');
      var outlineLevel = parseInt(_getText(node, 'OutlineLevel') || '0', 10);
      var isSummaryTag = _getText(node, 'Summary') === '1';
      var startRaw     = _getText(node, 'Start');
      var finishRaw    = _getText(node, 'Finish');
      var pctRaw       = _getText(node, 'PercentComplete') || '0';

      var rowNum = idx + 1;

      /* Skip project-root task (UID=0 / OutlineLevel=0). */
      if (!uid || uid === '0' || outlineLevel === 0) return;

      if (!name) {
        warnings.push({ row: rowNum, field: 'Name', message: 'UID ' + uid + ' has no <Name> — skipped.' });
        return;
      }

      var taskId = 'T-' + (Array(4).join('0') + uid).slice(-3);

      if (seenUIDs[uid] !== undefined) {
        errors.push({ row: rowNum, field: 'UID', message: 'Duplicate <UID> ' + uid + ' (task "' + name + '") — skipped.' });
        return;
      }
      seenUIDs[uid] = rowNum;
      if (wbs) wbsToTaskId[wbs] = taskId;

      var isGroup = isSummaryTag || outlineLevel === 1;

      /* Predecessor links — FS (Type=1) only, no lag. */
      var predLinkNodes  = _getTags(node, 'PredecessorLink');
      var dependsOnUIDs  = [];
      predLinkNodes.forEach(function (link) {
        var predUID = _getText(link, 'PredecessorUID');
        var type    = _getText(link, 'Type');
        var lag     = _getText(link, 'LinkLag');

        if (type !== '' && type !== '1') {
          if (!warnedNonFS) {
            warnings.push({ row: 0, field: 'PredecessorLink', message: 'Non-FS predecessor relationships (FF/SS/SF) ignored — only Finish-to-Start links imported.' });
            warnedNonFS = true;
          }
          return;
        }
        if (lag && lag !== '0') {
          if (!warnedLag) {
            warnings.push({ row: 0, field: 'PredecessorLink', message: 'Predecessor lags ignored — relationships imported without lag.' });
            warnedLag = true;
          }
        }
        if (predUID && predUID !== '0') dependsOnUIDs.push(predUID);
      });

      /* Extract YYYY-MM-DD prefix from ISO datetime (2026-05-01T08:00:00). */
      var startMatch  = startRaw  ? startRaw.match(/^(\d{4}-\d{2}-\d{2})/)  : null;
      var finishMatch = finishRaw ? finishRaw.match(/^(\d{4}-\d{2}-\d{2})/) : null;
      var start = startMatch  ? startMatch[1]  : '';
      var end   = finishMatch ? finishMatch[1] : '';

      /* Date validation for leaf tasks only. */
      if (!isGroup) {
        if (!start || !isValidDate(start)) {
          errors.push({ row: rowNum, field: 'Start', message: 'UID ' + uid + ' (' + name + '): invalid or missing <Start> "' + startRaw + '".' });
          return;
        }
        if (!end || !isValidDate(end)) {
          errors.push({ row: rowNum, field: 'Finish', message: 'UID ' + uid + ' (' + name + '): invalid or missing <Finish> "' + finishRaw + '".' });
          return;
        }
        if (end < start) {
          errors.push({ row: rowNum, field: 'Finish', message: 'UID ' + uid + ' (' + name + '): <Finish> must be ≥ <Start>.' });
          return;
        }
      }

      /* Derive status from percent complete. */
      var pct = parseInt(pctRaw, 10);
      if (isNaN(pct)) pct = 0;
      pct = Math.min(100, Math.max(0, pct));
      var status = isGroup    ? 'group'
                 : pct >= 100 ? 'completed'
                 : pct > 0    ? 'in_progress'
                 :              'not_started';

      rawNodes.push({
        _uid:           uid,
        _outlineLevel:  outlineLevel,
        _isGroup:       isGroup,
        _wbs:           wbs,
        _dependsOnUIDs: dependsOnUIDs,
        task_id:        taskId,
        wbs:            wbs,
        name:           name,
        start:          start,
        end:            end,
        duration_days:  (start && end) ? diffDays(start, end) + 1 : null,
        progress_pct:   pct,
        status:         status,
        depends_on:     [],   /* resolved in pass 2 */
        parent_id:      '',   /* resolved in pass 2 */
        assignees:      [],
        resource_pool:  [],
        linked_action_items: [],
        tags:           [],
        baseline_start: start,
        baseline_end:   end,
      });
    });

    if (errors.length > 0) {
      return { parents: [], leaves: [], errors: errors, warnings: warnings };
    }

    /* --- Second pass: resolve UIDs → task_ids and parent_ids via WBS ------- */

    var uidMap = {};
    rawNodes.forEach(function (r) { uidMap[r._uid] = r.task_id; });

    rawNodes.forEach(function (r) {
      /* Resolve predecessor UIDs. */
      r.depends_on = r._dependsOnUIDs.map(function (uid) { return uidMap[uid]; }).filter(Boolean);
      r._dependsOnUIDs.forEach(function (uid) {
        if (!uidMap[uid]) {
          warnings.push({ row: 0, field: 'PredecessorLink', message: r.task_id + ': predecessor UID "' + uid + '" not found in file (relationship ignored).' });
        }
      });

      /* Resolve parent_id from WBS for leaf tasks. */
      if (!r._isGroup && r._wbs) {
        r.parent_id = _groupAncestor(r._wbs, rawNodes, wbsToTaskId);
        if (!r.parent_id) {
          warnings.push({ row: 0, field: 'WBS', message: r.task_id + ' (' + r.name + '): no group ancestor found for WBS "' + r._wbs + '" — task imported without a parent.' });
        }
      }
    });

    /* --- Split and strip internal fields ------------------------------------ */

    function cleanXMLRow(r) {
      return {
        task_id:        r.task_id,
        wbs:            r.wbs,
        parent_id:      r.parent_id,
        name:           r.name,
        start:          r.start,
        end:            r.end,
        duration_days:  r.duration_days,
        progress_pct:   r.progress_pct,
        status:         r.status,
        depends_on:     r.depends_on,
        assignees:      r.assignees,
        resource_pool:  r.resource_pool,
        linked_action_items: r.linked_action_items,
        tags:           r.tags,
        baseline_start: r.baseline_start,
        baseline_end:   r.baseline_end,
      };
    }

    var parents = rawNodes.filter(function (r) { return  r._isGroup; }).map(cleanXMLRow);
    var leaves  = rawNodes.filter(function (r) { return !r._isGroup; }).map(cleanXMLRow);

    return { parents: parents, leaves: leaves, errors: errors, warnings: warnings };
  }

  /* =========================================================================
     Sprint 8.2.2 — XLSX import via SheetJS CE (CDN UMD, MIT licence)
     parseXLSX is async (FileReader returns a Promise).

     Column contract: same as CSV (task_id, wbs, name, start, end, status …).
     When first-row headers don't match the required columns and no
     columnMap is provided, resolves with { needsMapping: true, headers, rows }
     so the caller can render a column-mapper step.

     parseXLSXWithMap(file, columnMap) — same, but applies the caller-
     supplied mapping before delegating to parseCSV.
     ========================================================================= */

  var XLSX_REQUIRED_COLS = ['task_id', 'wbs', 'name', 'start', 'end', 'status'];

  function _xlsxRowsToCsv(headers, rows) {
    /* Convert SheetJS rows (arrays) to a CSV text string reusing parseCSV. */
    var lines = [headers.map(function (h) {
      return h.indexOf(',') !== -1 || h.indexOf('"') !== -1
        ? '"' + h.replace(/"/g, '""') + '"' : h;
    }).join(',')];

    for (var i = 1; i < rows.length; i++) {
      var cells = rows[i] || [];
      var csvCells = headers.map(function (_, j) {
        var c = String(cells[j] == null ? '' : cells[j]);
        if (c.indexOf(',') !== -1 || c.indexOf('"') !== -1) {
          c = '"' + c.replace(/"/g, '""') + '"';
        }
        return c;
      });
      lines.push(csvCells.join(','));
    }
    return lines.join('\n');
  }

  function parseXLSX(file) {
    /* Returns Promise resolving to one of:
         { parents, leaves, errors, warnings, needsMapping: false }
         { needsMapping: true, headers, rows, errors: [], warnings: [] } */
    return new Promise(function (resolve) {
      if (typeof window.XLSX === 'undefined') {
        resolve({
          parents: [], leaves: [], needsMapping: false,
          errors:   [{ row: 0, field: null, message: 'SheetJS (XLSX) library is not loaded. Ensure the CDN script is present in the page.' }],
          warnings: [],
        });
        return;
      }

      var reader = new FileReader();
      reader.onload = function (e) {
        var wb;
        try {
          wb = window.XLSX.read(new Uint8Array(e.target.result), { type: 'array' });
        } catch (ex) {
          resolve({
            parents: [], leaves: [], needsMapping: false,
            errors:   [{ row: 0, field: null, message: 'Failed to parse XLSX file: ' + ex.message }],
            warnings: [],
          });
          return;
        }

        var sheetName = wb.SheetNames && wb.SheetNames[0];
        if (!sheetName) {
          resolve({
            parents: [], leaves: [], needsMapping: false,
            errors:   [{ row: 0, field: null, message: 'Workbook contains no sheets.' }],
            warnings: [],
          });
          return;
        }

        var sheet = wb.Sheets[sheetName];
        /* raw: false → all values as formatted strings; header: 1 → 2-D array */
        var rows  = window.XLSX.utils.sheet_to_json(sheet, { header: 1, raw: false, defval: '' });

        if (!rows.length) {
          resolve({
            parents: [], leaves: [], needsMapping: false,
            errors:   [{ row: 0, field: null, message: 'Sheet "' + sheetName + '" is empty.' }],
            warnings: [],
          });
          return;
        }

        var headers = (rows[0] || []).map(function (h) { return String(h || '').trim(); });

        var hasRequired = XLSX_REQUIRED_COLS.every(function (c) {
          return headers.indexOf(c) !== -1;
        });

        if (!hasRequired) {
          /* Return raw data for the column-mapper UI */
          resolve({ needsMapping: true, headers: headers, rows: rows, errors: [], warnings: [] });
          return;
        }

        var csvText = _xlsxRowsToCsv(headers, rows);
        var result  = parseCSV(csvText);
        result.needsMapping = false;
        resolve(result);
      };
      reader.onerror = function () {
        resolve({
          parents: [], leaves: [], needsMapping: false,
          errors:   [{ row: 0, field: null, message: 'FileReader error reading XLSX file.' }],
          warnings: [],
        });
      };
      reader.readAsArrayBuffer(file);
    });
  }

  function parseXLSXWithMap(file, columnMap) {
    /* columnMap: { originalHeader: csvColumnName } */
    return new Promise(function (resolve) {
      if (typeof window.XLSX === 'undefined') {
        resolve({
          parents: [], leaves: [], needsMapping: false,
          errors:   [{ row: 0, field: null, message: 'SheetJS (XLSX) library is not loaded.' }],
          warnings: [],
        });
        return;
      }

      var reader = new FileReader();
      reader.onload = function (e) {
        var wb;
        try {
          wb = window.XLSX.read(new Uint8Array(e.target.result), { type: 'array' });
        } catch (ex) {
          resolve({
            parents: [], leaves: [], needsMapping: false,
            errors:   [{ row: 0, field: null, message: 'Failed to parse XLSX: ' + ex.message }],
            warnings: [],
          });
          return;
        }

        var sheetName = wb.SheetNames && wb.SheetNames[0];
        if (!sheetName) {
          resolve({
            parents: [], leaves: [], needsMapping: false,
            errors:   [{ row: 0, field: null, message: 'Workbook contains no sheets.' }],
            warnings: [],
          });
          return;
        }

        var sheet = wb.Sheets[sheetName];
        var rows  = window.XLSX.utils.sheet_to_json(sheet, { header: 1, raw: false, defval: '' });
        if (!rows.length) {
          resolve({
            parents: [], leaves: [], needsMapping: false,
            errors:   [{ row: 0, field: null, message: 'Sheet is empty.' }],
            warnings: [],
          });
          return;
        }

        var origHeaders = (rows[0] || []).map(function (h) { return String(h || '').trim(); });
        var mappedHeaders = origHeaders.map(function (h) {
          return (columnMap && columnMap[h]) ? columnMap[h] : h;
        });

        var csvText = _xlsxRowsToCsv(mappedHeaders, rows);
        var result  = parseCSV(csvText);
        result.needsMapping = false;
        resolve(result);
      };
      reader.onerror = function () {
        resolve({
          parents: [], leaves: [], needsMapping: false,
          errors:   [{ row: 0, field: null, message: 'FileReader error.' }],
          warnings: [],
        });
      };
      reader.readAsArrayBuffer(file);
    });
  }

  /* --- attach -------------------------------------------------------------- */

  window.FS = window.FS || {};
  window.FS.api = window.FS.api || {};
  window.FS.api.programmeImport = {
    parseCSV:          parseCSV,
    parseMSProjectXML: parseMSProjectXML,
    parseXLSX:         parseXLSX,
    parseXLSXWithMap:  parseXLSXWithMap,
  };

}());
