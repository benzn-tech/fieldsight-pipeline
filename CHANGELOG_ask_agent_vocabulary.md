# Changelog: Ask Agent + Custom Vocabulary

> Date: 2026-03-23
> Scope: 3 new files, 3 modified files

---

## New Files

### 1. lambda_ask_agent.py (413 lines) — Ask Agent Lambda

Stateless Q&A grounded in report + transcript data. Users can ask questions about any daily report or meeting minutes and get answers sourced from the actual data.

**Architecture:**
```
POST /api/ask
  → API Lambda (permission check + proxy)
    → Ask Agent Lambda
      1. Load report JSON from S3
      2. Load raw transcripts from S3
      3. Normalize via transcript_utils
      4. Build grounded prompt (system context + report + transcripts + question)
      5. Call Claude Haiku → return answer
```

**Key features:**
- **Model:** Claude Haiku 4.5 (cheap, fast — retrieval/summarization only)
- **Scope control:** `"scope": "report"` / `"transcript"` / `"both"` (default)
- **Topic narrowing:** `"topic_id": 2` → auto-filters transcripts to that topic's time range
- **Grounded answers:** System prompt enforces strict grounding — no hallucination
- **Bilingual:** Responds in the language the user asks in (English or 中文)
- **RBAC:** Permission check happens in API Lambda before proxying

**Request example:**
```json
POST /api/ask
{
  "date": "2026-03-20",
  "user": "Jarley_Trainor",
  "question": "What safety issues were raised today?",
  "scope": "both",
  "topic_id": null
}
```

**Response:**
```json
{
  "answer": "Two safety issues were raised: ...",
  "grounded": true,
  "data_sources": {
    "report": true,
    "report_type": "daily",
    "transcript_files": 8
  }
}
```

### 2. custom_vocabulary_construction_nz.txt (129 entries) — AWS Transcribe Custom Vocabulary

TSV file with 4 columns: `Phrase | SoundsLike | IPA | DisplayAs`

**Coverage (129 terms across 7 categories):**

| Category | Examples | Count |
|---|---|---|
| Materials & Components | GIB, dwang, purlin, soffit, fascia, sarking, rebar, lintel, precast, formwork | 32 |
| NZ Building Standards | BRANZ, NZBC, NZS 3604, NZS 4402, E2/AS1, B1/AS1, CCC, H1/H3/H5 | 14 |
| Documents & Processes | PS1, PS4, PIR, PIMs, RFI, EOT, LBP | 10 |
| Commercial Terms | PC sum, provisional sum, variations, defects liability, practical completion | 10 |
| Equipment & Methods | Hiab, Acrow props, scaffold, strongback, falsework, boxing | 12 |
| Safety & Compliance | PPE, SWMS, JSA, toolbox talk, site induction, high-vis, harness | 14 |
| NZ Locations & Product Names | Roskill, Ellesmere, Wanaka, Queenstown, FieldSight, SiteSync | 8 |
| Roles & Trades | site manager, foreman, QS, subcontractor, steelfixer, subbies | 14 |
| Building Elements | cladding, weatherboard, flashing, downpipe, foundations, pile caps | 15 |

---

## Modified Files

### 3. template.yaml (+44 lines → 1054 total)

| Change | Detail |
|---|---|
| New parameter | `CustomVocabularyName` — AWS Transcribe vocabulary name (default: empty = skip) |
| New Lambda | `fieldsight-ask-agent` — Ask Agent, 60s timeout, 256MB, Haiku model |
| New log group | `AskAgentLogGroup` — 14 day retention |
| New output | `AskAgentFunctionArn` |
| API Lambda | Added `ASK_AGENT_FUNCTION` env var + `LambdaInvokePolicy` for ask agent |
| Transcribe Lambda | Added `VOCABULARY_NAME` env var from `CustomVocabularyName` parameter |

### 4. lambda_fieldsight_api.py (+57 lines → 973 total)

| Change | Detail |
|---|---|
| New env var | `ASK_AGENT_FUNCTION` |
| New route | `POST /api/ask` → `ask_question()` |
| New handler | `ask_question(body, caller)` — permission check + sync Lambda invoke to Ask Agent |
| RBAC | Workers forced to own data; management roles can query any accessible user |

### 5. lambda_transcribe.py (+26 lines → 416 total)

| Change | Detail |
|---|---|
| New config | `VOCABULARY_NAME = os.environ.get('VOCABULARY_NAME', '')` |
| `build_transcribe_params()` | Now supports Custom Vocabulary in both modes: |
| | — **IdentifyLanguage mode:** maps vocabulary to each `en-*` language via `LanguageIdSettings` |
| | — **Single LanguageCode mode:** sets `VocabularyName` directly in `Settings` |

---

## Deployment Steps

### 1. Create Custom Vocabulary in AWS Transcribe

```bash
# Upload TSV to S3
aws s3 cp custom_vocabulary_construction_nz.txt \
  s3://fieldsight-data-509194952652/config/custom_vocabulary_construction_nz.txt

# Create vocabulary (takes 5-10 minutes to process)
aws transcribe create-vocabulary \
  --vocabulary-name fieldsight-construction-nz \
  --language-code en-NZ \
  --vocabulary-file-uri s3://fieldsight-data-509194952652/config/custom_vocabulary_construction_nz.txt

# Check status (wait for READY)
aws transcribe get-vocabulary --vocabulary-name fieldsight-construction-nz
```

### 2. Deploy Stack

```bash
sam build
sam deploy --parameter-overrides \
  CustomVocabularyName=fieldsight-construction-nz
```

Or leave `CustomVocabularyName` empty to deploy without vocabulary (can add later without redeployment — just update Lambda env var).

### 3. Test Ask Agent

```bash
# Direct Lambda test
aws lambda invoke \
  --function-name fieldsight-ask-agent \
  --payload '{"date":"2026-03-20","user":"Jarley_Trainor","question":"What was discussed about scaffolding?"}' \
  output.json
cat output.json

# Via API Gateway
curl -X POST https://{api-id}.execute-api.ap-southeast-2.amazonaws.com/prod/api/ask \
  -H "Authorization: Bearer {cognito-token}" \
  -H "Content-Type: application/json" \
  -d '{"date":"2026-03-20","user":"Jarley_Trainor","question":"Any safety issues today?"}'
```

### 4. Test Custom Vocabulary

Re-run transcription on a test file and compare output:
```bash
# Before: check existing transcript for a known term
aws s3 cp s3://fieldsight-data-509194952652/transcripts/Jarley_Trainor/2026-03-20/some_file.json - | python3 -c "import sys,json; print(json.load(sys.stdin)['results']['transcripts'][0]['transcript'][:500])"

# Trigger re-transcription (delete old job first)
aws transcribe delete-transcription-job --transcription-job-name fieldsight_Jarley_Trainor_some_file

# Re-trigger by re-uploading the audio segment
# (S3 event → Transcribe Lambda → new job with vocabulary)
```
