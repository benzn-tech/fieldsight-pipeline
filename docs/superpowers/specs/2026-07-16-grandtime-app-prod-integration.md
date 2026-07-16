# GrandTime (F2SP self-dev recorder app) → FieldSight pipeline integration fixes

Date: 2026-07-16
Origin: user's 2026-07-16 morning recordings (Osmo/self-dev, "Ellesmere project") did not appear on the customer site. Root-caused live to the app uploading into the wrong environment + several format/attribution mismatches with the FieldSight ingest pipeline.

This spec is for a SEPARATE session working on the **GrandTime app** (and, where noted, a small pipeline-side decision). It is self-contained: all evidence, IDs, and the exact mismatches are below.

## User's confirmed intent + questions (2026-07-16) — the acceptance criteria

1. **Every test recording SELECTED the Ellesmere project in the app**, yet nothing attributed to Ellesmere. → The app's project selection is not reaching the pipeline's site attribution. Fixing this is the whole point: app project pick → `siteId` on the recording (G2) → pipeline attributes by `recordings.site_id` (G5b). Membership/who-recorded must NOT be the attribution key (it fails for the admin/test account and is the wrong model anyway).
2. **All content must default to the PRODUCTION S3 lake** (`fieldsight-data-509194952652`) — one unified source of valid data. The app currently writes to the TEST lake `fieldsight-data-test-509194952652`. (G1)
3. **Audio format:** the app records **M4A (AAC)**. The backend today ingests ONLY `.wav` and `.mp4` (VAD S3 trigger suffixes) — `.m4a`, `.mp3`, `.wmv` are silently dropped. Either the app emits `.wav`, or the pipeline adds `.m4a` to the VAD trigger + confirms ffmpeg AAC-in-M4A decode (it already decodes AAC-in-MP4). (G5a)

The multi-tenant model the fixes must respect: data is separated by COMPANY; each company's content stays within it; each project's (site's) content stays within that project; only higher-permission roles cross-cut. The admin/test account (Ben_Lin) is the deliberate cross-cutting exception. So attribution must be driven by an EXPLICIT company+site tag on the recording (siteId → site → company), not inferred from the recorder's role.

## What happened (evidence, verified live 2026-07-16)

The app (account "Ben_Lin") uploaded 5 files this morning:
- `users/Ben_Lin/video/2026-07-15/ben_lin_20260716_080228.mp4` (1 video)
- `users/Ben_Lin/audio/2026-07-15/ben_lin_20260716_095000.m4a`, `..._095112.m4a` (2 audio)
- `users/Ben_Lin/pictures/2026-07-15/ben_lin_20260716_095133.jpg`, `..._095157.jpg` (2 photos)

They landed in the **TEST bucket** `fieldsight-data-test-509194952652` (dev), NOT the production lake, so the customer prod site never saw them. (A one-off manual migration of these 5 files to prod was done separately to surface today's video — that is remediation, not the fix; the fixes below prevent recurrence.)

## The five gaps to fix

### G1 — Wrong environment (THE headline bug)
The app writes to the **dev/test** bucket. It must target **production**:
- Prod API/org gateway: `https://ys94qy2tk0.execute-api.ap-southeast-2.amazonaws.com/prod/api`
- Prod lake bucket: `fieldsight-data-509194952652`
- Prod Cognito pool (auth): `ap-southeast-2_q88pd6XXr` (client `4ratjdjonqm17tln6bs2761ci3`), USER_PASSWORD_AUTH, idToken placed bare in the `Authorization` header (no `Bearer`).
Find where the app selects its backend/bucket (build flavor, env config, or a hardcoded host) and point release builds at prod. Keep a dev flavor for testing, but the shipping build must be prod.

### G2 — Upload path: use the presigned recordings flow, not direct bucket writes
The 5 files created **no `recordings` table row** → the app uploaded directly to S3, bypassing the org-api. The designed path is:
1. `POST /api/org/recordings/upload-url` with body `{kind: "video"|"audio"|"photo", clientUuid, fileName, contentType, startedAt (ISO), siteId, endedAt?, durationS?, resolution?, codec?}` → returns `{recordingId, uploadUrl, s3Key}`. Idempotent on `clientUuid`.
2. `PUT` the bytes to `uploadUrl` (raw PUT, do not add headers that break the S3 signature).
3. `POST /api/org/recordings/{recordingId}/complete` with `{sizeBytes, gpsTrack?}`.
This registers the recording WITH `siteId` (see G5) and lands the object at the pipeline's `users/{display}/{video|audio|pictures}/{date}/` convention in the lake automatically. Switch the app to this flow against prod.

### G3 — Date-folder derivation is off by one (UTC vs NZ)
Files recorded 2026-07-16 were filed under the `2026-07-15` folder while the filename carried `20260716`. Same class as a bug just fixed in the pipeline orchestrator: the app is deriving the date in UTC (or an off-by-one) instead of NZ local. The `startedAt` sent to `/recordings/upload-url` (and any client-side date-folder logic) must be the **NZ local** date/time of the recording. If the app sends a correct `startedAt` ISO with offset, the server derives the folder from it (`_recording_s3_key` uses `startedAt[:10]`), so fixing `startedAt` to NZ-local fixes the folder.

### G4 — Filename format the pipeline can parse
The app names files `ben_lin_20260716_095000.m4a` (compact, no dashes, lowercase). The pipeline's time extraction (BUG-01 regex) requires `{Prefix}_YYYY-MM-DD_HH-MM-SS.ext` with DASHES, e.g. `Ben_Lin_2026-07-16_09-50-00.wav`. With the compact name, VAD/transcribe cannot parse the timestamp → wrong/empty times, broken deep-links, and broken photo↔topic time-correlation. Emit `fileName` in the dashed `YYYY-MM-DD_HH-MM-SS` form (the `{Prefix}` can be the device/user token). Note: if using G2's presigned flow, the server builds the key from `fileName`, so `fileName` must already be in this format.

### G5 — Audio container/extension: `.m4a` is not ingested; and attribution
Two sub-issues:
- **(a) Format:** the app uploads `.m4a` (AAC). The prod lake's VAD S3-event trigger fires only on suffixes `.wav` and `.mp4` — `.m4a` audio is silently never processed. DECISION (pick one, coordinate with the pipeline owner):
  - App emits `.wav` for audio (simplest downstream; matches the RealPTT path), OR
  - Pipeline adds `.m4a` to the VAD trigger suffixes AND confirms VAD's ffmpeg path extracts AAC-in-M4A (it already handles AAC-in-MP4, so likely a small change) — this is a PIPELINE-side change (`scripts/wire-s3-events.sh` + a redeploy), not app-side.
  Recommendation: app emits `.wav` if feasible; otherwise add `.m4a` pipeline support. Photos (`.jpg`) and video (`.mp4`) are already fine.
- **(b) Attribution:** raw recordings attribute to a site via the RECORDER's single Aurora membership (extraction path has no `report['site']`). The "Ben_Lin" user had ZERO memberships → recordings fail-closed to no-site and never appear under any project. Two clean fixes, not mutually exclusive:
  - Ensure every recording user has the correct site membership(s) in the org (so the fallback resolves), AND/OR
  - Make the extraction/ingest path CONSUME `recordings.site_id` (set via G2's `siteId`) as the authoritative site when present — this is the proper multi-site solution and closes the long-standing recording↔site attribution gap. This is a PIPELINE-side change worth scoping (repositories + lambda_ingest/lambda_item_writer resolve_site: prefer `recordings.site_id` matched by s3_key/clientUuid over the membership fallback).

## Suggested order
1. G1 (point at prod) + G2 (presigned flow) — these two make uploads land in prod, registered, with siteId. Biggest win.
2. G3 + G4 (NZ date + dashed filename) — so the pipeline parses time correctly.
3. G5a (audio → .wav or pipeline .m4a support) — so audio conversations process.
4. G5b (pipeline consumes recordings.site_id) — the durable multi-site attribution fix (separate pipeline task).

## Out of scope here
- The orchestrator RealPTT-path timezone fix (already shipped in the pipeline, PR #70).
- The one-off migration of today's 5 files (already done manually).

## Key IDs (prod)
- org gateway `ys94qy2tk0`; lake `fieldsight-data-509194952652`; Cognito pool `ap-southeast-2_q88pd6XXr` client `4ratjdjonqm17tln6bs2761ci3`; internal company `FieldSight` = `dc2eafa9-1260-4bd9-8d65-862f47dacb3c`; site SB1108 Ellesmere College = `2f6b0776-02bf-425e-bbc1-994c170f11da` (slug `sb1108-ellesmere`).
- Recording S3 key convention: `users/{display_name}/{video|audio|pictures}/{YYYY-MM-DD}/{Prefix}_{YYYY-MM-DD}_{HH-MM-SS}.{ext}`.
