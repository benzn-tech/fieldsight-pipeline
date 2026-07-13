#!/usr/bin/env node
/**
 * scripts/backfill-audit-user-dimension.js
 *
 * One-time (idempotent, re-runnable) migration for the `fieldsight-audit`
 * table: moves "current state" action-audit records from the OLD SK
 * (`TOPIC#{tid}#ACTION#{idx}`) to the NEW user-dimension SK
 * (`USER#{owner_folder}#TOPIC#{tid}#ACTION#{idx}`), so the audit key finally
 * carries the report OWNER's folder (not just date+topic+action-index).
 * PK (`ACTIONS#{date}`) is unchanged.
 *
 * See plan (fieldsight-ui repo):
 *   docs/superpowers/plans/2026-07-13-user-dimension-audit-key.md
 *   §0.2 (6-date -> owner table), §1.4 (migration rules + ambiguity
 *   fallback), Task 2.
 *
 * Scope: only "current state" items — PK begins_with 'ACTIONS#' AND SK
 * begins_with 'TOPIC#'. Deliberately does NOT touch:
 *   - AUDIT#{date} append-only log items (backend only writes these, never
 *     reads them back — see Task 1 comment in lambda_fieldsight_api.py).
 *   - Items already carrying the new USER# SK (re-run safety / idempotency).
 *
 * Usage:
 *   node scripts/backfill-audit-user-dimension.js --table fieldsight-audit \
 *     --bucket fieldsight-data-509194952652 [--apply]
 *
 * Default is DRY-RUN: prints the full `oldSK -> newSK (owner)` mapping table
 * plus a summary, and makes ZERO DynamoDB writes (scan + S3 list/get only).
 * Pass --apply to actually migrate (put new item, then delete old item).
 *
 * Idempotent: a re-run scans for SK begins_with 'TOPIC#'. After a successful
 * --apply there are none left (they're all USER#... now) -> no-op.
 *
 * Zero npm dependencies: shells out to the `aws` CLI via
 * child_process.execFileSync using array args (NOT a shell string) so
 * Windows/Git-Bash/MSYS argument mangling never enters the picture (see
 * pipeline CLAUDE.md BUG-27/28: MSYS rewrites bare "/" args like
 * `--delimiter /` into Windows paths when a POSIX shell invokes aws.exe
 * directly; execFileSync never goes through that shell). All JSON is parsed
 * with Node's JSON.parse (BUG-29: no python3 on this machine).
 *
 * Honors AWS_PROFILE from the environment (the caller sets
 * fieldsight-deployer). Region is hardcoded to ap-southeast-2 per CLAUDE.md.
 */
'use strict';

const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');

const REGION = 'ap-southeast-2';

// ---------------------------------------------------------------------------
// CLI args
// ---------------------------------------------------------------------------

function parseArgs(argv) {
  const args = { table: null, bucket: null, apply: false, help: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--table') args.table = argv[++i];
    else if (a === '--bucket') args.bucket = argv[++i];
    else if (a === '--apply') args.apply = true;
    else if (a === '--help' || a === '-h') args.help = true;
    else throw new Error(`Unknown argument: ${a}`);
  }
  if (!args.help) {
    if (!args.table) throw new Error('Missing required --table <dynamodb-table-name>');
    if (!args.bucket) throw new Error('Missing required --bucket <s3-bucket-name>');
  }
  return args;
}

function printUsage() {
  console.log([
    'Usage: node scripts/backfill-audit-user-dimension.js --table <dynamodb-table> --bucket <s3-bucket> [--apply]',
    '',
    '  --table   DynamoDB table name (e.g. fieldsight-audit)',
    '  --bucket  S3 bucket holding reports/{date}/{folder}/daily_report.json (e.g. fieldsight-data-509194952652)',
    '  --apply   Perform the migration (put new USER# item, then delete old item). Omit for DRY-RUN (default, zero writes).',
  ].join('\n'));
}

// ---------------------------------------------------------------------------
// aws CLI helpers
// ---------------------------------------------------------------------------

function awsRaw(args) {
  return execFileSync('aws', [...args, '--region', REGION, '--output', 'json'], {
    encoding: 'utf8',
    maxBuffer: 1024 * 1024 * 128,
  });
}

function awsJson(args) {
  const out = awsRaw(args);
  if (!out || !out.trim()) return null;
  return JSON.parse(out);
}

function tmpJsonFile(prefix, obj) {
  const file = path.join(os.tmpdir(), `${prefix}-${process.pid}-${crypto.randomBytes(6).toString('hex')}.json`);
  fs.writeFileSync(file, JSON.stringify(obj));
  return file;
}

function toFileUri(p) {
  // Forward-slash form works for aws.exe on Windows and POSIX alike. This
  // sidesteps BUG-27's cygpath dance -- that bug is about fileb:// binary
  // reads invoked *through* Git Bash; here Node calls aws.exe directly
  // (no shell) and the payload is a small JSON text file, not a zip.
  return 'file://' + p.split(path.sep).join('/');
}

// Runs `aws <commandArgs...> --cli-input-json file://tmp.json`. Body is
// written to a scratch temp file (sidesteps CLI arg length/quoting issues
// entirely) and removed afterward regardless of outcome.
function awsCliInput(commandArgs, bodyObj) {
  const file = tmpJsonFile('audit-backfill-in', bodyObj);
  try {
    return awsJson([...commandArgs, '--cli-input-json', toFileUri(file)]);
  } finally {
    try { fs.unlinkSync(file); } catch (_e) { /* best effort cleanup */ }
  }
}

// ---------------------------------------------------------------------------
// Step 1: scan un-migrated current-state records
// ---------------------------------------------------------------------------

function scanUnmigratedItems(table) {
  const items = [];
  let exclusiveStartKey;
  do {
    const body = {
      TableName: table,
      FilterExpression: 'begins_with(PK, :pk) AND begins_with(SK, :sk)',
      ExpressionAttributeValues: { ':pk': { S: 'ACTIONS#' }, ':sk': { S: 'TOPIC#' } },
    };
    if (exclusiveStartKey) body.ExclusiveStartKey = exclusiveStartKey;
    const resp = awsCliInput(['dynamodb', 'scan'], body);
    for (const it of (resp && resp.Items) || []) items.push(it);
    exclusiveStartKey = resp && resp.LastEvaluatedKey;
  } while (exclusiveStartKey);
  return items;
}

// ---------------------------------------------------------------------------
// Step 2: owner derivation
// ---------------------------------------------------------------------------

function listReportFolders(bucket, date) {
  const folders = [];
  let continuationToken;
  do {
    const args = [
      's3api', 'list-objects-v2',
      '--bucket', bucket,
      '--prefix', `reports/${date}/`,
      '--delimiter', '/',
    ];
    if (continuationToken) args.push('--continuation-token', continuationToken);
    const resp = awsJson(args);
    for (const cp of (resp && resp.CommonPrefixes) || []) {
      // Prefix looks like "reports/{date}/{folder}/"
      const parts = cp.Prefix.split('/').filter(Boolean); // ['reports', date, folder]
      const folder = parts[2];
      if (folder) folders.push(folder);
    }
    continuationToken = resp && resp.NextContinuationToken;
  } while (continuationToken);
  return folders;
}

function getS3JsonObject(bucket, key) {
  const file = path.join(os.tmpdir(), `audit-backfill-obj-${process.pid}-${crypto.randomBytes(6).toString('hex')}.json`);
  try {
    execFileSync('aws', ['s3api', 'get-object', '--bucket', bucket, '--key', key, '--region', REGION, file], {
      encoding: 'utf8',
    });
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } finally {
    try { fs.unlinkSync(file); } catch (_e) { /* best effort cleanup */ }
  }
}

// Best-effort text extraction for the §1.4 ambiguity fallback. Current
// production data never exercises this path (§0.2: every one of the 6 dates
// has exactly one owner folder) -- this exists purely so a future re-run
// against multi-owner data doesn't silently misattribute. It knows the
// documented action_items/safety_flags shape (lambda_report_generator.py's
// DAILY_REPORT_SCHEMA) and tries a handful of plausible field names for the
// newer obs_/quality idx forms. If it can't find a confident match it
// returns null -- the caller treats that as "no match", never a guess.
function extractActionText(topic, idxStr) {
  if (!topic) return null;
  if (/^\d+$/.test(idxStr)) {
    const items = topic.action_items || [];
    const it = items[Number(idxStr)];
    return it ? (it.action || it.text || null) : null;
  }
  if (idxStr.startsWith('flag_')) {
    const n = Number(idxStr.slice('flag_'.length));
    const flags = topic.safety_flags || [];
    const f = flags[n];
    return f ? (f.observation || f.text || null) : null;
  }
  if (idxStr.startsWith('obs_')) {
    const n = Number(idxStr.slice('obs_'.length));
    const obsList = topic.observations || topic.quality_observations || [];
    const o = obsList[n];
    return o ? (o.observation || o.text || o.description || null) : null;
  }
  if (idxStr === 'quality') {
    const q = topic.quality || topic.quality_notes || topic.quality_flags;
    if (!q) return null;
    if (typeof q === 'string') return q;
    if (Array.isArray(q)) return (q[0] && (q[0].observation || q[0].text)) || null;
    return q.observation || q.text || null;
  }
  return null;
}

function findTopic(topics, tid) {
  const tidNum = Number(tid);
  return (topics || []).find((t) => Number(t.topic_id) === tidNum) || null;
}

// Returns { owner } on a unique match, or { owner: null, reason } when
// ambiguous / unresolvable -- callers must treat a null owner as "skip and
// add to manual review", never guess.
function deriveOwnerForRecord(bucket, date, folders, tid, idx, actionText) {
  const matches = [];
  for (const folder of folders) {
    let report;
    try {
      report = getS3JsonObject(bucket, `reports/${date}/${folder}/daily_report.json`);
    } catch (_e) {
      continue; // unreadable candidate does not count as a match
    }
    const topic = findTopic(report.topics, tid);
    if (!topic) continue;
    const text = extractActionText(topic, idx);
    if (text !== null && actionText && text.trim() === actionText.trim()) {
      matches.push(folder);
    }
  }
  if (matches.length === 1) return { owner: matches[0] };
  return {
    owner: null,
    reason: matches.length === 0
      ? `${folders.length} candidate folders (${folders.join(', ')}), none matched action_text`
      : `${folders.length} candidate folders, ${matches.length} matched action_text ambiguously (${matches.join(', ')})`,
  };
}

// ---------------------------------------------------------------------------
// Step 3: migrate
// ---------------------------------------------------------------------------

function parseOldSk(sk) {
  // OLD: TOPIC#{tid}#ACTION#{idx}. idx/tid never contain '#' (flag_0, obs_0,
  // quality, -1, or plain numeric) so a plain split is safe -- same
  // assumption Task 1's get_actions parser makes.
  const parts = sk.split('#');
  if (parts.length < 4 || parts[0] !== 'TOPIC' || parts[2] !== 'ACTION') return null;
  return { tid: parts[1], idx: parts[3] };
}

function buildNewSk(owner, tid, idx) {
  return `USER#${owner}#TOPIC#${tid}#ACTION#${idx}`;
}

function putItem(table, item) {
  awsCliInput(['dynamodb', 'put-item'], { TableName: table, Item: item });
}

function deleteItem(table, key) {
  awsCliInput(['dynamodb', 'delete-item'], { TableName: table, Key: key });
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

function main() {
  const argv = process.argv.slice(2);
  let args;
  try {
    args = parseArgs(argv);
  } catch (e) {
    console.error('Error:', e.message);
    printUsage();
    process.exitCode = 1;
    return;
  }
  if (args.help) {
    printUsage();
    return;
  }

  const mode = args.apply ? 'APPLY' : 'DRY-RUN';
  console.log(`[backfill] mode=${mode} table=${args.table} bucket=${args.bucket} region=${REGION} profile=${process.env.AWS_PROFILE || '(default)'}`);

  // --- Step 1: scan + backup dump (rollback insurance; always written, ---
  // --- dry-run or apply -- it's a local file, not an AWS write) ----------
  console.log("[backfill] scanning for un-migrated current-state records (PK begins_with 'ACTIONS#', SK begins_with 'TOPIC#') ...");
  let items;
  try {
    items = scanUnmigratedItems(args.table);
  } catch (e) {
    console.error('[backfill] FATAL: scan failed:', e.message);
    process.exitCode = 1;
    return;
  }
  console.log(`[backfill] found ${items.length} un-migrated record(s).`);

  const ts = Date.now();
  const backupPath = path.join(process.cwd(), `backfill-backup-${ts}.json`);
  fs.writeFileSync(backupPath, JSON.stringify(items, null, 2));
  console.log(`[backfill] raw backup dumped to ${backupPath} (rollback insurance).`);

  if (items.length === 0) {
    console.log('[backfill] nothing to migrate. Idempotent no-op.');
    return;
  }

  // --- Step 2: derive owner per distinct date -----------------------------
  const dates = [...new Set(items.map((it) => (it.PK && it.PK.S || '').replace(/^ACTIONS#/, '')))].sort();
  console.log(`[backfill] ${dates.length} distinct date(s): ${dates.join(', ')}`);

  const dateInfo = new Map(); // date -> { owner, folders, error }
  for (const date of dates) {
    try {
      const folders = listReportFolders(args.bucket, date);
      if (folders.length === 1) {
        dateInfo.set(date, { owner: folders[0], folders });
        console.log(`[backfill]   ${date} -> owner "${folders[0]}" (single report folder)`);
      } else {
        // Ambiguous at the date level -- resolved per-record below via §1.4.
        dateInfo.set(date, { owner: null, folders });
        console.log(`[backfill]   ${date} -> ${folders.length} candidate folder(s) (${folders.join(', ') || 'none'}); will resolve per-record via daily_report.json match`);
      }
    } catch (e) {
      dateInfo.set(date, { owner: null, folders: [], error: e.message });
      console.error(`[backfill]   ${date} -> ERROR deriving owner: ${e.message}`);
    }
  }

  // --- Step 3: build the migration plan ------------------------------------
  const plan = [];         // { item, date, oldSk, newSk, owner }
  const manualReview = []; // { date, oldSk, reason }

  for (const item of items) {
    const date = (item.PK && item.PK.S || '').replace(/^ACTIONS#/, '');
    const oldSk = item.SK && item.SK.S;
    const parsed = oldSk && parseOldSk(oldSk);
    if (!parsed) {
      manualReview.push({ date, oldSk: oldSk || '(missing SK)', reason: 'unparsable SK (not TOPIC#{tid}#ACTION#{idx})' });
      continue;
    }
    const { tid, idx } = parsed;
    const info = dateInfo.get(date);
    let owner = info && info.owner;
    if (!owner) {
      if (info && info.error) {
        manualReview.push({ date, oldSk, reason: `owner lookup failed: ${info.error}` });
        continue;
      }
      const actionText = item.action_text && item.action_text.S;
      const resolved = deriveOwnerForRecord(args.bucket, date, (info && info.folders) || [], tid, idx, actionText);
      if (!resolved.owner) {
        manualReview.push({ date, oldSk, reason: resolved.reason || 'ambiguous owner, no unique match' });
        continue;
      }
      owner = resolved.owner;
    }
    plan.push({ item, date, oldSk, newSk: buildNewSk(owner, tid, idx), owner });
  }

  // --- print mapping table --------------------------------------------------
  console.log('');
  console.log('[backfill] === mapping (oldSK -> newSK) ===');
  for (const row of plan) {
    console.log(`  ${row.date} | ${row.oldSk} -> ${row.newSk} (${row.owner})`);
  }
  console.log('');
  console.log(`[backfill] summary: ${plan.length} record(s) mapped, ${manualReview.length} record(s) need manual review, ${items.length} total scanned.`);

  if (manualReview.length > 0) {
    console.log('');
    console.log('[backfill] === MANUAL REVIEW (ambiguous owner, NOT migrated -- never guessed) ===');
    for (const mr of manualReview) {
      console.log(`  date=${mr.date} oldSK=${mr.oldSk} reason=${mr.reason}`);
    }
  }

  // per-date breakdown (cross-check against plan §0.2's table)
  const byDate = new Map();
  for (const row of plan) {
    if (!byDate.has(row.date)) byDate.set(row.date, { count: 0, owners: new Set() });
    const d = byDate.get(row.date);
    d.count += 1;
    d.owners.add(row.owner);
  }
  console.log('');
  console.log('[backfill] === per-date owner summary ===');
  for (const [date, d] of [...byDate.entries()].sort()) {
    console.log(`  ${date}: ${d.count} record(s) -> ${[...d.owners].join(', ')}`);
  }

  if (!args.apply) {
    console.log('');
    console.log('[backfill] DRY-RUN complete. Zero DynamoDB writes made. Re-run with --apply to migrate.');
    return;
  }

  // --- Step 3 (apply): put new item, then delete old item ------------------
  console.log('');
  console.log('[backfill] APPLY: migrating records (put new USER# item, then delete old item) ...');
  const applyErrors = [];
  let migrated = 0;
  for (const row of plan) {
    try {
      const newItem = Object.assign({}, row.item); // copy ALL original attributes
      newItem.SK = { S: row.newSk };
      newItem.user_folder = { S: row.owner };       // set/overwrite user_folder
      putItem(args.table, newItem);
      deleteItem(args.table, { PK: row.item.PK, SK: row.item.SK });
      migrated += 1;
    } catch (e) {
      // Collect + report -- one failure must not corrupt the run or abort
      // remaining records. Put-then-delete order means a failure here always
      // leaves the OLD item intact (either put never landed, or delete
      // failed after a successful put -- next run's scan picks either case
      // back up: no old item left = already migrated; old item still there
      // = retry naturally re-covers it, and put-item is an overwrite so a
      // partial "put succeeded, delete failed" retry is a safe no-op put).
      applyErrors.push({ date: row.date, oldSk: row.oldSk, newSk: row.newSk, error: e.message });
      console.error(`[backfill]   FAILED ${row.oldSk} -> ${row.newSk}: ${e.message}`);
    }
  }
  console.log('');
  console.log(`[backfill] APPLY complete: ${migrated}/${plan.length} migrated, ${applyErrors.length} failed, ${manualReview.length} skipped (manual review).`);
  if (applyErrors.length > 0) {
    console.log('[backfill] === FAILED (old item still in place; safe to re-run script) ===');
    for (const e of applyErrors) {
      console.log(`  date=${e.date} oldSK=${e.oldSk} -> newSK=${e.newSk} error=${e.error}`);
    }
    process.exitCode = 1;
  }
}

main();
