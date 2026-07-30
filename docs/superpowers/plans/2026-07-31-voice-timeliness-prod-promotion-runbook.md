# Voice-Timeliness — Prod Promotion Runbook (2026-07-31)

Operational checklist to promote the voice-timeliness flagship (rolling summary,
≤2-min confirmation email, chunk-driven session lifecycle, structured action
items, web attribution) from TEST to PROD. Every item below was audited live on
2026-07-30/31; ✅ = verified in place, ⚠️ = action required.

## Current state (audited)
- `fieldsight-prod` stack: **UPDATE_COMPLETE** (healthy).
- Prod is **already multi-tenant** (`fieldsight-prod-item-writer` MULTI_TENANT_RESOLUTION=true; deploy-prod.yml hardcodes it). The "empty web timeline" bug was **TEST-only** — prod does not have it.
- All develop work (#155/#158 latency, #160 chunk-site attribution, #162/#165 chunk-driven lifecycle, #163 test multi-tenant) is on **develop**, ready to ride the develop→main PR **#159**.
- Everything ships **inert on prod** behind `EnableFinalize` (default false): the finalize sweep is DISABLED and SessionActivityFunction's trigger is DISABLED until deliberately enabled.

## Already in place ✅
- `PROD_VAD_LAYER_ARN`, `PROD_WIRE_LAKE=true`, `PROD_DOCX_LAYER_ARN`, `PROD_KEYFRAME_FFMPEG_LAYER_ARN`.
- Secrets: `AWS_ROLE_ARN`, `CLAUDE_API_KEY`, `DASHSCOPE_API_KEY`, `ELEVENLABS_API_KEY`, `FARGATE_*`, `PROD_CLOUDFRONT_ID`, `PROD_FRONTEND_BUCKET`, `REALPTT_*`.
- SES sender `sales@fieldsightai.com`: **verified**.
- `EmailSender`=ses, `SenderEmail`=sales@fieldsightai.com (template defaults).

## Actions required ⚠️

### A. Before merging #159 (config decision)
1. **Set repo var `PROD_LLM_PROVIDER=qwen`.** Prod currently defaults to `anthropic`; the whole pipeline (extraction/report/rolling/finalize) was validated on **qwen**, `DASHSCOPE_API_KEY` is present, and the #158 latency fix (enable_thinking:false) is qwen-specific. This switches ALL prod LLM calls to qwen on the next deploy — a deliberate prod change, hence a human decision.
   - `gh variable set PROD_LLM_PROVIDER --body qwen --repo benzn-tech/fieldsight-pipeline`
2. Leave `PROD_ENABLE_FINALIZE` **unset/false** for now (see C).

### B. Merge + deploy (dark launch — finalize stays OFF)
3. Merge **#159** (develop→main). This pushes `main` → triggers `deploy-prod.yml`, which **waits on the `production` environment approval**.
4. **Approve** the `production` gate on the deploy-prod run.
5. Prod deploys with finalize **inert** (SessionActivityFunction present but trigger DISABLED; finalize sweep DISABLED). Verify a prod recording still flows: VAD → transcribe → extract → item_writer writes topics → web timeline shows them (this path is independent of finalize and already prod-multi-tenant).

### C. Turn the confirmation email ON (needs SES out of sandbox)
6. ⚠️ **SES is in SANDBOX** (`ProductionAccessEnabled=False`). In sandbox, SES only delivers to *verified* recipients — real site recorders' inboxes are arbitrary, so finalize emails would be **rejected**. **Request SES production access** (AWS Support → "Request production access", region ap-southeast-2). *(User AWS action — cannot be done from code.)*
7. After production access is granted: **set `PROD_ENABLE_FINALIZE=true`** and **re-run `deploy-prod`** (the sweep's schedule State + SessionActivityFunction's trigger State are set at deploy time, so a var flip needs a redeploy). This enables: the rolling summary already runs; the sweep now claims closed sessions; SessionActivity opens/touches from the chunk stream; inferred idle-close activates.
8. Verify on a real prod recording: stop → confirmation email within ~2 min; and a session left open finalizes by inference after `SESSION_GAP_MINUTES` (15).

### D. Frontend (separate repo)
9. Promote fieldsight-ui dev→main (the Tier-2 Delivery-C review modal, #138) so the web surfaces the session-report UI. (Independent of the pipeline stack.)

## Optional / separate decisions
- `PROD_ASR_PROVIDER` is unset → defaults to `transcribe`; TEST runs `elevenlabs` (scribe_v2). If prod should mirror the validated ASR, set `PROD_ASR_PROVIDER=elevenlabs` (ELEVENLABS_API_KEY is present). Orthogonal to the flagship.
- The new **SessionActivityFunction is an always-on in-VPC lambda** once step 7 enables it (one invoke per transcript, same cadence as the rolling summary). Cost is modest; flagged for awareness.

## Rollback
- Set `PROD_ENABLE_FINALIZE=false` + redeploy → finalize sweep + SessionActivity trigger DISABLED, INFER_IDLE_CLOSE=false. The email/lifecycle go inert; the rest of the pipeline is unaffected.
