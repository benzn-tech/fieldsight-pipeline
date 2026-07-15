#!/usr/bin/env node
// compare-timeline.mjs — parallel-run differ, authority-flip gate (Task 5).
//
// Proves the org-api /timeline compat shim serves HISTORY byte-identically
// to the legacy prod report path, before the prod DNS/traffic promotion
// (RETARGET override 6). TOOLING ONLY — no live endpoint is called by
// writing/checking this in; the recorded evidence run happens after
// promotion (docs/superpowers/plans/2026-07-14-authority-flip.md).
//
// RETARGET adjustment (baked in, differs from the brief's original values):
//   PROD_BASE (customer site) =
//     https://ys94qy2tk0.execute-api.ap-southeast-2.amazonaws.com/prod/api
//   ORG_BASE  = the SAME gateway (org routes live under /api/org), so the
//     shim is called at `${ORG_BASE}/org/timeline`. Neither value is
//     hardcoded below — both come from env, so this also works unmodified
//     against the legacy khfj3p1fkb gateway as optional extra evidence.
//
// Env (required): PROD_BASE, ORG_BASE, IDTOKEN (raw Cognito idToken, sent
//   AS-IS — no "Bearer " prefix; this project's convention).
// Args: repeatable `--date YYYY-MM-DD --user Folder_Name` pairs (each
//   --date pairs with the --user that follows it), or `--dates-from <file>`
//   (lines of `date,user`; blank/`#` lines skipped). `--self-test` runs
//   embedded assertions with no network and no env required.
//
// Exit 0 only if every non-OVERRIDE pair is IDENTICAL (or --self-test
// passes); 1 on any DIFF, ERROR, HTTP status mismatch, or failed assertion.

/* ------------------------------ stable stringify ------------------------------ */

// Recursive key-sort then JSON.stringify. Arrays keep original order (only
// object keys are sorted) so ordered lists like `topics` aren't scrambled.
function sortKeysDeep(value) {
  if (Array.isArray(value)) return value.map(sortKeysDeep);
  if (value !== null && typeof value === 'object') {
    const out = {};
    for (const key of Object.keys(value).sort()) out[key] = sortKeysDeep(value[key]);
    return out;
  }
  return value;
}
const stableStringify = (value) => JSON.stringify(sortKeysDeep(value));

// Walks two sorted values in lockstep; returns the JSON path of the first
// divergence (e.g. "$.topics[2].time_range"), or null if deeply equal.
function firstDivergencePath(a, b, path = '$') {
  if (a === b) return null;
  const aArr = Array.isArray(a), bArr = Array.isArray(b);
  const aObj = a !== null && typeof a === 'object' && !aArr;
  const bObj = b !== null && typeof b === 'object' && !bArr;
  if (aArr && bArr) {
    const len = Math.max(a.length, b.length);
    for (let i = 0; i < len; i++) {
      if (i >= a.length || i >= b.length) return `${path}[${i}] (length ${a.length} vs ${b.length})`;
      const sub = firstDivergencePath(a[i], b[i], `${path}[${i}]`);
      if (sub) return sub;
    }
    return null;
  }
  if (aObj && bObj) {
    const keys = Array.from(new Set([...Object.keys(a), ...Object.keys(b)])).sort();
    for (const k of keys) {
      if (!(k in a)) return `${path}.${k} (missing on left)`;
      if (!(k in b)) return `${path}.${k} (missing on right)`;
      const sub = firstDivergencePath(a[k], b[k], `${path}.${k}`);
      if (sub) return sub;
    }
    return null;
  }
  if (aArr !== bArr || aObj !== bObj) return `${path} (type mismatch: ${typeof a} vs ${typeof b})`;
  return `${path} (${JSON.stringify(a)} vs ${JSON.stringify(b)})`;
}

/* ---------------------------------- self-test ---------------------------------- */

function runSelfTest() {
  const failures = [];
  const assert = (name, cond) => { if (!cond) failures.push(name); };

  const a1 = { b: 1, a: { z: 2, y: 3 } }, a2 = { a: { y: 3, z: 2 }, b: 1 };
  assert('key-order invariance', stableStringify(a1) === stableStringify(a2));

  const b1 = { topics: [{ title: 'x', time_range: '08:00-09:00' }] };
  const b2 = { topics: [{ title: 'x', time_range: '08:00-09:30' }] };
  const divPath = firstDivergencePath(sortKeysDeep(b1), sortKeysDeep(b2));
  assert('nested divergence path found', divPath === '$.topics[0].time_range ("08:00-09:00" vs "08:00-09:30")');

  const nf1 = { message: 'No report for Ben_Lin on 2026-06-01', date: '2026-06-01' };
  const nf2 = { date: '2026-06-01', message: 'No report for Ben_Lin on 2026-06-01' };
  assert('404-body equality', stableStringify(nf1) === stableStringify(nf2));

  const c1 = { topics: [{ id: 1 }, { id: 2 }] }, c2 = { topics: [{ id: 2 }, { id: 1 }] };
  assert('array order preserved (reorder is a real diff)', stableStringify(c1) !== stableStringify(c2));

  if (failures.length) { console.log(`SELF-TEST FAIL: ${failures.join(', ')}`); return 1; }
  console.log('SELF-TEST PASS: 4/4 assertions (key-order invariance, nested divergence path, 404-body equality, array order preserved)');
  return 0;
}

/* ------------------------------------ args/env ------------------------------------ */

function usageError(msg) {
  console.error(`Usage error: ${msg}`);
  console.error('Usage: PROD_BASE=... ORG_BASE=... IDTOKEN=... node scripts/compare-timeline.mjs --date YYYY-MM-DD --user Folder_Name [...] | --dates-from <file>');
  console.error('       node scripts/compare-timeline.mjs --self-test');
  process.exit(1);
}

function parseArgs(argv) {
  const pairs = [];
  let datesFrom = null, pendingDate = null;
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--date') {
      pendingDate = argv[++i];
      if (!pendingDate) usageError('--date requires a value');
    } else if (arg === '--user') {
      const user = argv[++i];
      if (!user) usageError('--user requires a value');
      if (pendingDate === null) usageError('--user must follow a --date');
      pairs.push({ date: pendingDate, user });
      pendingDate = null;
    } else if (arg === '--dates-from') {
      datesFrom = argv[++i];
      if (!datesFrom) usageError('--dates-from requires a file path');
    } else {
      usageError(`unrecognized argument: ${arg}`);
    }
  }
  if (pendingDate !== null) usageError('--date given without a following --user');
  return { pairs, datesFrom };
}

async function loadPairsFromFile(filePath) {
  const fs = await import('node:fs/promises');
  const text = await fs.readFile(filePath, 'utf8');
  const pairs = [];
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const idx = line.indexOf(',');
    if (idx === -1) usageError(`--dates-from line missing comma: "${line}"`);
    const date = line.slice(0, idx).trim(), user = line.slice(idx + 1).trim();
    if (!date || !user) usageError(`--dates-from line malformed: "${line}"`);
    pairs.push({ date, user });
  }
  return pairs;
}

/* -------------------------------------- fetch -------------------------------------- */

async function fetchOnce(url, idToken) {
  const res = await fetch(url, { headers: { Authorization: idToken } });
  const text = await res.text();
  let body;
  try { body = JSON.parse(text); } catch { body = { _unparsable_body: text }; }
  return { status: res.status, body };
}

async function fetchWithRetry(url, idToken) {
  try { return await fetchOnce(url, idToken); }
  catch { try { return await fetchOnce(url, idToken); } catch (err2) { return { error: err2 }; } }
}

// topics count + per-topic {time_range, n_action_items, n_safety_flags,
// n_photos} — the field-presence summary for an OVERRIDE (live_extraction) day.
function overrideSummary(body) {
  const topics = Array.isArray(body.topics) ? body.topics : [];
  return {
    topics_count: topics.length,
    topics: topics.map((t) => ({
      time_range: t.time_range ?? null,
      n_action_items: Array.isArray(t.action_items) ? t.action_items.length : 0,
      n_safety_flags: Array.isArray(t.safety_flags) ? t.safety_flags.length : 0,
      n_photos: Array.isArray(t.related_photos) ? t.related_photos.length : 0,
    })),
  };
}

/* -------------------------------------- main -------------------------------------- */

async function main() {
  const argv = process.argv.slice(2);
  if (argv.includes('--self-test')) process.exit(runSelfTest());

  const PROD_BASE = process.env.PROD_BASE, ORG_BASE = process.env.ORG_BASE, IDTOKEN = process.env.IDTOKEN;
  if (!PROD_BASE) usageError('env PROD_BASE is required');
  if (!ORG_BASE) usageError('env ORG_BASE is required');
  if (!IDTOKEN) usageError('env IDTOKEN is required');

  const { pairs: argPairs, datesFrom } = parseArgs(argv);
  const pairs = datesFrom ? await loadPairsFromFile(datesFrom) : argPairs;
  if (pairs.length === 0) usageError('no (date,user) pairs given — use --date/--user or --dates-from');

  let exitCode = 0;

  for (const { date, user } of pairs) {
    const qs = `date=${encodeURIComponent(date)}&user=${encodeURIComponent(user)}`;
    const label = `${date} ${user}`;
    const [prod, org] = await Promise.all([
      fetchWithRetry(`${PROD_BASE}/timeline?${qs}`, IDTOKEN),
      fetchWithRetry(`${ORG_BASE}/org/timeline?${qs}`, IDTOKEN),
    ]);

    if (prod.error || org.error) {
      exitCode = 1;
      const which = prod.error ? 'PROD' : 'ORG';
      console.log(`ERROR  ${label} — ${which} fetch failed after retry: ${(prod.error || org.error).message}`);
      continue;
    }
    if (prod.status !== org.status) {
      exitCode = 1;
      console.log(`DIFF   ${label} — HTTP status mismatch: prod=${prod.status} org=${org.status}`);
      continue;
    }
    if (org.body && org.body._report_metadata && org.body._report_metadata.source === 'live_extraction') {
      const summary = overrideSummary(org.body);
      console.log(`OVERRIDE ${label} — status=${org.status} topics=${summary.topics_count}`);
      summary.topics.forEach((t, i) => console.log(
        `  topic[${i}] time_range=${JSON.stringify(t.time_range)} n_action_items=${t.n_action_items} n_safety_flags=${t.n_safety_flags} n_photos=${t.n_photos}`
      ));
      continue;
    }

    const prodStable = stableStringify(prod.body), orgStable = stableStringify(org.body);
    if (prodStable === orgStable) {
      console.log(`IDENTICAL ${label} — status=${prod.status}`);
    } else {
      exitCode = 1;
      const divergence = firstDivergencePath(sortKeysDeep(prod.body), sortKeysDeep(org.body));
      console.log(`DIFF   ${label} — status=${prod.status} first divergence: ${divergence}`);
    }
  }

  process.exit(exitCode);
}

main();
