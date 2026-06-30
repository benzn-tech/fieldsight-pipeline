# ASR Providers — credentials, pricing & sign-up

Reference for the engines this benchmark compares: **what credential each needs,
where to get it, what it costs, and whether a credit card / real-name
verification is required to start.**

> Pricing & free-tier terms are as of **June 2026** and change often — treat the
> numbers as a planning guide and confirm on the official page before relying on
> them. Items that could not be verified from a public page are flagged ⚠️.

The app needs **only one** provider configured to run; any key left blank shows
as ⚪ *not configured* and is skipped. For a meaningful "candidate vs incumbent"
comparison the minimum is **Cartesia + AWS** (+ Anthropic *or* a reference
transcript for scoring).

---

## 1. What each engine needs

| Engine (role) | Env var(s) | Uses your S3? | Audio sent as |
|---|---|:--:|---|
| **Cartesia Ink** (candidate) | `CARTESIA_API_KEY` | — | bytes (multipart) |
| **ElevenLabs Scribe** (candidate) | `ELEVENLABS_API_KEY` | — | bytes (multipart) |
| **Plaud** (candidate) | `PLAUD_CLIENT_ID` + `PLAUD_API_KEY` | ✅ presign (or Plaud upload) | audio URL (async) |
| **AWS Transcribe** (incumbent baseline) | `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_TRANSCRIBE_BUCKET` | ✅ stages WAV in S3 | `s3://` URI |
| **Anthropic Claude** (LLM judge — optional) | `ANTHROPIC_API_KEY` | — | n/a (scores text) |
| **Zhipu GLM-ASR** | `ZHIPU_API_KEY` | — | bytes (base64) |
| **Qwen3-ASR-Flash** | `DASHSCOPE_API_KEY` | — | bytes |
| **Ali Fun-ASR** | `DASHSCOPE_API_KEY` (same key as Qwen) | ✅ presigns a GET URL | public URL |

**Why S3?** The app never reads audio *from* S3 — your drag-&-dropped file is the
only input. S3 is just a transit step for the two engines whose APIs refuse a
direct upload: **AWS Transcribe** (batch job reads an `s3://` object) and
**Fun-ASR** (only accepts a public file URL → the adapter uploads the WAV and
hands DashScope a 1-hour presigned URL). Both temp objects live under the
`asr-benchmark/` prefix and are **deleted after each run**. The Anthropic judge
is only invoked when there is **no reference transcript**; with a reference the
app scores WER/CER locally and Anthropic is not needed.

---

## 2. Where to get each key (sign-up portals)

| Provider | Portal | What to grab | Region / notes |
|---|---|---|---|
| **Cartesia** | play.cartesia.ai → API Keys (docs.cartesia.ai) | one bearer key | needs `Cartesia-Version` header (default `2025-04-16`) |
| **ElevenLabs** | elevenlabs.io → API Keys | one key (`xi-api-key` header) | Scribe v2; 90+ langs auto-detect (en+zh); free tier, likely no card |
| **Plaud** | dev.plaud.ai → portal → App Settings → API Keys | `client_id` + `api_key` (api-key **≠** secret) | regional host (US / Japan); `secret_key` only for Plaud's own upload |
| **AWS** | Console → IAM (account `509194952652` exists) | IAM user access key id + secret | keys are global; use `AWS_REGION=ap-southeast-2`. Min policy below. |
| **Anthropic** | console.anthropic.com → API Keys | one key | reuse the existing FieldSight Claude key |
| **Zhipu GLM-ASR** | intl: z.ai · China: open.bigmodel.cn | one key | the two platforms' keys are **not** interchangeable. Limit: wav/mp3, ≤25 MB, **≤30 s** per request |
| **Ali (Qwen + Fun-ASR)** | intl: modelstudio.console.alibabacloud.com · China: bailian.console.aliyun.com | one `DASHSCOPE_API_KEY` powers **both** engines | `DASHSCOPE_REGION=intl` (Singapore) or `cn` (Beijing). Fun-ASR also needs the AWS S3 creds above. |

### Minimal AWS IAM policy (Transcribe + the `asr-benchmark/` prefix)

Run in **AWS CloudShell** (already authenticated), then paste the printed key
pair into the sidebar. The secret is shown **only once**.

```bash
USER=fieldsight-asr-benchmark
cat > /tmp/asr-policy.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "Transcribe", "Effect": "Allow",
      "Action": ["transcribe:StartTranscriptionJob","transcribe:GetTranscriptionJob","transcribe:DeleteTranscriptionJob"],
      "Resource": "*" },
    { "Sid": "S3Staging", "Effect": "Allow",
      "Action": ["s3:PutObject","s3:GetObject","s3:DeleteObject"],
      "Resource": "arn:aws:s3:::fieldsight-data-509194952652/asr-benchmark/*" }
  ]
}
EOF
aws iam create-user --user-name "$USER"
aws iam put-user-policy --user-name "$USER" --policy-name asr-benchmark --policy-document file:///tmp/asr-policy.json
aws iam create-access-key --user-name "$USER" --output table   # copy the secret NOW
```

> You cannot retrieve an existing secret key — AWS shows it only at creation.
> `aws iam list-access-keys --user-name "$USER"` lists the **AccessKeyId** (public
> part) only; if you lost the secret, delete that key and create a new one.

---

## 3. Cost, free tier & payment method

| Engine | Audio price | Free tier | Card / real-name to start |
|---|---|---|---|
| **Cartesia** Ink Whisper | ~**$0.13/hr** (Scale tier) | ✅ free plan $0/mo, 20k credits/mo ≈ **~5.5 h STT/mo**, API included | ❌ **no card** |
| **ElevenLabs** Scribe v2 | ~**$0.40/hr** ($0.0067/min) ⚠️ confirm on /pricing/api | ✅ free plan (10k credits/mo; exact STT minutes ⚠️ unspecified) | ❌ likely no card (⚠️ unverified) |
| **Plaud** (plaud-fast-whisper) | ⚠️ see Plaud developer portal (not public) | ⚠️ check portal | ⚠️ unverified |
| **AWS Transcribe** | ~$0.024/min ≈ **$1.44/hr** (US) | ✅ **60 min/mo for 12 mo** | ⚠️ **card required** to open the account |
| **Anthropic** (judge, optional) | Sonnet 4.6 $3 / $15 per 1M tok; judge calls ≈ cents | ✅ ~**$5** trial credit | ❌ no card to start (phone verify) |
| **Zhipu GLM-ASR** | **¥0.06/min ≈ ¥3.6/hr** (~$0.5/hr, bigmodel.cn) | ⚠️ new-user token grant (whether it covers ASR unverified) | 🔴 bigmodel.cn needs **China real-name + prepaid**; z.ai = email + intl card, no real-name |
| **Qwen3-ASR-Flash** | China ~**¥0.8/hr ≈ $0.12/hr** (¥0.00022/s) | ✅ **10 h free — China region only**; none on intl/Singapore | 🔴 China bailian = real-name; intl Model Studio = intl card |
| **Ali Fun-ASR** | billed by audio token (1 s = 25 tok); exact ¥/tok ⚠️ login-gated. Same-API Paraformer-v2 ref ≈ **¥0.288/hr (~$0.04/hr)** | ⚠️ free quota **Beijing/China-mainland only**, 30–90 days; none on intl | same DashScope account as Qwen + the AWS S3 creds |

### Takeaways

- **Running the benchmark is essentially free** — every engine has a free tier or
  trial that covers a handful of test files. The only hard gate is that opening
  an **AWS account requires a card**.
- **Cost ranking (cheap → expensive):** Fun-ASR `~$0.04/hr` < Qwen3-ASR `~$0.12/hr`
  ≈ Cartesia `~$0.13/hr` < ElevenLabs `~$0.40/hr` < Zhipu `~$0.5/hr` < **AWS
  `~$1.44/hr+` (the incumbent — most expensive)**. Replacing AWS could cut
  per-hour cost by ~3–10×.
- **No card needed:** Cartesia, ElevenLabs (free tier), Anthropic (to start). **Card required:** AWS.
  **China real-name required:** Zhipu bigmodel, Ali China (bailian) — to
  avoid it, use the **international** route (Zhipu via z.ai, Ali via international
  Model Studio with an international card). ⚠️ For Qwen/Fun-ASR the international
  route also **forfeits the China-only free quota** and (for Qwen) is materially
  pricier.

### Not fully verified (official pages block automated fetch / are login-gated)

- AWS Transcribe **Sydney (ap-southeast-2)** per-minute rate (regional premium over US).
- **Zhipu z.ai** (international) ASR per-unit price; whether new-user free grants cover ASR.
- **Fun-ASR** exact ¥/token and **Qwen3-ASR-Flash** international/Singapore rate.

---

## Sources

- Cartesia — <https://www.cartesia.ai/pricing> · <https://docs.cartesia.ai/build-with-cartesia/models/stt>
- AWS — <https://aws.amazon.com/transcribe/pricing/> · <https://aws.amazon.com/free/>
- Anthropic — <https://www.anthropic.com/pricing>
- Zhipu — <https://bigmodel.cn/pricing> · <https://docs.z.ai/guides/audio/glm-asr-2512>
- Alibaba (Qwen + Fun-ASR) — <https://help.aliyun.com/zh/model-studio/model-pricing> · <https://help.aliyun.com/zh/model-studio/recording-file-recognition> · <https://help.aliyun.com/zh/model-studio/new-free-quota>
