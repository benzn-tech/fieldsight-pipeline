# Ask: prod release checklist (orchestration v2, DeepSeek judge, voice two-phase)

**Date:** 2026-10-19. **Scope:** the Ask line only (screen + voice + device). Other lines riding the same develop→main release are listed at the end, but are not checked here.
**Owner actions are marked [owner].** Nothing here runs on prod until the owner merges `develop` → `main` and approves the `production` environment.

## 1. What TEST runs today (the tested setup)

| Setting | TEST value | Where |
|---|---|---|
| `ASK_AGENTIC` | `screen,voice` | repo var `TEST_ASK_AGENTIC` |
| Judge model | `deepseek/deepseek-v4.1-flash` | `TEST_ASK_JUDGE_MODEL` |
| Judge endpoint | `coreweave/fp8`, no provider fallback | `TEST_ASK_JUDGE_PROVIDER` |
| Judge reasoning | `off` | `TEST_ASK_JUDGE_REASONING` |
| Judge fallback model | `google/gemini-3.8-flash` | `TEST_ASK_JUDGE_FALLBACKS` |
| Voice restatement | `true` | `TEST_ASK_VOICE_RESTATE` |
| Voice memory + web offer | `true` | `TEST_ASK_VOICE_CONVO` |
| Voice web search | `true` | `TEST_VOICE_WEB_ENABLED` |
| Screen async turn | `true` | Amplify `dev` `FS_ASK_ASYNC` |
| Screen web offer button | `true` | Amplify `dev` `FS_ASK_WEB_OFFER` |
| Legacy synthesis model | `google/gemini-3.8-flash` | `TEST_QWEN_MODEL_FAST` (unchanged) |

The routing eval passed on this setup: ×3 (434 calls; only pl2 failed, then fixed by #1140, 6/6) and ×1 after the fix (145 calls, GATE PASS; p50 project 4.1 s, general 6.4 s).

## 2. Prod state checked on 2026-10-19

- `main` is at #1116. `develop` is 425 commits ahead and 153 behind (the 153 are merge commits of release branches).
- `ask_orchestrator.py` and the `AskVoiceWorkerFunction` are not on `main`; `fieldsight-prod-ask-voice-worker` does not exist.
- New template resources on develop: `AskVoiceWorkerFunction`, `TemplateDigitiseFunction`, `VoiceLlmFunction`, `VoiceLlmUrlInvokePermission`. All are Lambda functions or Lambda permissions.
- **Deploy-role IAM is not a blocker.** `github-actions-fieldsight-deploy` / `FieldSightDeployPolicy` allows `lambda:*` on `function:fieldsight-*`, IAM role management on `role/fieldsight-*`, and log groups on `/aws/lambda/fieldsight-*`. These patterns are not stack-specific, and the TEST stack created the same resources with the same role.
- New DB migration on develop: `0087_lake_objects.sql` (lake ownership line, not Ask).
- Prod repo vars already set: `PROD_ASK_JUDGE_MODEL/PROVIDER/REASONING/FALLBACKS` (identical to TEST). `PROD_ASK_AGENTIC` is deliberately unset.

## 3. Wiring in place on develop

- #1141: `deploy-prod.yml` passes `AskAgentic` and the four `AskJudge*` parameters from `PROD_` vars, guarded so an unset var keeps the template default (off).
- This PR: `deploy-prod.yml` passes `AskVoiceRestate`, `AskVoiceConvo`, `VoiceWebEnabled` from `PROD_ASK_VOICE_RESTATE`, `PROD_ASK_VOICE_CONVO`, `PROD_VOICE_WEB_ENABLED`, each `|| 'false'`. Unset deploys false, so this PR changes nothing on prod by itself.

## 4. Release order

1. **[owner] Hand-test on dev/TEST** (spec `fieldsight-ui/docs/specs/2026-10-17-ask-answer-first-folds.md` §8, plus GrandTime 0.7.22 on TEST).
2. **[owner] Top up OpenRouter.** Every Ask, report and extraction call goes through it; the balance was $0.37 on 2026-10-19.
3. **[owner] Set the prod flags** (inert until step 4):
   ```
   gh variable set PROD_ASK_AGENTIC       -R benzn-tech/fieldsight-pipeline -b screen,voice
   gh variable set PROD_ASK_VOICE_RESTATE -R benzn-tech/fieldsight-pipeline -b true
   gh variable set PROD_ASK_VOICE_CONVO   -R benzn-tech/fieldsight-pipeline -b true
   gh variable set PROD_VOICE_WEB_ENABLED -R benzn-tech/fieldsight-pipeline -b true
   ```
   To ship the code without the new behaviour, skip this step; everything stays on the legacy path.
4. **[owner] Backend release:** merge `develop` → `main`, approve the `production` deployment.
5. **Verify prod backend** (read-only, Claude can do it): `fieldsight-prod-ask-voice-worker` exists and its env shows the §1 values; one screen turn and one voice turn on a test account (about 6 model calls, stated before running).
6. **[owner] Frontend release, after step 5:** Amplify `main` gets `FS_ASK_ASYNC=true` and `FS_ASK_WEB_OFFER=true`, then merge `fieldsight-ui` `dev` → `main`. The UI must not reach prod before the backend: the async turn routes do not exist on prod until step 4.
7. **[owner] Device release, after step 5:** merge GrandTime #40 (0.7.21/0.7.22, two-phase Ask). A prod APK built before step 4 falls back to the old `/api/ask/voice` route on a 404, so the reverse order is safe but the new behaviour waits for the backend.

## 5. Rollback

- Behaviour: unset `PROD_ASK_AGENTIC` (and the three voice vars) and redeploy prod. The legacy path is unchanged and comes back.
- Judge outage: CoreWeave is pinned with no provider fallback; a 5xx, a 429 or a transport failure moves to `google/gemini-3.8-flash`, then the turn fails.
- UI: set `FS_ASK_ASYNC=false` on Amplify `main` and redeploy; the screen goes back to the synchronous Ask.

## 6. Also in this release, not checked here

`TemplateDigitiseFunction` (flag `TemplateDigitise`, default false; needs manual CORS PUT and lifecycle steps, see that line's notes), `VoiceLlmFunction` (condition `HasVoiceLlm`; its `VoiceLlmBearer` / `VoiceSessionSigningKey` are not passed by `deploy-prod.yml`), migration `0087_lake_objects.sql`, and every other line's commits among the 425.
