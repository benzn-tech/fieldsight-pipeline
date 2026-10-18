# Mobile App ↔ Backend Recording-Upload Contract (2026-07-17)

Source of truth: `src/lambda_org_api.py` — `create_recording_upload_url` (L255),
`complete_recording` (L310), `_recording_s3_key` (L246), `_safe_seg` (L240),
`_KIND_FOLDER` (L83). Attribution consumer: `lambda_item_writer` + G5b
`recordings.site_for_media`.

**Key principle: the app does NOT construct S3 paths. The backend computes the
S3 key server-side and returns a presigned PUT URL. The app "matches" the
backend by sending the right REQUEST FIELDS — above all `siteId` and the user's
own auth — not by building folders.**

---

## Auth (every request)
- All `/api/org/*` routes sit behind the Cognito authorizer.
- Send the **user's own Cognito `idToken`** in the `Authorization` header
  (raw token, no `Bearer ` prefix — see the mobile-auth note).
- The backend resolves `caller` from the token's `sub`. So **whoever's token is
  used becomes the recording's owner** (`recordings.user_id = caller.id`). For
  Neil's recordings to be Neil's, the app must be logged in as Neil.
- Base URL = the prod API gateway the app's prod flavor points at
  (`ys94qy2tk0`), path prefix `/api/org`.

---

## Step 1 — reserve the upload
`POST /api/org/recordings/upload-url`

Request body (JSON):

| field | required | notes |
|---|---|---|
| `kind` | yes | one of `"video"` \| `"audio"` \| `"photo"` |
| `clientUuid` | yes | device-side capture id; **idempotency key** — a resend reuses the same row, never duplicates |
| `fileName` | yes | base file name; becomes the last path segment (sanitized) |
| `contentType` | yes | MIME; **must be reused verbatim as the PUT `Content-Type`** |
| `startedAt` | yes | ISO 8601 (`YYYY-MM-DDThh:mm:ssZ`); its date part `[:10]` = the folder date |
| **`siteId`** | **strongly recommended** | the **site UUID** the user selected (e.g. UC PK). Validated to be in the caller's company (else 403). **Written to `recordings.site_id` — this is THE attribution tag (see below).** Omit → `NULL` → falls back to folder→membership guess |
| `endedAt` | no | ISO 8601 |
| `durationS` | no | int seconds |
| `resolution` | no | e.g. `"1920x1080"` |
| `codec` | no | e.g. `"h264"` |
| `sizeBytes` | no | int |

Response `200`:
```json
{ "recordingId": "<uuid>", "uploadUrl": "<presigned PUT url>", "s3Key": "users/.../file.mp4" }
```

## Step 2 — upload the bytes
`PUT {uploadUrl}` with header `Content-Type: {the same contentType}` and the raw
file body. (Presigned URL; no auth header needed on the PUT itself.)

## Step 3 — finalize
`POST /api/org/recordings/{recordingId}/complete`

Request body (all optional):
| field | notes |
|---|---|
| `sizeBytes` | final uploaded size |
| `gpsTrack` | JSON array of `{t,lat,lon}` points; malformed → silently dropped, never fails |

Response `200`: `{ "ok": true }`. (Lenient by design so a mobile retry never
strands the recording.)

---

## How the backend derives the folder (for your understanding only — app doesn't build it)
```
users/{seg(display_name)}/{video|audio|pictures}/{YYYY-MM-DD}/{seg(fileName)}
```
- `display_name` = the caller's `folder_name` **if set**, else `"{first_name}_{last_name}"`.
- `seg(x)` = replace every char not in `[A-Za-z0-9._-]` with `_` (so a space → `_`).
- `photo` kind maps to the `pictures/` folder; `video`/`audio` map 1:1.
- Example: Neil (folder_name `Neil_Blunden`), a video started 2026-07-20 →
  `users/Neil_Blunden/video/2026-07-20/clip123.mp4`.

## Attribution semantics (what makes it show under UC PK, as Neil's)
1. **Site → UC PK**: driven by `recordings.site_id` (the `siteId` you send).
   G5b (`recordings.site_for_media`) makes the extraction pipeline attribute the
   topic to that site, **overriding any folder/membership guess**. This is the
   robust, explicit path — **send `siteId` = UC PK's UUID and site attribution is
   guaranteed**, regardless of who recorded or their memberships.
2. **Owner → Neil**: `recordings.user_id` = the authenticated caller. Auth as Neil.
3. **Neil sees it on his own Today/Timeline**: that view is self-keyed on the
   login user's `folder_name`. Requires Neil's login `folder_name = "Neil_Blunden"`
   (matches the folder). **This is a one-time BACKEND enrollment step (not the
   app's job)** — currently no product route sets `folder_name`; being added.

## Where the app gets `siteId`
`GET /api/org/sites` (ACL-filtered) → returns the sites the user may see (UC PK
appears once Neil has a UC PK membership). Each row's `id` is the `siteId` to send.
So the app's flow: list sites → user picks UC PK → send that `id` as `siteId` on
every upload.

---

## App checklist to attribute Neil/James → UC PK
- [ ] Log in as the actual user (their Cognito idToken).
- [ ] `GET /api/org/sites`, let the user pick their project → capture `id`.
- [ ] On every `upload-url` call send `siteId` = that id, plus kind/clientUuid/fileName/contentType/startedAt.
- [ ] PUT bytes with the same contentType; then POST `/complete`.
- [ ] (Backend, us) enroll each user's `folder_name` so their own Today/Timeline shows their clips.

The app already implements this shape (app→prod G1–G5a, prod-accepted); this doc
is the exact field/semantics reference to verify against.
