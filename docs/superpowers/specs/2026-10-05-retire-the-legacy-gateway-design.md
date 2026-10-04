# Sub-project 1 — retire the legacy gateway's data routes (design, 2026-10-05)

Status: **design.** Part of `2026-10-05-tenancy-roadmap-design.md` (decisions D1–D6).
Spans `fieldsight-pipeline` and `fieldsight-ui`.

## Why

`lambda_fieldsight_api` (`fieldsight-prod-api`) reads tenant data from S3 and DynamoDB
with **no company concept**: its roles come from DynamoDB and stop at admin/gm,
`can_access_user_data` returns True for admin/gm, and presign skips owner checks for
them. So an admin or gm of any customer can read any other customer's reports,
transcripts, recordings and photos by naming a folder. #1026 closed the lake-wide
summary; this closes the rest — by taking tenant data out of the gateway, not by
patching it. The gateway cannot be patched into correctness: it has no database (D2).

## Every legacy route, and its fate

Traffic is prod, 14 days to 2026-10-05.

| route | calls | data source | ACL today | fate |
|---|---|---|---|---|
| `/api/actions` GET | 2559 | DynamoDB `fieldsight-audit`, `PK=ACTIONS#{date}`, checkbox per (date, folder, **position**) | admin/gm any folder | **move to org-api `action_items.status`**, backfill the 356 rows, then **410** |
| `/api/actions/toggle` POST | 1 | same table | same | **410** after the move |
| `/api/actions` POST (create) | — | report gateway | same | already rides org-api for edits; create moves too |
| `/api/timeline` | 777 | S3 reports | admin/gm any folder | web: **stop falling back** (`legacyReadFallback=false`); then **410** |
| `/api/dates` | 1 | S3 reports | per-folder scope, admin/gm unrestricted | **410** (web reads org-api `/dates`) |
| `/api/sites` `/api/users` `/api/site-users` | 194 | **frozen `user_mapping.json`** | mapping roster | **move web to org-api** `/sites`, `/members`, `/sites/{id}/members`; then **410** |
| `/api/sites` `/api/users` POST/PATCH | — | DynamoDB / mapping | — | **410** (org-api owns the directory) |
| `/api/transcripts` `/api/audio-segments` `/api/video-segments` `/api/media/presigned-url` `/api/reports/history` | ≈0 | S3 | admin/gm unrestricted | **410** now — the web already uses org-api; nobody else is calling |
| `/api/recording-stats` `/api/reports/generate` | — | — | — | **410** unless a caller is found (step 1) |
| `/api/search` | 63 | proxies to ask-agent → **rag-search** with `caller_sub` | rag-search scopes by org | **keep** as a proxy (carries no tenant data itself) |
| `/api/ask` `/api/ask/corroborate` | 15 | proxies to ask-agent → RAG with `caller_sub` | rag-search | **keep** |
| `/api/ask/voice` | 10 | device → STT → RAG with `caller_sub` | rag-search | **keep** (D6: device contract) |
| `/api/health` | 26 | — | — | keep |

**After this sub-project the gateway holds no tenant data.** It is an authenticated
proxy to the RAG/Ask lambdas, whose access decisions are made in-VPC against the
directory (prod `fieldsight-prod-ask-agent` has `RAG_SEARCH_FUNCTION` set — verified
2026-10-05). Its DynamoDB user table and `user_mapping.json` reads go with the data
routes.

## Order — instrument, move, then close

1. **Know every caller before cutting.** Add one structured log line per request to
   the gateway: route, method, caller role, whether `caller_sub` is present, and the
   User-Agent family (browser / Android / other). Ship it, observe for one full working
   week. This answers what the current logs cannot: whether `/api/timeline`'s 777 calls
   are the web's fallback or a device, and whether anything still calls the ≈0 routes.
   **No route is closed while an unexplained caller remains.**
2. **Move the web off each data route** (fieldsight-ui):
   - `timeline.js`: legacy fallback off (`FS_LEGACY_READ_FALLBACK=false` on `main`,
     the Amplify build variable already wired in `amplify.yml`); when org-api denies,
     the page shows org-api's own message — which already distinguishes "no access"
     from "this login has no folder".
   - `sites.js`: `getSites` / `getUsers` / `getSiteUsers` → org-api `/sites`,
     `/members`, `/sites/{id}/members`. Where a role cannot call `/members`, the page
     shows what that role's `visible_scope` returns — never a fallback to the mapping.
   - `actions.js`: checkbox state read from and written to `action_items.status`
     through org-api (the Tasks page already PATCHes `action_items`).
3. **Backfill the 356 legacy checkbox rows** into `action_items.status`. Each row
   carries `PK=ACTIONS#{date}`, an SK with the folder (when present), topic and action
   positions, and — what makes this safe — **the action's own text** (`action_text`),
   plus `checked`, `checked_at`, `checked_by`. Match on **text, not position**: within
   (date, folder), the `action_items` row whose normalised text equals `action_text`.
   Positions shift when a day's report is regenerated; the text does not. A row with no
   folder in its SK, or with zero or several text matches, is **listed, never guessed**.
   A one-off, idempotent, dry-run-first operator task (the `photo_collapse` pattern in
   org-api: invoked by hand, `apply` must be exactly true). Prod run is the owner's.
4. **Close the data routes**: they return **410 Gone** with a body naming the org-api
   replacement. 410, not 404, so a forgotten caller fails loudly and the log line from
   step 1 names it.
5. **Remove the dead code** (handlers, DynamoDB users/audit reads, mapping reads) one
   release after step 4 has been quiet.

Each step is its own release and independently revertible. Steps 2–3 change no
behaviour a user can see except that the data now comes from the directory.

## What changes for users

- Nothing visible for the web, except: an admin/gm who was relying on the legacy
  fallback to see **another company's** day no longer can. That is the fix.
- Action-item ticks keep their state (backfill).
- Devices: unchanged (`/api/ask/voice` kept).

## Tests that must go red without the change

- Gateway: each closed route returns 410 with the replacement named — for admin, gm and
  worker; the kept proxy routes still forward `caller_sub`.
- No closed route reads S3 tenant data or DynamoDB users: `s3_client.get_object` /
  `list_objects_v2` / `dynamodb.Table` are never called on a 410 path.
- Web: `timeline.js` never calls `request('/timeline')` when `legacyReadFallback` is
  false; `sites.js` and `actions.js` build their requests through `orgRequest`, and the
  bodies are checked on the request handed to it (the api-layer whitelist trap).
- Backfill: dry run reports per-row match / no-match and writes nothing; `apply=true`
  writes only matched rows; a second apply writes nothing (idempotent); an unmatched,
  ambiguous or folder-less row is listed and left; matching is by text, so a reordered
  action list still maps each tick to the same task.
- The instrumentation line carries no tenant content (route, role, flags only).

## Risks

- **An unknown caller of a closed route.** Mitigated by step 1 and by 410 (loud).
- **A tick lands on the wrong task.** Mitigated by matching on the stored action text
  within (date, folder), never on position; ambiguous or missing matches are listed and
  left for a person. Some early rows have no folder in their SK (e.g.
  `TOPIC#0#ACTION#flag_0`) — those are listed, not spread across every folder that day.
- **`FS_LEGACY_READ_FALLBACK=false` turns a silent cross-company read into a visible
  "no access".** Intended.

## Out of scope

The frozen mapping's other readers (sub-project 2), project-owned data (3), lake key
layout (4), the Cognito pool split (S1), connection pooling (S2).
