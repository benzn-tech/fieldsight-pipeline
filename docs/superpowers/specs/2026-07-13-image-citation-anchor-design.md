# Image Citation Anchor — Design Spec (ready-to-build, gated on images-in-RAG)

**Date:** 2026-07-13
**Status:** Design parked for later. Approved direction (user: "先写 spec 备着"). **Blocked on a prerequisite decision + work: images must first enter the RAG corpus (`report_chunks`).** Do NOT start the frontend/citation part until that lands.
**Account/region:** 509194952652 / ap-southeast-2, `fieldsight-test` only.

## 1. Goal

Make an Ask citation that is grounded in a PHOTO deep-link to that exact photo — open the source topic, switch to its Photos tab, scroll the photo into view and flash it — exactly mirroring the transcript-line anchor shipped 2026-07-12 (pipeline PR #45 + ui PR #47). "Click the citation → jump to the right image in the right topic," the photo analogue of "jump to the right spoken line."

This spec has TWO layers:
- **Layer A (prerequisite, the real work): get images INTO the RAG corpus** as retrievable chunks. Today `report_chunks` has only `topic` (75) and `transcript_window` (127) chunks — **zero image chunks** (`chunking.py` never touches photos). Until an image is a retrievable chunk, Ask can never cite it, so there is nothing to anchor.
- **Layer B (the anchor, small): once an image chunk can be cited, add the jump.** This is a near-1:1 clone of the transcript anchor and is cheap.

## 2. Current state (verified 2026-07-13)

| Fact | Where |
|---|---|
| RAG corpus = `report_chunks`, chunk_type ∈ {`topic`, `transcript_window`}. NO image chunks. | Data API: `SELECT DISTINCT chunk_type` → topic/transcript_window only; `src/chunking.py` has no photo/image/caption handling |
| Photos in the OPERATIONAL store: `topic_photos (id, topic_id, s3_key, caption_text, created_at)` — separate from RAG | `src/migrations/0003_dashboard_readmodel.sql:37-43` |
| Frontend renders photos from `topic.related_photos` = an array of **filenames** (NOT s3 keys); `photo-grid.js` maps them by index and derives the s3Key via `window.FS.api.media.photoKey({...})` | `fieldsight-ui/scripts/composites/photo-grid.js:4,15,53-58` |
| The transcript anchor (the template to clone): backend adds `time_start` to the citation, frontend `ask-chat.js` appends `&turnTime=`, `timeline.js` threads it to `TranscriptList` + auto-switches to the Transcript tab, `transcript-list.js` scroll+flash the matching row | pipeline `lambda_ask_agent.py` citation dict; ui `ask-chat.js`/`pages/timeline.js`/`composites/transcript-list.js` (PRs #45/#47) |
| Topic detail already has a Photos tab rendering `photo-grid.js` | `timeline.js` topic-detail tab set (Overview/Transcript/Audio/Video/Photos/Ask) |

## 3. Layer A — get images into RAG (the prerequisite decision)

Images can't be embedded by the existing text-embedding pipeline (DashScope `text-embedding-v4`) directly. Two ways in:

### D1 — how an image becomes a chunk
**Option A (recommended): caption-as-chunk.** Each photo becomes a text chunk whose `chunk_text` is a caption/description, embedded by the SAME DashScope text pipeline, with the image's identity in `metadata`. Reuses the entire embed→ingest→search→cite chain with zero new infra; the new chunk is just a third `chunk_type`.
- `chunk_type = 'image'`
- `chunk_text` = caption (optionally prefixed with the topic title for retrieval context, mirroring how `transcript_window`/`topic` chunks include context)
- `metadata` = `{ "photo_filename": <the related_photos filename>, "s3_key": <topic_photos.s3_key>, "topic_seq": <n>, "caption_source": "report"|"generated", ... }`
- Produced by `src/chunking.py` (new `_image_chunks` helper) during `lambda_ingest`, one chunk per topic photo.

**Option B (deferred): true multimodal embeddings.** Embed the image itself with a vision-embedding model (CLIP-like / a DashScope multimodal endpoint) into a separate vector space + a parallel index. Strictly more capable (finds images by visual similarity, not just caption words) but needs a new model, a second embedding column/table, and query-side routing. Out of scope for v1; revisit only if caption-based recall proves insufficient.

**Recommendation: Option A.** It ships on the existing rails and is enough for "find the photo of the cracked slab" when captions are decent.

### D2 — where the caption comes from
Option A needs a text caption per photo. Does the pipeline already have one?
- `topic_photos.caption_text` EXISTS as a column but its population is unverified — check at build time whether the report generator / item-writer fills it.
- The report's `related_photos` are filenames; unclear if the daily_report JSON carries per-photo captions.

**Two sub-options:**
- **D2a — use existing captions** when present (`topic_photos.caption_text` or a report field). Zero AI cost. Risk: sparse/empty captions → useless chunks.
- **D2b — generate captions at ingest** via a vision model (Claude vision, or a DashScope multimodal model) — one caption per photo during chunking, describing the construction content. Robust, but adds a vision call per image at ingest (cost + a non-VPC step, since the image bytes + vision API are internet-side, like the existing embed-report/matcher non-VPC pattern).

**Recommendation: D2a-then-D2b fallback** — use an existing caption if non-empty; else generate one. Store `caption_source` in metadata for auditability. (If existing captions turn out to be universally empty, it's just D2b.)

### D3 — anchor identity (what the citation will carry)
The frontend photo grid keys on the **filename** (`related_photos`), deriving s3Key via `media.photoKey`. So the anchor the citation carries should be the **`photo_filename`** (matches `related_photos` directly; the frontend already knows how to go filename→s3Key). Carry `s3_key` too in metadata for backend/debug, but the frontend match key is the filename. **Recommendation: filename as the anchor.**

## 4. Layer B — the citation anchor (clone of the transcript anchor)

Once `chunk_type='image'` chunks exist and can be retrieved/cited:

**Backend — `src/lambda_ask_agent.py` citation dict** (the same place `time_start` was added): add an `image_ref` field, mirroring `_citation_time_start`:
```python
def _citation_image_ref(c):
    if c.get("chunk_type") != "image":
        return None
    md = c.get("metadata")
    if not isinstance(md, dict):
        return None
    return md.get("photo_filename") or None
# citation dict gains:  "image_ref": _citation_image_ref(c),
```
(Defensive, None for non-image chunks — identical shape to the transcript fix.)

**Frontend:**
- `ask-chat.js` citation `onOpen`: when `c.image_ref` is truthy, append `&photo=` + `encodeURIComponent(c.image_ref)` to the deep-link (alongside topicTitle/site; a citation has at most one of `turnTime`/`photo`).
- `pages/timeline.js`: parse `?photo=` (mirror `?turnTime=` / `?flag=`), thread to the topic detail as `highlightPhoto`, and **auto-switch to the Photos tab** when set (as turnTime auto-switches to Transcript).
- `composites/photo-grid.js`: add a `highlightPhoto` prop; give each cell a ref; in a `useEffect` keyed on `[highlightPhoto, photos]`, find the cell whose filename === `highlightPhoto` (fallback: none → no flash), `scrollIntoView({block:'center'})` + flash via a new `.fs-photo-grid__cell--flash` 3-pulse class, tokens-only, `prefers-reduced-motion` aware — the SAME flash convention as `transcript-list.js` / `safety-flag-row.js` / `topic-card.js`.

**Guard (learned from the transcript work):** `highlightPhoto` must only come from the deep-link auto-select path (never a manual topic open), so a stale `?photo=` can't flash the wrong topic's grid — mirror how `sel.turnTime` is scoped in `timeline.js`.

## 5. Reuse map (this is mostly a clone)

| Transcript anchor (done) | Image anchor (this spec) |
|---|---|
| `_citation_time_start` | `_citation_image_ref` |
| citation `time_start` | citation `image_ref` |
| `&turnTime=` | `&photo=` |
| auto-switch Transcript tab | auto-switch Photos tab |
| `transcript-list.js` row scroll+flash, match by time | `photo-grid.js` cell scroll+flash, match by filename |
| `.fs-transcript-list__row--flash` | `.fs-photo-grid__cell--flash` |

Layer B is ~a day; Layer A (esp. D2b caption generation) is the real effort.

## 6. Phased plan (execute ONLY after D1/D2 are decided and images are ingested)

1. **Decide D1/D2/D3** (product owner) — caption-based (A) + caption source (existing/generated) + filename anchor.
2. **Ingest image chunks** — `chunking.py` `_image_chunks` (one `image` chunk per topic photo, caption text, metadata w/ filename+s3_key+topic_seq); if D2b, a vision-caption step (non-VPC, mirroring embed-report). Backfill existing reports. TDD + live smoke: a photo-grounded Ask returns an `image` citation.
3. **Citation `image_ref`** — backend one-field add + tests (clone of the `time_start` task).
4. **Frontend photo jump** — `&photo=` + timeline threading + photo-grid flash + Photos-tab auto-switch + cache-busters. Browser E2E: Ask a photo question → click image citation → jumps to the photo + flashes.
5. **Tune** — caption quality / retrieval; confirm image citations are distinguishable in the citation list.

## 7. Open questions for the product owner (answer when you start putting images into RAG)
1. **D1**: caption-based chunks (recommended) or multimodal embeddings?
2. **D2**: do your reports already carry photo captions (`topic_photos.caption_text` populated?), or should ingest generate them with a vision model?
3. **Scope**: images from the daily report's `related_photos` only, or also standalone evidence photos (`/evidence`)? (v1: report photos, matching where transcripts/topics already come from.)
4. **Cost/latency**: if generating captions (D2b), a vision call per photo at ingest — acceptable at your photo volume?

## 8. Note
This is a parked design. It is safe to sit unbuilt — nothing depends on it. When images start flowing into RAG, this spec drops straight into the same subagent-driven flow (ingest → citation field → frontend jump), and Layer B reuses the transcript-anchor code almost verbatim.
