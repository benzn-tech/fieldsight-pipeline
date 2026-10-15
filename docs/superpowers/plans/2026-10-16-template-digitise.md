# Template Digitise Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A person uploads a checklist form (Word file, PDF, or photos of its pages) in the Library, gets back a draft checklist template they check and edit beside the source, and saves it as an ordinary template.

**Architecture:** org-api (in-VPC) presigns the upload into the lake, checks it, and writes a request artifact; a new non-VPC `TemplateDigitiseFunction`, S3-triggered on `template_digitise_requests/`, reads DOCX with python-docx and PDF/images with one Gemini call through `llm_utils` (its own model chain), validates the output in code, and writes `template_digitise_results/`; org-api serves the result to a poll; the Library modal uploads, polls, shows a review screen, and saves through the existing `POST /templates`.

**Tech Stack:** Python 3.12 Lambda (python-docx layer, Pillow from the same layer), urllib3 via `llm_utils`, OpenRouter (`google/gemini-3.8-flash`), SAM `template.yaml`, bash wiring scripts; frontend vanilla JS + in-browser React (`React.createElement`), `node --test`.

**Spec:** `docs/superpowers/specs/2026-10-16-template-digitise-design.md` (binding). Spike artifacts: `docs/superpowers/specs/2026-10-16-template-digitise-spike/` (`run_llm.py` prompt, `truth.py`, `score.py`, `scores.json`).

**Repos and worktrees**

- Backend: `C:\Users\camil\Dropbox\fs-template-digitise`, branch `feat/template-digitise` (from `origin/develop`). Tasks 0-5, plus one backend step in Task 7.
- Frontend: tasks 6-9 run in a separate worktree **`C:\Users\camil\Dropbox\fsui-template-digitise`**, created by the executor before Task 6 from `origin/dev` of `C:\Users\camil\Dropbox\fieldsight-ui`:
  `git -C C:/Users/camil/Dropbox/fieldsight-ui fetch origin dev && git -C C:/Users/camil/Dropbox/fieldsight-ui worktree add -b feat/template-digitise C:/Users/camil/Dropbox/fsui-template-digitise origin/dev`.
  The integration branch of that repo is `dev` (not `develop`). It has no CI: its tests run locally with `node --test tests/`.

## Global Constraints

- Accepted uploads: `.pdf .docx .jpg .jpeg .png`; max **20 MB** per file; up to **10** images per request (one per page); one Word file or one PDF per request; no mixing. `.md` and `.tiff` are dropped.
- Vision page limit: **15**.
- Model: env `TEMPLATE_DIGITISE_MODEL`, default `google/gemini-3.8-flash`; **temperature 0**; JSON output; the spike prompt **verbatim** from `run_llm.py`.
- Vision model chain: env `TEMPLATE_DIGITISE_FALLBACK_MODELS`, default **empty**. The worker sets `LLM_FALLBACK_MODELS: ''`. The text `call_llm` is not changed.
- Limits from the code (do not restate numbers, import them): `checklist.MAX_ITEMS` (120), `checklist.MAX_ITEM_CHARS` (300), `report_template.MAX_TITLE_CHARS` (120), `report_template.MAX_SECTIONS` (40), `report_template.MAX_BODY_CHARS` (60000).
- Flag names, exactly: `label_kept`, `near_duplicate_section` (ratio >= 0.85), `split_section`, `truncated`, `split_for_limit`; result-level `too_many_sections`, `too_long` (see ruling R9).
- Result JSON: `template_digitise_results/{company_id}/{id}.json` = `{status: done|unreadable|error, sections: [{title, items: [{text, flags}], flags}], counts: {sections, items}, route: "docx"|"vision", model, pages, error?, reason?}` plus `id, companyId, userId, name, sourceKeys, flags, finishedAt`.
- Prefixes: `template_sources/{company_id}/{32-hex}.{ext}`, `template_digitise_requests/{company_id}/{32-hex}.json`, `template_digitise_results/{company_id}/{32-hex}.json`. All three expire after **90 days**.
- `TEMPLATE_DIGITISE` (org-api env, template Parameter `TemplateDigitise`) defaults to `'false'` everywhere; off -> every `/templates/digitise...` route answers **404**, never 403.
- Save body (frontend): each section `kind: "checklist"`, `title` <= 120, `items` as **plain strings**, `purpose` exactly `Record the inspector's answer to each item from the recording.`; `catch_all` = the UI's `DEFAULT_CATCH_ALL`; `report_type: "custom"`.
- Banner, exactly: `Check the wording against your form — the reader can correct a spelling without saying so.` (The spec's `--` is rendered as an em dash `—` in every user-facing string in this plan.)
- Poll every **3 s**; give up after **3 minutes** with `Taking too long — try again.`; a result older than **7 days** is not served.
- Worker: `Runtime: python3.12` (docx layer is cp312, BUG-24; Globals is 3.11), `Timeout: 900`, `LLM_CHAIN_BUDGET_SECONDS: '840'`, `LLM_HTTP_TIMEOUT` below 900, carries both `LLM_PROVIDER` and `LLM_TEMPERATURE` (pinned by `test_the_temperature_knob_reaches_every_function_that_calls_an_llm`), no `Events` (S3 wired by `scripts/wire-s3-events.sh`, BUG-33).
- Every boolean template Parameter is passed by **both** deploy workflows (pinned by `test_every_boolean_toggle_is_reachable_from_a_repo_variable`).
- Development artifacts (code, comments, commit messages, docs) in English. Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Git: **never `git stash`**, **never `git add -A`** (stage explicit paths), use single-line `Edit` anchors (Windows CRLF).
- Backend test command (used below as `$PYTEST <args>`), from the backend worktree in Git Bash:
  `export UV_LINK_MODE=copy AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=ap-southeast-2; uv run --with-requirements requirements-test.txt python -m pytest <args>`
- Real AWS commands use `--profile fieldsight-deployer --region ap-southeast-2` and `export MSYS_NO_PATHCONV=1` before any `/`-prefixed argument (BUG-28/42). Never print an API key.
- Python run from Git Bash takes **Windows paths** (`C:/Users/...`), never `/c/Users/...`.

## Rulings on the spec (read before Task 0)

R1. **The S3 trigger is not hand-wired on either stage.** `deploy.yml` runs `wire-s3-events.sh ... --apply` on TEST, and `deploy-prod.yml` does the same on prod because `PROD_WIRE_LAKE=true` (verified 2026-10-16). Adding the `fs-template-digitise` entry to the script is what wires both. TEST lifecycle is applied by `deploy.yml` too. **By hand on prod only:** the three lifecycle rules (the script refuses on prod because of foreign rules) and CORS PUT. `scripts/wire-bucket-cors.sh` replaces the whole CORS config and must never be run on prod (it would delete `amplify-read-for-canvas`).
R2. The wiring test forces `TemplateDigitise` into `deploy-prod.yml` as well: `"TemplateDigitise=${{ vars.PROD_TEMPLATE_DIGITISE || 'false' }}"`. Prod stays off until the owner sets the variable.
R3. The vision request is built directly (`model, messages, temperature: 0, max_tokens, response_format`) and handed to `_qwen_attempt`, **not** through `_openrouter_fields`: no `reasoning` field, no headroom, temperature forced to 0 even where `LLM_TEMPERATURE` is unset (prod sets it to `''`). This makes the shipped request the spike's request, so Task 0 measures the thing that ships; retries, the empty-answer check and usage logging are still reused.
R4. PDF page count: there is no PDF parser in any layer, so the 15-page limit is enforced on PDFs by a best-effort byte count of `/Type /Page` objects (0 when the page tree is in compressed object streams = not enforced; the 20 MB cap still bounds it). Images are counted exactly.
R5. Poll scoping is stricter than the spec's "company-scoped": a draft is served only to the person who requested it (or a platform_admin acting in that company). Pending vs 404 is decided by reading the request key, so org-api also needs GetObject **and ListBucket on `template_digitise_requests/*`** (the spec listed ListBucket only on results and sources).
R6. Images are shrunk to a 2000 px long edge and turned upright (EXIF) with `report_photos.shrink` before the call; a 12 MP phone photo would otherwise make a 10-page request tens of MB.
R7. `lake_keys` is not taught the new families (spec). Sources/request/result rows are recorded with an explicit `kind` at request time; the worker's `lake_put` of the result passes `owner_hint={"company_id": ...}` so the manifest drain resolves the owner by hint.
R8. The DOCX section rule is generalised from `truth.py`'s literal `ACTIVITY & REQUIREMENTS` exclusion: a header row (`Yes/No/NA` / `COMPLIES` in cell 2) names a section **unless the next row is a single-cell section row**. Same output on both fixtures, no customer-specific string.
R9. Two result-level flags are added beside the item/section flags: `too_many_sections` (> 40, the spec's "flagged") and `too_long` (the save body would exceed 60,000 chars). Both block Save in the UI until the person merges/removes.
R10. With `TEMPLATE_DIGITISE` off, the modal falls back to today's "create with starting sections" and says so after creating ("nothing was read from your file"); there is no probe route.

## Review Focus

1. **A real phone photo** (12 MP, EXIF-rotated 90°) — expected: sent upright at <= 2000 px, not sideways and not 5 MB of base64 per page. Pinned in Task 0 (`test_a_rotated_phone_photo_is_sent_upright_and_small`).
2. **A Word file whose browser `file.type` is empty** — expected: the content type is derived from the extension on both the upload-url request and the PUT, so S3's signature matches. Pinned in Task 7 (`a Word file with no browser type still uploads with the signed type`).
3. **Save refused by the server after review** (a template with that name already exists, 409) — expected: the person stays on the review screen with every edit intact and the server's message shown. Pinned in Task 9 (`a refused save keeps the draft`).
4. **Poll for someone else's draft, an expired draft, or an id that never existed** — expected: 404, never 500 and never another person's form. Pinned in Task 5 (`test_poll_*`).
5. **S3 delivers an event for a key that is not a request** (a result written into a wired prefix by mistake, or a malformed key) — expected: ignored with a warning, no model call, no write (BUG-13 shape). Pinned in Task 3 (`test_a_key_that_is_not_a_request_is_ignored`).

---

### Task 0: Gate — multi-part call, the vision reader's request, and a live re-score

**Files:**
- Modify: `src/llm_utils.py` (add `call_llm_parts` after `_call_qwen`, before `_status_movable`)
- Create: `src/template_types.py`
- Create: `src/template_vision.py`
- Create: `tests/unit/test_llm_multipart_call.py`
- Create: `tests/unit/test_template_vision.py`
- Create: `tests/fixtures/template_digitise/truth_bicl.json`, `tests/fixtures/template_digitise/truth_penina.json` (copied)
- Create: `docs/superpowers/specs/2026-10-16-template-digitise-spike/gate.json` (scores only)
- Scratch (not committed): `C:/Users/camil/AppData/Local/Temp/claude/C--Users-camil-Dropbox/30a7129b-818e-469b-8fa3-af041f45fcf2/scratchpad/template-spike/gate.py`

**Interfaces:**
- Produces: `llm_utils.call_llm_parts(parts, *, model, max_tokens, force_json=True, temperature=0, fallback_env=None, deadline=None, caller="unknown") -> (text|None, error|None)`
- Produces: `template_types.EXT_TYPES: dict[str,str]`, `IMAGE_EXTS`, `MAX_BYTES`, `MAX_IMAGES`, `MAX_PAGES`, `PDF_ACCEPTED: bool`, `ext_of(name) -> str`, `accepted_exts() -> list[str]`, `source_shape_error(exts: list[str]) -> str|None`
- Produces: `template_vision.PROMPT`, `DEFAULT_MODEL`, `FALLBACK_ENV = "TEMPLATE_DIGITISE_FALLBACK_MODELS"`, `model() -> str`, `pdf_page_count(body: bytes) -> int`, `check(pages) -> str|None` (an input problem, worded for the person), `build_parts(pages) -> list[dict]`, `read(pages, deadline=None) -> (raw_sections|None, error|None)`; `pages` = `[{"body": bytes, "content_type": str, "name": str}]` in page order; `raw_sections` = the model's `[{"title": str, "items": [str]}]`, unvalidated.

- [ ] **Step 1: Copy the ground truth into the repo**

```bash
mkdir -p C:/Users/camil/Dropbox/fs-template-digitise/tests/fixtures/template_digitise
cp "C:/Users/camil/AppData/Local/Temp/claude/C--Users-camil-Dropbox/30a7129b-818e-469b-8fa3-af041f45fcf2/scratchpad/template-spike/truth_bicl.json" \
   "C:/Users/camil/AppData/Local/Temp/claude/C--Users-camil-Dropbox/30a7129b-818e-469b-8fa3-af041f45fcf2/scratchpad/template-spike/truth_penina.json" \
   C:/Users/camil/Dropbox/fs-template-digitise/tests/fixtures/template_digitise/
```

Expected: two files, ~3 KB and ~8 KB, each `{"sections": [{"title", "items": [...]}]}` (47 items / 11 sections, 115 / 19).

- [ ] **Step 2: Write the failing tests for `call_llm_parts`**

`tests/unit/test_llm_multipart_call.py`:

```python
"""The multi-part (image / file) call: its own request, its own model chain.

Spec 2026-10-16 (template digitise) section 3. The vision read must not inherit the
text path's habits: the deploy's LLM_FALLBACK_MODELS names text models, and a text
model handed page images answers from the prompt alone and invents items that no
code check can catch. And the request must be the spike's request (ruling R3), so the
gate in Task 0 measured what ships. HTTP is stubbed at urllib3.PoolManager.request,
as in test_model_fallback_chain.py.
"""
import json

import pytest

lu = pytest.importorskip("llm_utils", reason="requires urllib3 (installed in CI)")

GEM = "google/gemini-3.8-flash"
TEXT_FALLBACK = "openai/gpt-6-luna-pro"
PARTS = [{"type": "text", "text": "read this"},
         {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}]


class _R:
    def __init__(self, status, payload):
        self.status = status
        self.data = json.dumps(payload).encode("utf-8")


def _ok(text='{"sections": []}', model=GEM):
    return _R(200, {"model": model, "choices": [{"message": {"content": text},
                                                  "finish_reason": "stop"}],
                    "usage": {"completion_tokens": 5}})


def _err(status, msg="boom"):
    return _R(status, {"error": {"message": msg}})


@pytest.fixture(autouse=True)
def openrouter(monkeypatch):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "k")
    monkeypatch.setattr(lu, "QWEN_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(lu, "LLM_TEMPERATURE", None)        # prod: unset
    monkeypatch.setattr(lu, "LLM_REASONING_EFFORT", "high")  # the text path's setting
    monkeypatch.setenv("LLM_FALLBACK_MODELS", TEXT_FALLBACK)  # the TEXT chain
    monkeypatch.delenv("TEMPLATE_DIGITISE_FALLBACK_MODELS", raising=False)
    monkeypatch.setenv("LLM_CHAIN_BUDGET_SECONDS", "840")
    monkeypatch.setattr(lu.time, "sleep", lambda s: None)


def _stub(monkeypatch, responses):
    sent = []
    seq = list(responses)

    def fake(self, method, url, body=None, headers=None, timeout=None):
        sent.append(json.loads(body))
        return seq.pop(0)

    monkeypatch.setattr(lu.urllib3.PoolManager, "request", fake)
    return sent


def test_the_parts_go_out_as_one_user_message_exactly_as_given(monkeypatch):
    sent = _stub(monkeypatch, [_ok()])
    text, err = lu.call_llm_parts(PARTS, model=GEM, max_tokens=16000, caller="t")
    assert (text, err) == ('{"sections": []}', None)
    assert sent[0]["model"] == GEM
    assert sent[0]["messages"] == [{"role": "user", "content": PARTS}]


def test_the_request_is_the_spikes_not_the_text_paths(monkeypatch):
    sent = _stub(monkeypatch, [_ok()])
    lu.call_llm_parts(PARTS, model=GEM, max_tokens=16000, caller="t")
    body = sent[0]
    assert body["temperature"] == 0, "temperature 0 even where LLM_TEMPERATURE is unset"
    assert "reasoning" not in body, "LLM_REASONING_EFFORT=high must not ride into a vision read"
    assert body["max_tokens"] == 16000, "no reasoning headroom is added"
    assert body["response_format"] == {"type": "json_object"}


def test_the_text_fallback_chain_is_never_consulted(monkeypatch):
    sent = _stub(monkeypatch, [_err(503)] * lu.MAX_ATTEMPTS)
    text, err = lu.call_llm_parts(PARTS, model=GEM, max_tokens=100, caller="t")
    assert text is None and err
    assert {b["model"] for b in sent} == {GEM}


def test_its_own_chain_is_tried_when_one_is_named(monkeypatch):
    monkeypatch.setenv("TEMPLATE_DIGITISE_FALLBACK_MODELS", "vendor/vision-b")
    sent = _stub(monkeypatch, [_err(503)] * lu.MAX_ATTEMPTS + [_ok("from-b", "vendor/vision-b")])
    text, err = lu.call_llm_parts(PARTS, model=GEM, max_tokens=100,
                                  fallback_env="TEMPLATE_DIGITISE_FALLBACK_MODELS", caller="t")
    assert (text, err) == ("from-b", None)
    assert sent[-1]["model"] == "vendor/vision-b"
    assert sent[-1]["messages"][0]["content"] == PARTS


def test_our_own_bad_request_is_not_moved_to_another_model(monkeypatch):
    monkeypatch.setenv("TEMPLATE_DIGITISE_FALLBACK_MODELS", "vendor/vision-b")
    sent = _stub(monkeypatch, [_err(400, "bad part")])
    text, err = lu.call_llm_parts(PARTS, model=GEM, max_tokens=100,
                                  fallback_env="TEMPLATE_DIGITISE_FALLBACK_MODELS", caller="t")
    assert (text, err) == (None, "bad part")
    assert len(sent) == 1


def test_dashscope_cannot_take_parts_and_nothing_is_sent(monkeypatch):
    monkeypatch.setattr(lu, "QWEN_BASE_URL",
                        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
    sent = _stub(monkeypatch, [])
    text, err = lu.call_llm_parts(PARTS, model=GEM, max_tokens=100, caller="t")
    assert text is None and "OpenRouter" in err
    assert sent == []


def test_the_anthropic_provider_cannot_take_parts(monkeypatch):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "anthropic")
    sent = _stub(monkeypatch, [])
    text, err = lu.call_llm_parts(PARTS, model=GEM, max_tokens=100, caller="t")
    assert text is None and "OpenRouter" in err and sent == []
```

- [ ] **Step 3: Run them to verify they fail**

Run: `$PYTEST tests/unit/test_llm_multipart_call.py -q`
Expected: FAIL with `AttributeError: module 'llm_utils' has no attribute 'call_llm_parts'`.

- [ ] **Step 4: Implement `call_llm_parts`**

In `src/llm_utils.py`, insert directly above `def _status_movable(status):`:

```python
def call_llm_parts(parts, *, model, max_tokens, force_json=True, temperature=0,
                   fallback_env=None, deadline=None, caller="unknown"):
    """One user message made of content parts (text + image_url / file) -> (text, None)
    or (None, error). OpenRouter path only.

    Its OWN model chain: `model`, then each model named in the env var `fallback_env`
    (unset or empty = none). The deploy's LLM_FALLBACK_MODELS is never read here: it
    names TEXT models, and a text model handed page images answers from the prompt alone
    -- it would invent checklist items that no code check can tell from real ones
    (template digitise spec 2026-10-16, section 3).

    The request is built here and not by `_openrouter_fields`: no `reasoning` field, no
    reasoning headroom, and an explicit temperature. That is the request the 2026-10-16
    spike measured; the text path's effort and headroom were never measured on a vision
    read. Retries, the empty-answer check and the LLM_USAGE line come from
    `_qwen_attempt`, unchanged.
    """
    if LLM_PROVIDER != "qwen" or _is_dashscope(QWEN_BASE_URL):
        return None, ("multi-part (image/file) calls need the OpenRouter endpoint: "
                      "LLM_PROVIDER=qwen and a non-DashScope QWEN_BASE_URL")
    if not QWEN_API_KEY:
        logger.error("QWEN_API_KEY / DASHSCOPE_API_KEY not set")
        return None, "QWEN_API_KEY not configured"
    chain = [model]
    if fallback_env:
        for name in (os.environ.get(fallback_env) or "").split(","):
            name = name.strip()
            if name and name not in chain:
                chain.append(name)
    budget = _chain_budget()
    if deadline is not None:
        budget = deadline if budget is None else min(budget, deadline)
    started = time.monotonic()
    failures = []
    for i, name in enumerate(chain):
        remaining = None if budget is None else budget - (time.monotonic() - started)
        if i > 0 and remaining is not None and remaining < MIN_USEFUL_SECONDS:
            failures.append(f"{name} skipped (time budget exhausted)")
            break
        payload = {"model": name, "messages": [{"role": "user", "content": parts}],
                   "temperature": temperature, "max_tokens": max_tokens}
        if force_json:
            payload["response_format"] = {"type": "json_object"}
        text, err, movable = _qwen_attempt(payload, False, remaining, caller)
        if err is None:
            return text, None
        if len(chain) == 1 or not movable:
            return None, err
        failures.append(f"{name} {str(err)[:120]}")
        if i < len(chain) - 1:
            logger.info("LLM_FALLBACK %s", json.dumps(
                {"caller": caller, "from": name, "to": chain[i + 1],
                 "reason": str(err)[:200]}))
    return None, "all models failed: " + "; ".join(failures)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `$PYTEST tests/unit/test_llm_multipart_call.py tests/unit/test_model_fallback_chain.py tests/unit/test_llm_utils.py -q`
Expected: all PASS (the two existing files prove the text path is unchanged).

- [ ] **Step 6: Write the failing tests for `template_types` and `template_vision`**

`tests/unit/test_template_vision.py`:

```python
"""The vision reader's request: the spike's prompt, pages in order, photos upright and small.

Spec 2026-10-16 section 3. HTTP is stubbed at urllib3, never `call_llm_parts` itself.
"""
import base64
import json
from io import BytesIO

import pytest

lu = pytest.importorskip("llm_utils", reason="requires urllib3 (installed in CI)")
import template_types  # noqa: E402
import template_vision  # noqa: E402

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


class _R:
    def __init__(self, status, payload):
        self.status = status
        self.data = json.dumps(payload).encode("utf-8")


def _ok(text):
    return _R(200, {"model": "google/gemini-3.8-flash",
                    "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                    "usage": {"completion_tokens": 5}})


@pytest.fixture(autouse=True)
def openrouter(monkeypatch):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "k")
    monkeypatch.setattr(lu, "QWEN_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(lu.time, "sleep", lambda s: None)
    monkeypatch.delenv("TEMPLATE_DIGITISE_MODEL", raising=False)


def _stub(monkeypatch, responses):
    sent = []
    seq = list(responses)

    def fake(self, method, url, body=None, headers=None, timeout=None):
        sent.append(json.loads(body))
        return seq.pop(0)

    monkeypatch.setattr(lu.urllib3.PoolManager, "request", fake)
    return sent


def _jpeg(w, h, orientation=None):
    img = Image.new("RGB", (w, h), (200, 200, 200))
    buf = BytesIO()
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        img.save(buf, "JPEG", quality=95, exif=exif.tobytes())
    else:
        img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def _decode(part):
    url = part["image_url"]["url"]
    head, data = url.split(",", 1)
    return head, Image.open(BytesIO(base64.b64decode(data)))


def test_the_prompt_is_the_spikes_verbatim():
    import pathlib
    spike = (pathlib.Path(__file__).resolve().parents[2] / "docs" / "superpowers" / "specs"
             / "2026-10-16-template-digitise-spike" / "run_llm.py").read_text(encoding="utf-8")
    assert template_vision.PROMPT in spike


def test_pages_go_after_the_prompt_in_order():
    pages = [{"body": b"\x89PNG small-1", "content_type": "image/png", "name": "p1.png"},
             {"body": b"\x89PNG small-2", "content_type": "image/png", "name": "p2.png"}]
    parts = template_vision.build_parts(pages)
    assert parts[0] == {"type": "text", "text": template_vision.PROMPT}
    assert [p["type"] for p in parts[1:]] == ["image_url", "image_url"]
    # not an image Pillow can read -> sent as given, with its own type
    assert parts[1]["image_url"]["url"] == (
        "data:image/png;base64," + base64.b64encode(b"\x89PNG small-1").decode())


def test_a_rotated_phone_photo_is_sent_upright_and_small():
    # 3000 x 1000 pixels stored sideways (EXIF 6 = rotate 90 CW): upright it is portrait.
    body = _jpeg(3000, 1000, orientation=6)
    parts = template_vision.build_parts(
        [{"body": body, "content_type": "image/jpeg", "name": "IMG_1.jpg"}])
    head, img = _decode(parts[1])
    assert head == "data:image/jpeg;base64"
    assert max(img.size) == template_vision.IMAGE_EDGE
    assert img.size[1] > img.size[0], "still sideways"


def test_a_pdf_goes_as_a_file_part():
    body = b"%PDF-1.7 fake"
    parts = template_vision.build_parts(
        [{"body": body, "content_type": "application/pdf", "name": "form.pdf"}])
    assert parts[1] == {"type": "file", "file": {
        "filename": "form.pdf",
        "file_data": "data:application/pdf;base64," + base64.b64encode(body).decode()}}


def test_the_pdf_page_count_reads_page_objects_not_the_page_tree():
    body = b"%PDF " + b"<< /Type /Pages /Count 2 >> " + b"<< /Type /Page >> " * 2
    assert template_vision.pdf_page_count(body) == 2
    assert template_vision.pdf_page_count(b"%PDF compressed") == 0


def test_more_than_fifteen_pages_is_refused_before_any_call():
    pdf = b"%PDF " + b"<</Type/Page>>" * (template_types.MAX_PAGES + 1)
    why = template_vision.check([{"body": pdf, "content_type": "application/pdf",
                                  "name": "big.pdf"}])
    assert why and "15 pages" in why
    images = [{"body": b"x", "content_type": "image/png", "name": "p.png"}] * 16
    assert "15 pages" in template_vision.check(images)


def test_a_refused_pdf_route_says_so(monkeypatch):
    monkeypatch.setattr(template_types, "PDF_ACCEPTED", False)
    why = template_vision.check([{"body": b"%PDF", "content_type": "application/pdf",
                                  "name": "f.pdf"}])
    assert why and "export the pages as images" in why


def test_read_returns_the_models_sections(monkeypatch):
    sent = _stub(monkeypatch, [_ok('{"sections": [{"title": "General", "items": ["A?"]}]}')])
    raw, err = template_vision.read([{"body": b"x", "content_type": "image/png", "name": "p"}])
    assert err is None
    assert raw == [{"title": "General", "items": ["A?"]}]
    assert sent[0]["model"] == template_vision.DEFAULT_MODEL
    assert sent[0]["temperature"] == 0


def test_the_model_can_be_changed_by_env(monkeypatch):
    monkeypatch.setenv("TEMPLATE_DIGITISE_MODEL", "vendor/other")
    sent = _stub(monkeypatch, [_ok('{"sections": []}')])
    template_vision.read([{"body": b"x", "content_type": "image/png", "name": "p"}])
    assert sent[0]["model"] == "vendor/other"


def test_an_answer_that_is_not_the_json_asked_for_is_retried_once(monkeypatch):
    sent = _stub(monkeypatch, [_ok("Sure! here is the form"),
                               _ok('{"sections": [{"title": "T", "items": ["i"]}]}')])
    raw, err = template_vision.read([{"body": b"x", "content_type": "image/png", "name": "p"}])
    assert err is None and raw[0]["title"] == "T"
    assert len(sent) == 2


def test_two_unusable_answers_are_an_error(monkeypatch):
    _stub(monkeypatch, [_ok("nope"), _ok('{"rows": []}')])
    raw, err = template_vision.read([{"body": b"x", "content_type": "image/png", "name": "p"}])
    assert raw is None and err == template_vision.MSG_BAD_ANSWER


def test_a_vendor_outage_is_reported_in_plain_words(monkeypatch):
    _stub(monkeypatch, [_R(503, {"error": {"message": "down"}})] * lu.MAX_ATTEMPTS)
    raw, err = template_vision.read([{"body": b"x", "content_type": "image/png", "name": "p"}])
    assert raw is None and err == template_vision.MSG_UNAVAILABLE


def test_the_source_shapes():
    assert template_types.source_shape_error(["docx"]) is None
    assert template_types.source_shape_error(["pdf"]) is None
    assert template_types.source_shape_error(["jpg", "png", "jpeg"]) is None
    assert template_types.source_shape_error(["docx", "jpg"])
    assert template_types.source_shape_error(["pdf", "pdf"])
    assert template_types.source_shape_error(["jpg"] * 11)
    assert template_types.source_shape_error([])
    assert template_types.source_shape_error(["md"])
    assert template_types.ext_of("Form.PDF") == "pdf"
    assert template_types.ext_of("noext") == ""
```

- [ ] **Step 7: Run them to verify they fail**

Run: `$PYTEST tests/unit/test_template_vision.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'template_types'`.

- [ ] **Step 8: Implement `template_types.py`**

`src/template_types.py`:

```python
"""What a checklist form may be uploaded as (template digitise spec 2026-10-16).

One list, read by org-api (upload-url, request) and by the worker, so the two cannot
disagree about what is accepted.
"""

EXT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
}
IMAGE_EXTS = ("jpg", "jpeg", "png")
MAX_BYTES = 20 * 1024 * 1024
MAX_IMAGES = 10
MAX_PAGES = 15
# Set by the Task 0 probe of OpenRouter's `file` content part (2026-10-16). False means
# a PDF is refused with "export the pages as images" -- there is no PDF renderer in any
# layer to fall back on.
PDF_ACCEPTED = True

SHAPE_ERROR = "Upload one Word file, one PDF, or up to 10 photos."


def ext_of(name):
    name = (name or "").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def accepted_exts():
    return [e for e in EXT_TYPES if PDF_ACCEPTED or e != "pdf"]


def source_shape_error(exts):
    """None when these extensions make one readable request, else why not."""
    exts = list(exts or [])
    if not exts or any(e not in EXT_TYPES for e in exts):
        return SHAPE_ERROR
    if exts in (["docx"], ["pdf"]):
        return None
    if all(e in IMAGE_EXTS for e in exts) and len(exts) <= MAX_IMAGES:
        return None
    return SHAPE_ERROR
```

- [ ] **Step 9: Implement `template_vision.py`**

`src/template_vision.py` (the `PROMPT` is copied character-for-character from `run_llm.py`'s `PROMPT="""..."""`, including its line breaks):

```python
"""Read a checklist form's pages with a vision model (template digitise spec 2026-10-16).

The uploaded document is DATA to this prompt; everything decided about it is decided in
code (template_draft.clean). The prompt is the 2026-10-16 spike's, verbatim -- it scored
recall 1.00 / wording 0.99-1.00 on both fixture forms and is not to be tuned without
re-running that measurement.
"""
import base64
import logging
import os
import re

import llm_utils
import report_photos
import template_types

logger = logging.getLogger(__name__)

PROMPT = """These page images are a construction site checklist form (one document, pages in order).
Digitise it into a reusable checklist TEMPLATE.
Return STRICT JSON only: {"sections":[{"title":"...","items":["...", "..."]}]}
Rules:
- An item is one checklist row/question that the inspector answers (e.g. with Yes/No/N/A boxes).
- A section is the heading that groups those rows (a section header row or table heading). Keep sections in page order.
- Copy every item's wording EXACTLY as printed (same words, spelling, punctuation, capitalisation, including typos). Join lines that wrap inside one cell with a single space.
- Drop only the leading list label (like "1." or "a)") from titles and items.
- Preserve the original order. Do not invent, merge, split, paraphrase or reorder items.
- Do NOT include: column headers (Yes/No/NA, Comment, Due by...), answers/ticks, filled-in comments, names/dates, signature blocks, page footers, the project details box, or free-text observation registers.
- A table repeated across a page break with the same heading is ONE section."""

DEFAULT_MODEL = "google/gemini-3.8-flash"
FALLBACK_ENV = "TEMPLATE_DIGITISE_FALLBACK_MODELS"
IMAGE_EDGE = 2000       # px, long edge: ruling R6
ANSWER_TOKENS = 16000   # the 115-item form answered in ~2,500

MSG_PDF_REFUSED = "PDF files cannot be read yet — export the pages as images and upload those."
MSG_TOO_MANY_PAGES = "This form has more than 15 pages — upload the checklist pages only."
MSG_BAD_ANSWER = "The reader's answer could not be used — try again."
MSG_UNAVAILABLE = "The reader is not answering right now — try again in a few minutes."

_PAGE_OBJ = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")


def model():
    return os.environ.get("TEMPLATE_DIGITISE_MODEL", "").strip() or DEFAULT_MODEL


def pdf_page_count(body):
    """Best effort (ruling R4): page objects written in the clear. 0 when the page tree
    sits in a compressed object stream -- then the count is unknown, not zero pages."""
    return len(_PAGE_OBJ.findall(body or b""))


def check(pages):
    """An input problem worded for the person, or None. Runs before any model call."""
    pdfs = [p for p in pages if p["content_type"] == "application/pdf"]
    if pdfs and not template_types.PDF_ACCEPTED:
        return MSG_PDF_REFUSED
    count = pdf_page_count(pdfs[0]["body"]) if pdfs else len(pages)
    if count > template_types.MAX_PAGES:
        return MSG_TOO_MANY_PAGES
    return None


def _b64(body):
    return base64.b64encode(body).decode("ascii")


def _image_part(page):
    small = report_photos.shrink(page["body"], IMAGE_EDGE)
    # shrink() hands back the original bytes when it cannot read or improve them, and
    # a re-encoded image is always JPEG.
    mime = page["content_type"] if small is page["body"] else "image/jpeg"
    return {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (mime, _b64(small))}}


def _pdf_part(page):
    return {"type": "file", "file": {
        "filename": page.get("name") or "form.pdf",
        "file_data": "data:application/pdf;base64," + _b64(page["body"])}}


def build_parts(pages):
    parts = [{"type": "text", "text": PROMPT}]
    for page in pages:
        if page["content_type"] == "application/pdf":
            parts.append(_pdf_part(page))
        else:
            parts.append(_image_part(page))
    return parts


def read(pages, deadline=None):
    """(raw_sections, None) or (None, message for the person). One retry when the answer
    is not the JSON asked for; a transport failure is not retried here (the chain in
    llm_utils already has)."""
    parts = build_parts(pages)
    for attempt in range(2):
        text, err = llm_utils.call_llm_parts(
            parts, model=model(), max_tokens=ANSWER_TOKENS, fallback_env=FALLBACK_ENV,
            deadline=deadline, caller="template_digitise")
        if err is not None:
            logger.error("template vision call failed: %s", err)
            return None, MSG_UNAVAILABLE
        data = llm_utils.extract_json(text or "")
        if isinstance(data, dict) and isinstance(data.get("sections"), list):
            return data["sections"], None
        logger.warning("template vision answer was not {sections: [...]} (attempt %d): %.300r",
                       attempt + 1, text)
    return None, MSG_BAD_ANSWER
```

- [ ] **Step 10: Run the unit tests to verify they pass**

Run: `$PYTEST tests/unit/test_template_vision.py tests/unit/test_llm_multipart_call.py -q`
Expected: all PASS.

- [ ] **Step 11: Check OpenRouter's PDF part shape before spending a call**

Read https://openrouter.ai/docs/features/multimodal/pdfs (WebFetch). Confirm the content part is `{"type": "file", "file": {"filename": ..., "file_data": "data:application/pdf;base64,..."}}`. If the documented shape differs, change `_pdf_part` and `test_a_pdf_goes_as_a_file_part` to the documented shape before Step 12, and say so in the commit message.

- [ ] **Step 12: Write the gate harness (scratch, not committed)**

`C:/Users/camil/AppData/Local/Temp/claude/C--Users-camil-Dropbox/30a7129b-818e-469b-8fa3-af041f45fcf2/scratchpad/template-spike/gate.py`:

```python
"""Task 0 gate: the SHIPPED vision read on the spike fixtures. Budget: 4 real calls.

usage: python gate.py penina_photo | bicl_photo | bicl_pdf
"""
import glob, json, os, sys, time

REPO = "C:/Users/camil/Dropbox/fs-template-digitise"
SPIKE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO + "/src")
sys.path.insert(0, SPIKE)
import template_vision  # noqa: E402
from score import score  # noqa: E402  (the spike scorer, unchanged)


def pages_for(doc, variant):
    if variant == "pdf":
        path = f"{SPIKE}/{doc}.pdf"
        return [{"body": open(path, "rb").read(), "content_type": "application/pdf",
                 "name": f"{doc}.pdf"}]
    files = glob.glob(f"{SPIKE}/{doc}_p*_{variant}.*")
    files.sort(key=lambda f: int(os.path.basename(f).split("_p")[1].split("_")[0]))
    return [{"body": open(f, "rb").read(),
             "content_type": "image/png" if f.endswith(".png") else "image/jpeg",
             "name": os.path.basename(f)} for f in files]


out = {}
for name in sys.argv[1:]:
    doc, variant = name.split("_")
    pages = pages_for(doc, variant)
    why = template_vision.check(pages)
    if why:
        print(name, "REFUSED BEFORE CALL:", why); continue
    t = time.time()
    raw, err = template_vision.read(pages)
    dt = round(time.time() - t, 1)
    if err:
        print(name, "ERROR", err, dt); out[name] = {"error": err, "sec": dt}; continue
    json.dump({"sections": raw}, open(f"{SPIKE}/gate_{name}.json", "w", encoding="utf-8"),
              indent=1, ensure_ascii=False)
    truth = json.load(open(f"{SPIKE}/truth_{doc}.json", encoding="utf-8"))
    s = score(truth, {"sections": raw})
    print(name, dt, "s", {k: s[k] for k in ("truth_items", "pred_items", "recall", "exact",
                                             "order", "sec_titles", "item_in_right_section")})
    for e in s["errs"][:12]:
        print("   ", e)
    out[name] = {k: s[k] for k in ("truth_items", "pred_items", "recall", "precision", "exact",
                                    "strict", "order", "sec_titles", "item_in_right_section")}
    out[name]["sec"] = dt
path = f"{SPIKE}/gate_scores.json"
merged = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
merged.update(out)
json.dump(merged, open(path, "w", encoding="utf-8"), indent=1)
```

- [ ] **Step 13: Run the gate — Penina first (the harder form), then BICL**

One command per run, so the key exists only inside that process (never echoed):

```bash
cd C:/Users/camil/Dropbox/fs-template-digitise && export UV_LINK_MODE=copy && \
QWEN_API_KEY="$(aws lambda get-function-configuration --function-name fieldsight-test-extract-session \
  --profile fieldsight-deployer --region ap-southeast-2 --query Environment.Variables.QWEN_API_KEY --output text)" \
LLM_PROVIDER=qwen QWEN_BASE_URL=https://openrouter.ai/api/v1 \
uv run --with-requirements requirements-test.txt python \
  "C:/Users/camil/AppData/Local/Temp/claude/C--Users-camil-Dropbox/30a7129b-818e-469b-8fa3-af041f45fcf2/scratchpad/template-spike/gate.py" penina_photo
```

Then the same command with `bicl_photo`. Count calls: `read()` may spend 2 calls on one run (the JSON retry). **Budget for Steps 13-14 together: 4 calls.** If a run returns a 400 about a request field, that spent one call: fix the field, rerun once.

Expected (spike): penina_photo 115/115, recall 1.0, exact 1.0, order 1.0, `19/19 (pred 19)`; bicl_photo 47/47, recall 1.0, exact 1.0, `11/11 (pred 11)`.

**STOP CONDITION:** if either run scores `recall < 1.0` or `exact < 0.98`, stop here. Do not tune the prompt or the request. Report the two score lines, the `errs` lines and the request you sent to the controller and wait.

- [ ] **Step 14: Probe the PDF route (1 call)**

Same command with `bicl_pdf`.
- 200 with sections: `PDF_ACCEPTED` stays `True`. Record the score (it is not a gate; the photo runs are).
- A 4xx refusing the `file` part (or an answer that plainly ignored the document, e.g. 0 items): set `PDF_ACCEPTED = False` in `src/template_types.py`, change its comment to say why (status + message, date), and run `$PYTEST tests/unit/test_template_vision.py -q` again (the refusal test already covers this path). Tell the controller: Task 9 must then drop `.pdf` from the modal's accepted list.

- [ ] **Step 15: Record the gate result in the spike directory**

Copy the scores (no request bodies, no keys) into the committed spike dir:

```bash
cp "C:/Users/camil/AppData/Local/Temp/claude/C--Users-camil-Dropbox/30a7129b-818e-469b-8fa3-af041f45fcf2/scratchpad/template-spike/gate_scores.json" \
   C:/Users/camil/Dropbox/fs-template-digitise/docs/superpowers/specs/2026-10-16-template-digitise-spike/gate.json
grep -c "sk-or\|Bearer" C:/Users/camil/Dropbox/fs-template-digitise/docs/superpowers/specs/2026-10-16-template-digitise-spike/gate.json
```

Expected: the grep prints `0`.

- [ ] **Step 16: Commit**

```bash
cd C:/Users/camil/Dropbox/fs-template-digitise
git add src/llm_utils.py src/template_types.py src/template_vision.py \
  tests/unit/test_llm_multipart_call.py tests/unit/test_template_vision.py \
  tests/fixtures/template_digitise/truth_bicl.json tests/fixtures/template_digitise/truth_penina.json \
  docs/superpowers/specs/2026-10-16-template-digitise-spike/gate.json
git commit -m "$(cat <<'EOF'
llm_utils: multi-part call with its own model chain; template vision reader

call_llm_parts sends one user message of content parts with the spike's request
(temperature 0, no reasoning field) and never consults the text fallback chain.
template_vision carries the spike prompt verbatim. Gate re-run on both fixture
forms through the real function: see the spike dir's gate.json.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 1: The Word reader (`template_docx`)

**Files:**
- Create: `src/template_docx.py`
- Create: `tests/unit/test_template_docx.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `template_docx.AVAILABLE: bool`; `template_docx.read(source) -> list[{"title": str, "items": [str]}]` where `source` is a path or a binary file-like; `[]` when no checklist table is found. Same raw shape as `template_vision.read`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_template_docx.py`:

```python
"""Reading a checklist out of a Word form -- no model (spec 2026-10-16 section 3).

Synthetic forms mirror the two real layouts measured in the spike:
  BICL   one big table; a column-header row, then single-cell (merged) section rows,
         each followed by item rows whose answer cell reads "Yes No N/A".
  Penina one table per section; the header row's first cell IS the section title
         ("General | Yes/No/NA | Comment | ..."), and filled-in comments sit beside items.
python-docx cannot author w14 checkbox content controls, so the synthetic forms use the
literal answer text; the real-file test covers the content-control path.
"""
import json
import os
import pathlib
from io import BytesIO

import pytest

docx = pytest.importorskip("docx")
import template_docx  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "template_digitise"
REAL = {
    "bicl": os.environ.get("TEMPLATE_DOCX_BICL",
                           "C:/Users/camil/Dropbox/ADE/12.15 - Site Establishment Checklist (BICL).docx"),
    "penina": os.environ.get("TEMPLATE_DOCX_PENINA",
                             "C:/Users/camil/Dropbox/ADE/Penina Site Inspection Checklist.docx"),
}


def _save(d):
    buf = BytesIO()
    d.save(buf)
    buf.seek(0)
    return buf


def _row(table, *cells, merge=False):
    r = table.add_row()
    if merge:
        merged = r.cells[0].merge(r.cells[len(r.cells) - 1])
        merged.text = cells[0]
    else:
        for i, c in enumerate(cells):
            r.cells[i].text = c


def _bicl_like():
    d = docx.Document()
    d.add_paragraph("Site Establishment Checklist")
    details = d.add_table(rows=0, cols=2)
    _row(details, "Project Name", "")
    t = d.add_table(rows=0, cols=4)
    _row(t, "ACTIVITY & REQUIREMENTS", "COMPLIES Y / N / NA", "DUE BY", "COMMENT")
    _row(t, "Site Boundaries", merge=True)
    _row(t, "Site secured", "Yes No N/A", "", "")
    _row(t, "Hoardings established\nwhere appropriate", "\u2610 Yes \u2610 No \u2610 N/A", "", "")
    _row(t, "Emergency Procedures", merge=True)
    _row(t, "Evacuation plan displayed", "Yes No N/A", "", "")
    other = d.add_table(rows=0, cols=1)
    _row(other, "Other Comments")
    return _save(d)


def _penina_like():
    d = docx.Document()
    head = d.add_table(rows=0, cols=2)
    _row(head, "Project: Penina", "Date: 26/1/26")
    for title, items in (("General", ["Site Hazard Identification Board display",
                                      "Is the H&S Policy displayed?"]),
                         ("Site Amenities", ["Is the toilet facility in a clean and sanitary state?"])):
        t = d.add_table(rows=0, cols=5)
        _row(t, title, "Yes/No/NA", "Comment", "Person Responsible", "Target Close Out Date")
        for it in items:
            _row(t, it, "Yes No N/A", "Service twice a week", "", "")
    return _save(d)


def test_THE_bicl_layout_column_header_then_merged_section_rows():
    assert template_docx.read(_bicl_like()) == [
        {"title": "Site Boundaries",
         "items": ["Site secured", "Hoardings established where appropriate"]},
        {"title": "Emergency Procedures", "items": ["Evacuation plan displayed"]},
    ]


def test_the_penina_layout_section_title_in_the_header_row():
    assert template_docx.read(_penina_like()) == [
        {"title": "General", "items": ["Site Hazard Identification Board display",
                                       "Is the H&S Policy displayed?"]},
        {"title": "Site Amenities",
         "items": ["Is the toilet facility in a clean and sanitary state?"]},
    ]


def test_filled_in_answers_and_comments_are_never_read_as_items():
    got = template_docx.read(_penina_like())
    flat = [i for s in got for i in s["items"]]
    assert "Service twice a week" not in flat


def test_a_word_file_with_no_checklist_table_reads_as_empty():
    d = docx.Document()
    d.add_paragraph("Minutes of the site meeting")
    t = d.add_table(rows=0, cols=2)
    _row(t, "Attendees", "Ben, Neil")
    assert template_docx.read(_save(d)) == []


def test_items_before_any_heading_go_in_an_untitled_section():
    d = docx.Document()
    t = d.add_table(rows=0, cols=2)
    _row(t, "Gate locked", "Yes No N/A")
    assert template_docx.read(_save(d)) == [{"title": "", "items": ["Gate locked"]}]


@pytest.mark.parametrize("name,n_sections,n_items", [("bicl", 11, 47), ("penina", 19, 115)])
def test_the_real_forms_read_exactly_as_the_ground_truth(name, n_sections, n_items):
    path = REAL[name]
    if not os.path.exists(path):
        pytest.skip("real form not on this machine: " + path)
    truth = json.load(open(FIXTURES / f"truth_{name}.json", encoding="utf-8"))
    got = template_docx.read(path)
    assert got == truth["sections"]
    assert len(got) == n_sections
    assert sum(len(s["items"]) for s in got) == n_items
```

- [ ] **Step 2: Run them to verify they fail**

Run: `$PYTEST tests/unit/test_template_docx.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'template_docx'`.

- [ ] **Step 3: Implement**

`src/template_docx.py`:

```python
"""Read a checklist out of a Word form -- python-docx, no model (spec 2026-10-16 section 3).

Free, exact and deterministic. The rule (the spike's truth reader, generalised -- ruling R8):
  * an ITEM row is a row any of whose cells after the first is an answer cell: its text
    is "Yes No N/A" once checkbox glyphs are removed, or it holds a w14 checkbox content
    control. The item text is the first cell's text, whitespace-joined.
  * a SECTION is a non-empty single-cell (merged) row, or the first cell of a header row
    (cell 2 reads "Yes/No/NA" or "COMPLIES ...") -- unless the very next row is itself a
    single-cell section row, in which case the header row is only column headings.
  * tables with no item row at all (project details, signatures, comments) are skipped.
The python-docx layer is cp312 (BUG-24); without it AVAILABLE is False and the worker
answers "upload a PDF" instead of raising ImportError.
"""
import logging
import re

logger = logging.getLogger(__name__)

try:
    import docx  # python-docx, from the docx layer
    AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the worker test via monkeypatch
    docx = None
    AVAILABLE = False

_ANSWER = re.compile(r"^yes\s*no\s*n/?a$", re.IGNORECASE)
_HEADER = re.compile(r"(yes\s*/\s*no|complies)", re.IGNORECASE)
_BOXES = re.compile("[\u2610\u2611\u2612\u2713\u2714\u25a1\u25a0]")
_W14_CHECKBOX = "{http://schemas.microsoft.com/office/word/2010/wordml}checkbox"


def _unique_cells(row):
    """A merged cell appears once per grid column it spans; keep it once."""
    out, prev = [], None
    for cell in row.cells:
        if cell._tc is prev:
            continue
        prev = cell._tc
        out.append(cell)
    return out


def _text(cell):
    return " ".join(_BOXES.sub(" ", cell.text).split())


def _is_answer_cell(cell):
    if _ANSWER.match(_text(cell)):
        return True
    return any(el.tag == _W14_CHECKBOX for el in cell._tc.iter())


def _kind(cells):
    """('item'|'header'|'section'|None, first-cell text)."""
    first = _text(cells[0]) if cells else ""
    if len(cells) > 1 and any(_is_answer_cell(c) for c in cells[1:]):
        return "item", first
    if len(cells) > 1 and _HEADER.search(_text(cells[1])):
        return "header", first
    if len(cells) == 1 and first:
        return "section", first
    return None, first


def read(source):
    """[{title, items: [str]}] in document order; [] when no checklist table is found."""
    document = docx.Document(source)
    sections = []
    for table in document.tables:
        rows = [_kind(_unique_cells(r)) for r in table.rows]
        if not any(kind == "item" for kind, _ in rows):
            continue
        for i, (kind, text) in enumerate(rows):
            if kind == "item":
                if not sections:
                    sections.append({"title": "", "items": []})
                sections[-1]["items"].append(text)
            elif kind == "header":
                next_kind = rows[i + 1][0] if i + 1 < len(rows) else None
                if next_kind != "section" and text:
                    sections.append({"title": text, "items": []})
            elif kind == "section":
                sections.append({"title": text, "items": []})
    return [s for s in sections if s["items"]]
```

Note: `truth.py` kept sections with no items in its output only when they were followed by items; the final filter here drops a heading with nothing under it (e.g. a trailing "Comments" row), which the ground truth never contains. If the real-file test shows a difference, print both sides and resolve against the form in Word, not by special-casing a string.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$PYTEST tests/unit/test_template_docx.py -v`
Expected: 5 synthetic tests PASS; the two real-file tests PASS on this machine (the .docx files exist under `C:/Users/camil/Dropbox/ADE/`) and SKIP in CI.

- [ ] **Step 5: Commit**

```bash
git add src/template_docx.py tests/unit/test_template_docx.py
git commit -m "$(cat <<'EOF'
template_docx: read a checklist out of a Word form without a model

Item rows are found by their Yes/No/N/A answer cell (text or w14 checkbox);
sections by merged single-cell rows or the title cell of a header row. Matches the
spike's ground truth exactly on both real forms (47/11 and 115/19).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Validation and review flags in code (`template_draft`)

**Files:**
- Create: `src/template_draft.py`
- Create: `tests/unit/test_template_draft.py`

**Interfaces:**
- Consumes: raw sections `[{"title": str, "items": [str]}]` from `template_docx.read` / `template_vision.read` (anything else in the list is tolerated and dropped).
- Produces: `template_draft.clean(raw) -> {"sections": [{"title": str, "items": [{"text": str, "flags": [str]}], "flags": [str]}], "counts": {"sections": int, "items": int}, "flags": [str]}`; constants `PURPOSE`, `DEFAULT_TITLE = "Checklist"`, `NEAR = 0.85`, `LABEL_RE`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_template_draft.py`:

```python
"""Everything decided about a read form is decided here, in code (spec 2026-10-16 s3).

The model's output is data: a label it left on, a section it split across a page break,
a 400-character "item" -- each is either fixed in code or flagged for the person, never
fixed by asking the model nicely.
"""
import json
import pathlib

import pytest

import checklist
import report_template
import template_draft

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "template_digitise"


def _texts(section):
    return [i["text"] for i in section["items"]]


def test_list_labels_are_stripped_from_titles_and_items():
    out = template_draft.clean([{"title": "8. Electrical",
                                 "items": ["1. Leads tagged", "a) RCDs used", "ii) Cables tidy",
                                           "1.2 Boards closed", "2 workers present"]}])
    s = out["sections"][0]
    assert s["title"] == "Electrical"
    assert _texts(s) == ["Leads tagged", "RCDs used", "Cables tidy", "Boards closed",
                         "2 workers present"]
    assert all(i["flags"] == [] for i in s["items"])


def test_a_label_still_there_after_stripping_is_flagged_not_guessed():
    s = template_draft.clean([{"title": "T", "items": ["1. a) Ladder footed"]}])["sections"][0]
    assert s["items"][0] == {"text": "a) Ladder footed", "flags": ["label_kept"]}


def test_empty_items_and_sections_with_no_items_are_dropped():
    out = template_draft.clean([{"title": "Empty", "items": ["", "   ", None, 7]},
                                {"title": "Real", "items": ["  Gate   locked "]},
                                "not a section"])
    assert [s["title"] for s in out["sections"]] == ["Real"]
    assert _texts(out["sections"][0]) == ["Gate locked"]
    assert out["counts"] == {"sections": 1, "items": 1}


def test_a_missing_title_becomes_checklist():
    out = template_draft.clean([{"title": "", "items": ["Gate locked"]}])
    assert out["sections"][0]["title"] == template_draft.DEFAULT_TITLE


def test_an_item_over_the_limit_is_cut_and_flagged():
    long = "x" * (checklist.MAX_ITEM_CHARS + 1)
    item = template_draft.clean([{"title": "T", "items": [long]}])["sections"][0]["items"][0]
    assert len(item["text"]) == checklist.MAX_ITEM_CHARS
    assert item["flags"] == ["truncated"]


def test_a_title_over_the_limit_is_cut_and_flagged():
    s = template_draft.clean([{"title": "T" * 121, "items": ["i"]}])["sections"][0]
    assert len(s["title"]) == report_template.MAX_TITLE_CHARS
    assert "truncated" in s["flags"]


def test_121_items_split_into_two_sections_each_flagged():
    items = ["Item %d" % n for n in range(checklist.MAX_ITEMS + 1)]
    out = template_draft.clean([{"title": "Big", "items": items}])
    assert [s["title"] for s in out["sections"]] == ["Big (1)", "Big (2)"]
    assert [len(s["items"]) for s in out["sections"]] == [checklist.MAX_ITEMS, 1]
    assert all(s["flags"] == ["split_for_limit"] for s in out["sections"])


def test_near_duplicate_titles_apart_are_flagged():
    out = template_draft.clean([{"title": "Compressed Gasses", "items": ["a"]},
                                {"title": "Ladders", "items": ["b"]},
                                {"title": "Compressed Gases", "items": ["c"]}])
    flags = [s["flags"] for s in out["sections"]]
    assert flags == [["near_duplicate_section"], [], ["near_duplicate_section"]]


def test_the_same_heading_twice_in_a_row_is_a_split_section():
    out = template_draft.clean([{"title": "Electrical", "items": ["a"]},
                                {"title": "Electrical", "items": ["b"]}])
    assert [s["flags"] for s in out["sections"]] == [["split_section"], ["split_section"]]


def test_more_than_forty_sections_is_flagged_for_the_person_to_merge():
    raw = [{"title": "Section %03d xyz" % n, "items": ["i"]}
           for n in range(report_template.MAX_SECTIONS + 1)]
    out = template_draft.clean(raw)
    assert out["counts"]["sections"] == report_template.MAX_SECTIONS + 1
    assert "too_many_sections" in out["flags"]


def test_a_draft_too_long_to_save_is_flagged():
    raw = [{"title": "Part %d" % n, "items": ["%d %s" % (k, "y" * 280) for k in range(120)]}
           for n in range(3)]
    assert "too_long" in template_draft.clean(raw)["flags"]


@pytest.mark.parametrize("name,n_sections,n_items", [("bicl", 11, 47), ("penina", 19, 115)])
def test_the_ground_truth_passes_through_untouched_and_unflagged(name, n_sections, n_items):
    truth = json.load(open(FIXTURES / f"truth_{name}.json", encoding="utf-8"))
    out = template_draft.clean(truth["sections"])
    assert out["counts"] == {"sections": n_sections, "items": n_items}
    assert [s["title"] for s in out["sections"]] == [s["title"] for s in truth["sections"]]
    assert [_texts(s) for s in out["sections"]] == [s["items"] for s in truth["sections"]]
    assert out["flags"] == []
    assert all(s["flags"] == [] for s in out["sections"])
    assert all(i["flags"] == [] for s in out["sections"] for i in s["items"])
```

- [ ] **Step 2: Run them to verify they fail**

Run: `$PYTEST tests/unit/test_template_draft.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'template_draft'`.

- [ ] **Step 3: Implement**

`src/template_draft.py`:

```python
"""Validate a read checklist form and flag what a person must look at (spec 2026-10-16 s3).

Validation lives in code, never in the prompt: the model's output is data. What can be
fixed safely is fixed (a list label, stray whitespace, an empty row); what cannot -- two
sections that may be one table split by a page break -- is FLAGGED for the review screen,
and nothing becomes a template until a person presses Save.
"""
import difflib
import json
import re

import checklist
import report_template

PURPOSE = "Record the inspector's answer to each item from the recording."
DEFAULT_TITLE = "Checklist"
NEAR = 0.85
# "1." "12)" "1.2" "1.2." "a)" "b." "ii)" -- followed by whitespace. "2 workers" is not a label.
LABEL_RE = re.compile(
    r"^\s*(?:\d{1,3}(?:\.\d{1,3})*[.)]|\d{1,3}(?:\.\d{1,3})+|[a-z][.)]|[ivx]{1,5}[.)])\s+",
    re.IGNORECASE)
# What the save body adds beyond the sections themselves (catch_all, keys, purposes are
# counted per section below); generous so the flag errs towards warning.
_BODY_OVERHEAD = 600


def _norm(value):
    return " ".join(value.split()) if isinstance(value, str) else ""


def _strip_label(text):
    return LABEL_RE.sub("", text, count=1)


def _ratio(a, b):
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _item(raw_text):
    text = _strip_label(_norm(raw_text))
    if not text:
        return None
    flags = []
    if LABEL_RE.match(text):
        flags.append("label_kept")
    if len(text) > checklist.MAX_ITEM_CHARS:
        text = text[:checklist.MAX_ITEM_CHARS].rstrip()
        flags.append("truncated")
    return {"text": text, "flags": flags}


def _title(raw_title):
    title = _strip_label(_norm(raw_title))
    flags = []
    if LABEL_RE.match(title):
        flags.append("label_kept")
    if not title:
        title = DEFAULT_TITLE
    if len(title) > report_template.MAX_TITLE_CHARS:
        title = title[:report_template.MAX_TITLE_CHARS].rstrip()
        flags.append("truncated")
    return title, flags


def _flag_similar(sections):
    for i in range(len(sections)):
        for j in range(i + 1, len(sections)):
            a, b = sections[i], sections[j]
            if "split_for_limit" in a["flags"] and "split_for_limit" in b["flags"]:
                continue  # our own split of one long section
            if _ratio(a["title"], b["title"]) < NEAR:
                continue
            flag = "split_section" if j == i + 1 else "near_duplicate_section"
            for s in (a, b):
                if flag not in s["flags"]:
                    s["flags"].append(flag)


def _body_chars(sections):
    body = [{"key": "section-%d" % n, "title": s["title"], "kind": "checklist",
             "purpose": PURPOSE, "items": [i["text"] for i in s["items"]]}
            for n, s in enumerate(sections)]
    return len(json.dumps(body, ensure_ascii=False)) + _BODY_OVERHEAD


def clean(raw):
    sections = []
    for raw_section in raw or []:
        if not isinstance(raw_section, dict):
            continue
        title, section_flags = _title(raw_section.get("title"))
        items = [it for it in (_item(x) for x in (raw_section.get("items") or [])) if it]
        if not items:
            continue
        if len(items) <= checklist.MAX_ITEMS:
            sections.append({"title": title, "items": items, "flags": section_flags})
            continue
        for n, start in enumerate(range(0, len(items), checklist.MAX_ITEMS), 1):
            suffix = " (%d)" % n
            base = title[:report_template.MAX_TITLE_CHARS - len(suffix)].rstrip()
            sections.append({"title": base + suffix,
                             "items": items[start:start + checklist.MAX_ITEMS],
                             "flags": section_flags + ["split_for_limit"]})
    _flag_similar(sections)
    flags = []
    if len(sections) > report_template.MAX_SECTIONS:
        flags.append("too_many_sections")
    if _body_chars(sections) > report_template.MAX_BODY_CHARS:
        flags.append("too_long")
    return {"sections": sections,
            "counts": {"sections": len(sections),
                       "items": sum(len(s["items"]) for s in sections)},
            "flags": flags}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$PYTEST tests/unit/test_template_draft.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/template_draft.py tests/unit/test_template_draft.py
git commit -m "$(cat <<'EOF'
template_draft: validate a read form in code and flag what a person must check

Labels stripped, empty rows dropped, limits from checklist/report_template applied
by cutting or splitting with a flag; near-duplicate and split sections flagged for
the review screen. Both ground-truth forms pass through unchanged and unflagged.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: The worker Lambda (`lambda_template_digitise`)

**Files:**
- Create: `src/lambda_template_digitise.py`
- Create: `tests/unit/test_template_digitise_worker.py`

**Interfaces:**
- Consumes: `template_types.ext_of/EXT_TYPES/source_shape_error/SHAPE_ERROR`; `template_docx.AVAILABLE/read`; `template_vision.check/read/model`; `template_draft.clean`; `lake_put.lake_put`.
- Produces: `lambda_template_digitise.REQUEST_RE`, `result_key(company_id, request_id) -> str`, `digitise(request, deadline=None) -> dict` (the result JSON minus nothing), `lambda_handler(event, context)`. Request JSON shape (written by Task 5): `{"id": 32-hex, "companyId": str, "userId": str, "sourceKeys": [str], "name": str, "requestedAt": iso}`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_template_digitise_worker.py`:

```python
"""The worker, driven from an S3 event to the result JSON (spec 2026-10-16 s3).

The model call is stubbed at the HTTP boundary (urllib3), never the whole function, so
the request the worker actually builds is what these tests see.
"""
import json
from io import BytesIO

import pytest

lu = pytest.importorskip("llm_utils", reason="requires urllib3 (installed in CI)")
docx = pytest.importorskip("docx")
import lambda_template_digitise as worker  # noqa: E402
import template_docx  # noqa: E402

COMPANY = "c0000000-0000-0000-0000-000000000001"
RID = "a" * 32
REQ_KEY = f"template_digitise_requests/{COMPANY}/{RID}.json"
RES_KEY = f"template_digitise_results/{COMPANY}/{RID}.json"


class _R:
    def __init__(self, status, payload):
        self.status = status
        self.data = json.dumps(payload).encode("utf-8")


def _ok(text):
    return _R(200, {"model": "google/gemini-3.8-flash",
                    "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                    "usage": {"completion_tokens": 5}})


class _S3:
    def __init__(self):
        self.store, self.puts, self.gets = {}, {}, []

    def get_object(self, Bucket, Key):
        self.gets.append(Key)
        return {"Body": BytesIO(self.store[Key])}

    def put_object(self, Bucket, Key, Body, **kw):
        self.puts[Key] = Body
        return {}


@pytest.fixture
def s3(monkeypatch):
    fake = _S3()
    monkeypatch.setattr(worker, "S3_BUCKET", "lake")
    monkeypatch.setattr(worker, "s3", lambda: fake)
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "k")
    monkeypatch.setattr(lu, "QWEN_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(lu.time, "sleep", lambda s: None)
    return fake


def _http(monkeypatch, responses):
    sent, seq = [], list(responses)

    def fake(self, method, url, body=None, headers=None, timeout=None):
        sent.append(json.loads(body))
        return seq.pop(0)

    monkeypatch.setattr(lu.urllib3.PoolManager, "request", fake)
    return sent


def _request(s3, keys, **over):
    req = {"id": RID, "companyId": COMPANY, "userId": "u-1", "sourceKeys": keys,
           "name": "Site inspection", "requestedAt": "2026-10-16T00:00:00+00:00"}
    req.update(over)
    s3.store[REQ_KEY] = json.dumps(req).encode()


def _event(key=REQ_KEY):
    return {"Records": [{"s3": {"bucket": {"name": "lake"}, "object": {"key": key}}}]}


def _result(s3):
    return json.loads(s3.puts[RES_KEY])


def _docx_bytes():
    d = docx.Document()
    t = d.add_table(rows=0, cols=2)
    r = t.add_row(); r.cells[0].text = "General"; r.cells[1].text = "Yes/No/NA"
    r = t.add_row(); r.cells[0].text = "Gate locked"; r.cells[1].text = "Yes No N/A"
    buf = BytesIO(); d.save(buf)
    return buf.getvalue()


def test_THE_photos_become_a_draft_result(s3, monkeypatch):
    keys = [f"template_sources/{COMPANY}/{'b' * 32}.jpg", f"template_sources/{COMPANY}/{'c' * 32}.jpg"]
    for k in keys:
        s3.store[k] = b"\xff\xd8 not really a jpeg"
    _request(s3, keys)
    sent = _http(monkeypatch, [_ok(json.dumps({"sections": [
        {"title": "1. General", "items": ["a) Gate locked", "Signage up"]},
        {"title": "General", "items": ["Lights on"]}]}))])

    worker.lambda_handler(_event(), None)

    res = _result(s3)
    assert res["status"] == "done"
    assert res["route"] == "vision" and res["model"] == "google/gemini-3.8-flash"
    assert res["pages"] == 2
    assert res["counts"] == {"sections": 2, "items": 3}
    assert res["sections"][0]["title"] == "General"
    assert res["sections"][0]["items"][0] == {"text": "Gate locked", "flags": []}
    assert res["sections"][0]["flags"] == ["split_section"]
    assert (res["id"], res["companyId"], res["userId"], res["sourceKeys"]) == (
        RID, COMPANY, "u-1", keys)
    # the request that went out: prompt first, then both pages, in order
    content = sent[0]["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert [p["type"] for p in content[1:]] == ["image_url", "image_url"]


def test_a_word_file_is_read_without_any_model_call(s3, monkeypatch):
    key = f"template_sources/{COMPANY}/{'d' * 32}.docx"
    s3.store[key] = _docx_bytes()
    _request(s3, [key])
    sent = _http(monkeypatch, [])
    worker.lambda_handler(_event(), None)
    res = _result(s3)
    assert res["status"] == "done" and res["route"] == "docx" and res["model"] is None
    assert res["sections"][0]["items"] == [{"text": "Gate locked", "flags": []}]
    assert sent == []


def test_without_the_docx_layer_a_word_file_is_an_error_not_an_importerror(s3, monkeypatch):
    key = f"template_sources/{COMPANY}/{'d' * 32}.docx"
    s3.store[key] = b"PK"
    _request(s3, [key])
    monkeypatch.setattr(template_docx, "AVAILABLE", False)
    worker.lambda_handler(_event(), None)
    res = _result(s3)
    assert res["status"] == "error" and res["error"] == worker.MSG_WORD_DISABLED


def test_a_word_file_with_no_checklist_table_is_unreadable(s3):
    d = docx.Document(); d.add_paragraph("Just prose")
    buf = BytesIO(); d.save(buf)
    key = f"template_sources/{COMPANY}/{'d' * 32}.docx"
    s3.store[key] = buf.getvalue()
    _request(s3, [key])
    worker.lambda_handler(_event(), None)
    res = _result(s3)
    assert res["status"] == "unreadable" and res["reason"] == worker.MSG_WORD_NO_TABLE


def test_zero_items_from_the_model_is_unreadable(s3, monkeypatch):
    key = f"template_sources/{COMPANY}/{'b' * 32}.png"
    s3.store[key] = b"png"
    _request(s3, [key])
    _http(monkeypatch, [_ok('{"sections": [{"title": "Photo", "items": []}]}')])
    worker.lambda_handler(_event(), None)
    res = _result(s3)
    assert res["status"] == "unreadable" and res["reason"] == worker.MSG_NO_ROWS


def test_a_source_outside_the_requests_company_is_never_read(s3, monkeypatch):
    other = "c0000000-0000-0000-0000-000000000002"
    key = f"template_sources/{other}/{'b' * 32}.png"
    s3.store[key] = b"png"
    _request(s3, [key])
    sent = _http(monkeypatch, [])
    worker.lambda_handler(_event(), None)
    assert key not in s3.gets and sent == []
    assert _result(s3)["status"] == "error"


def test_a_mixed_request_is_refused_before_any_read(s3, monkeypatch):
    keys = [f"template_sources/{COMPANY}/{'d' * 32}.docx", f"template_sources/{COMPANY}/{'b' * 32}.png"]
    _request(s3, keys)
    sent = _http(monkeypatch, [])
    worker.lambda_handler(_event(), None)
    res = _result(s3)
    assert res["status"] == "error" and res["error"] == worker.SHAPE_ERROR
    assert sent == []


def test_a_key_that_is_not_a_request_is_ignored(s3, monkeypatch):
    sent = _http(monkeypatch, [])
    worker.lambda_handler(_event(RES_KEY), None)
    worker.lambda_handler(_event("template_digitise_requests/not-a-company/x.json"), None)
    assert s3.puts == {} and s3.gets == [] and sent == []


def test_an_unexpected_failure_still_writes_an_error_result(s3, monkeypatch):
    key = f"template_sources/{COMPANY}/{'b' * 32}.png"
    _request(s3, [key])                       # the source object is missing -> KeyError
    worker.lambda_handler(_event(), None)
    res = _result(s3)
    assert res["status"] == "error" and res["error"] == worker.MSG_FAILED


def test_the_result_carries_the_owner_hint_for_the_lake_index(s3, monkeypatch):
    key = f"template_sources/{COMPANY}/{'b' * 32}.png"
    s3.store[key] = b"png"
    _request(s3, [key])
    _http(monkeypatch, [_ok('{"sections": [{"title": "T", "items": ["i"]}]}')])
    worker.lambda_handler(_event(), None)
    lines = [json.loads(v) for k, v in s3.puts.items() if k.startswith("lake_manifest/")]
    assert any(l["key"] == RES_KEY and l["hint"].get("company_id") == COMPANY for l in lines)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `$PYTEST tests/unit/test_template_digitise_worker.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'lambda_template_digitise'`.

- [ ] **Step 3: Implement**

`src/lambda_template_digitise.py`:

```python
"""lambda_template_digitise.py -- read a checklist form into a draft template.

Non-VPC (it calls OpenRouter; org-api is in-VPC with no outbound, BUG-36). Triggered by
org-api writing template_digitise_requests/{company_id}/{id}.json (wired by
scripts/wire-s3-events.sh, BUG-33). Reads the sources under template_sources/{company_id}/,
routes a Word file to python-docx and a PDF or photos to one vision call, validates in
code (template_draft), and writes template_digitise_results/{company_id}/{id}.json for the
poll. Writes a result for EVERY request it accepts, including failures: an async invoke
that raises is retried twice by Lambda, which would spend two more model calls on a form
that already failed.

Design: docs/superpowers/specs/2026-10-16-template-digitise-design.md section 3.
"""
import json
import logging
import os
import re
from datetime import datetime, timezone
from io import BytesIO
from urllib.parse import unquote_plus

import boto3

import template_docx
import template_draft
import template_types
import template_vision
from lake_put import lake_put

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
REQUEST_RE = re.compile(
    r"^template_digitise_requests/(?P<company>[0-9a-fA-F-]{36})/(?P<id>[0-9a-f]{32})\.json$")
SHAPE_ERROR = template_types.SHAPE_ERROR
MSG_WORD_DISABLED = "Word files cannot be read on this deployment — upload a PDF."
MSG_WORD_NO_TABLE = ("No checklist table found in the Word file — upload a PDF export "
                     "of it instead.")
MSG_NO_ROWS = "No checklist rows found — is this a checklist form?"
MSG_FAILED = "Something went wrong reading the form — try again."
# Leave the runtime this much after the model call for the result write.
WRITE_RESERVE_SECONDS = 30

logger.info("docx reader: %s", "enabled" if template_docx.AVAILABLE else "DISABLED")

_s3_client = None


def s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def result_key(company_id, request_id):
    return f"template_digitise_results/{company_id}/{request_id}.json"


def _empty(status, **extra):
    return {"status": status, "sections": [], "counts": {"sections": 0, "items": 0},
            "flags": [], "route": None, "model": None, "pages": None, **extra}


def _read(key):
    return s3().get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()


def digitise(request, deadline=None):
    company = request["companyId"]
    keys = list(request.get("sourceKeys") or [])
    prefix = f"template_sources/{company}/"
    if not keys or any(not isinstance(k, str) or not k.startswith(prefix) for k in keys):
        logger.error("template digitise %s: a source is outside %s -- refused", request["id"], prefix)
        return _empty("error", error=SHAPE_ERROR)
    exts = [template_types.ext_of(k) for k in keys]
    if template_types.source_shape_error(exts):
        return _empty("error", error=SHAPE_ERROR)

    if exts == ["docx"]:
        if not template_docx.AVAILABLE:
            return _empty("error", route="docx", error=MSG_WORD_DISABLED)
        raw = template_docx.read(BytesIO(_read(keys[0])))
        draft = template_draft.clean(raw)
        if draft["counts"]["items"] == 0:
            return _empty("unreadable", route="docx", reason=MSG_WORD_NO_TABLE)
        return {"status": "done", **draft, "route": "docx", "model": None, "pages": None}

    pages = [{"body": _read(k), "content_type": template_types.EXT_TYPES[e],
              "name": k.rsplit("/", 1)[-1]} for k, e in zip(keys, exts)]
    why = template_vision.check(pages)
    if why:
        return _empty("unreadable", route="vision", reason=why)
    page_count = (template_vision.pdf_page_count(pages[0]["body"]) or None
                  if exts == ["pdf"] else len(pages))
    raw, err = template_vision.read(pages, deadline=deadline)
    if err:
        return _empty("error", route="vision", model=template_vision.model(), error=err)
    draft = template_draft.clean(raw)
    if draft["counts"]["items"] == 0:
        return _empty("unreadable", route="vision", model=template_vision.model(),
                      pages=page_count, reason=MSG_NO_ROWS)
    return {"status": "done", **draft, "route": "vision", "model": template_vision.model(),
            "pages": page_count}


def _deadline(context):
    if context is None:
        return None
    return max(1.0, context.get_remaining_time_in_millis() / 1000.0 - WRITE_RESERVE_SECONDS)


def _process(key, context):
    m = REQUEST_RE.match(key)
    if not m:
        logger.warning("template digitise: ignoring %s (not a request key)", key)
        return
    company, request_id = m.group("company"), m.group("id")
    request = json.loads(_read(key).decode("utf-8"))
    if str(request.get("companyId")) != company or request.get("id") != request_id:
        logger.error("template digitise: %s does not match its own key -- ignored", key)
        return
    try:
        result = digitise(request, deadline=_deadline(context))
    except Exception:  # noqa: BLE001 - the person must get an answer, not a pending poll
        logger.exception("template digitise %s failed", request_id)
        result = _empty("error", error=MSG_FAILED)
    result.update({"id": request_id, "companyId": company, "userId": request.get("userId"),
                   "name": request.get("name"), "sourceKeys": request.get("sourceKeys") or [],
                   "finishedAt": datetime.now(timezone.utc).isoformat()})
    lake_put(S3_BUCKET, result_key(company, request_id), json.dumps(result, ensure_ascii=False),
             owner_hint={"company_id": company}, client=s3(), ContentType="application/json")
    logger.info("template digitise %s: %s route=%s sections=%s items=%s", request_id,
                result["status"], result.get("route"), result["counts"]["sections"],
                result["counts"]["items"])


def lambda_handler(event, context):
    for rec in event.get("Records", []):
        key = ((rec.get("s3") or {}).get("object") or {}).get("key")
        if key:
            _process(unquote_plus(key), context)
    return {"ok": True}
```

Note on `test_a_source_outside_the_requests_company_is_never_read`: the guard returns before any `_read`; the result is written to the request's own company prefix.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$PYTEST tests/unit/test_template_digitise_worker.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the neighbours that scan every handler**

Run: `$PYTEST tests/unit/test_every_call_llm_site_is_tagged.py tests/unit/test_lake_put.py -q`
Expected: PASS. (`test_the_caller_map_covers_every_handler_that_imports_a_lake_put_module` is made to pass in Task 4, when the function exists in the template.)

- [ ] **Step 6: Commit**

```bash
git add src/lambda_template_digitise.py tests/unit/test_template_digitise_worker.py
git commit -m "$(cat <<'EOF'
lambda_template_digitise: read a form from its request artifact into a draft result

Word files go to template_docx, PDFs and photos to one vision call; the draft is
validated by template_draft. Every accepted request gets a result, failures
included, so a poll never waits on a run that died.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Worker infrastructure — function, IAM, trigger, lifecycle

**Files:**
- Modify: `src/template.yaml` (new resource above the `# Lambda 10: Org API (Phase 3)` comment block)
- Modify: `scripts/wire-s3-events.sh` (ARN line + an entry after the session-report entry)
- Modify: `scripts/wire-bucket-lifecycle.sh` (exclusion query + three rules)
- Modify: `tests/unit/test_lake_ownership_template.py` (`LAKE_PUT_CALLERS`)
- Create: `tests/unit/test_template_digitise_wiring.py`

**Interfaces:**
- Consumes: handler `lambda_template_digitise.lambda_handler` (Task 3).
- Produces: CFN resource `TemplateDigitiseFunction` named `${Prefix}-template-digitise`; S3 notification Id `fs-template-digitise` on prefix `template_digitise_requests/` suffix `.json`; lifecycle rule IDs `template-sources-expiry`, `template-digitise-requests-expiry`, `template-digitise-results-expiry`.

- [ ] **Step 1: Write the failing wiring tests**

`tests/unit/test_template_digitise_wiring.py`:

```python
"""The digitise worker is wired end to end, or it is not wired (spec 2026-10-16, Rollout).

A function with NO Events whose wire-s3-events.sh entry is missing deploys green and never
fires; a trigger without a read grant fires into AccessDenied; a lifecycle rule missing
from the script's exclusion list makes the script refuse on its second run. Each of those
has happened in this repo once already.
"""
import io
import json
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATE = os.path.join(REPO, "src", "template.yaml")
WIRE = os.path.join(REPO, "scripts", "wire-s3-events.sh")
LIFECYCLE = os.path.join(REPO, "scripts", "wire-bucket-lifecycle.sh")

_yaml = pytest.importorskip("yaml")


class _Loader(_yaml.SafeLoader):
    pass


for _tag in ("!Sub", "!Ref", "!If", "!Not", "!Equals", "!GetAtt", "!FindInMap",
             "!Join", "!Condition", "!Select", "!Split", "!ImportValue", "!And", "!Or"):
    _Loader.add_constructor(_tag, lambda loader, node: getattr(node, "value", None))


def _resources():
    return _yaml.load(io.open(TEMPLATE, encoding="utf-8").read(), Loader=_Loader)["Resources"]


def _fn(name="TemplateDigitiseFunction"):
    return _resources()[name]["Properties"]


def _as_list(v):
    return v if isinstance(v, list) else [v]


def _statements(fn):
    for policy in fn.get("Policies", []):
        if not isinstance(policy, dict):
            continue
        for st in _as_list(policy.get("Statement", [])):
            for s in (st if isinstance(st, list) else [st]):
                if isinstance(s, dict) and s.get("Effect") == "Allow":
                    yield s


def _grants(fn, bucket="IngestBucketName"):
    out = set()
    marker = "${%s}/" % bucket
    for st in _statements(fn):
        for action in _as_list(st.get("Action")):
            for res in _as_list(st.get("Resource")):
                if isinstance(res, str) and marker in res:
                    out.add((action, res.split(marker, 1)[1]))
    return out


def test_THE_the_worker_exists_outside_the_vpc_on_the_docx_runtime():
    fn = _fn()
    assert fn["Handler"] == "lambda_template_digitise.lambda_handler"
    assert fn["Runtime"] == "python3.12", "the docx layer is cp312 (BUG-24)"
    assert fn["Timeout"] == 900
    assert "VpcConfig" not in fn, "it calls OpenRouter; in the VPC it would hang (BUG-36)"
    assert "Events" not in fn, "S3 events on the lake are wired by script (BUG-33)"
    assert "DocxLayerArn" in json.dumps(fn["Layers"], default=str)


def test_the_worker_reads_no_text_fallback_chain_and_has_its_own_empty_one():
    env = _fn()["Environment"]["Variables"]
    assert env["LLM_FALLBACK_MODELS"] == "", "Globals' text chain must not leak in"
    assert env["TEMPLATE_DIGITISE_FALLBACK_MODELS"] == ""
    assert env["TEMPLATE_DIGITISE_MODEL"] == "google/gemini-3.8-flash"
    assert env["LLM_CHAIN_BUDGET_SECONDS"] == "840"
    assert int(env["LLM_HTTP_TIMEOUT"]) < 900
    assert "QWEN_API_KEY" in env and "QWEN_BASE_URL" in env


def test_the_worker_may_read_its_inputs_and_write_only_its_result():
    g = _grants(_fn())
    assert ("s3:GetObject", "template_sources/*") in g
    assert ("s3:GetObject", "template_digitise_requests/*") in g
    assert ("s3:PutObject", "template_digitise_results/*") in g
    assert ("s3:PutObject", "lake_manifest/*") in g
    assert ("s3:PutObject", "template_sources/*") not in g
    assert ("s3:PutObject", "template_digitise_requests/*") not in g


def test_the_trigger_is_in_the_wiring_script():
    text = open(WIRE, encoding="utf-8").read()
    assert '"Id":"fs-template-digitise"' in text
    assert '{"Name":"prefix","Value":"template_digitise_requests/"}' in text
    assert "${PREFIX}-template-digitise" in text


def test_the_trigger_prefix_collides_with_no_other_rule():
    text = open(WIRE, encoding="utf-8").read()
    prefixes = re.findall(r'"Name":"prefix","Value":"([^"]+)"', text)
    mine = "template_digitise_requests/"
    for p in [p for p in prefixes if p != mine]:
        assert not (p.startswith(mine) or mine.startswith(p)), f"{mine} overlaps {p}"


def test_no_rule_fires_on_the_workers_own_output_or_the_uploads():
    text = open(WIRE, encoding="utf-8").read()
    prefixes = re.findall(r'"Name":"prefix","Value":"([^"]+)"', text)
    for out in ("template_digitise_results/", "template_sources/"):
        assert not any(out.startswith(p) or p.startswith(out) for p in prefixes), out


def test_the_three_prefixes_expire_after_ninety_days_and_the_script_still_reruns():
    script = open(LIFECYCLE, encoding="utf-8").read()
    body = re.search(r"--lifecycle-configuration '(\{.*?\})'", script, re.S).group(1)
    rules = {r["ID"]: r for r in json.loads(body)["Rules"]}
    for rid, prefix in (("template-sources-expiry", "template_sources/"),
                        ("template-digitise-requests-expiry", "template_digitise_requests/"),
                        ("template-digitise-results-expiry", "template_digitise_results/")):
        rule = rules.get(rid)
        assert rule and rule["Status"] == "Enabled", rid
        assert rule["Filter"]["Prefix"] == prefix
        assert rule["Expiration"]["Days"] == 90
        # the script refuses to run over rules it does not manage; its own must be listed
        assert "ID!=`%s`" % rid in script, f"{rid} missing from the exclusion query"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `$PYTEST tests/unit/test_template_digitise_wiring.py -q`
Expected: FAIL with `KeyError: 'TemplateDigitiseFunction'` and missing script entries.

- [ ] **Step 3: Add the function to `template.yaml`**

Insert directly above the line `  # Lambda 10: Org API (Phase 3) — real write backend for` — i.e. after MigrateFunction's `        - VPCAccessPolicy: {}` and the blank line — this block (keep the dashes comment line that follows it as OrgApi's header):

```yaml
  # ----------------------------------------------------------
  # Template digitise worker (spec 2026-10-16). Reads a checklist form a person
  # uploaded (template_sources/) into a DRAFT template: a Word file with python-docx,
  # a PDF or photos with one vision call. Non-VPC: it calls OpenRouter, and org-api
  # (in-VPC, no outbound) hands it the work through template_digitise_requests/
  # (BUG-36). NO Events: wired by scripts/wire-s3-events.sh (fs-template-digitise,
  # BUG-33). Its output prefix template_digitise_results/ matches no trigger.
  # ----------------------------------------------------------
  TemplateDigitiseFunction:
    Type: AWS::Serverless::Function
    Properties:
      FunctionName: !Sub ["${P}-template-digitise", {P: !FindInMap [StageConfig, !Ref Stage, Prefix]}]
      CodeUri: src/
      Handler: lambda_template_digitise.lambda_handler
      Timeout: 900
      MemorySize: 1024
      Runtime: python3.12  # python-docx-layer is built for cp312 (BUG-24); it also carries Pillow
      Layers: !If
        - HasDocxLayer
        - - !Ref DocxLayerArn
        - !Ref AWS::NoValue
      Environment:
        Variables:
          S3_BUCKET: !Ref IngestBucketName   # the lake -- where org-api writes requests and sources
          LLM_PROVIDER: !Ref LlmProvider
          # Carried because every LLM_PROVIDER carrier must carry it (wiring test); the
          # vision call sends temperature 0 explicitly whatever this says (ruling R3).
          LLM_TEMPERATURE: !Ref LlmTemperature
          QWEN_API_KEY: !If [UsesSeparateChatVendor, !Ref QwenChatApiKey, !Ref DashScopeApiKey]
          QWEN_BASE_URL: !Ref QwenBaseUrl
          LLM_CHAIN_BUDGET_SECONDS: '840'  # Lambda Timeout 900 - 60
          # A 115-item form took 27-40 s in the spike; 300 s leaves the chain room to retry.
          LLM_HTTP_TIMEOUT: '300'
          # EMPTY on purpose. Globals sets the TEXT models' chain; this function never
          # calls call_llm, and a text model handed page images would invent items.
          LLM_FALLBACK_MODELS: ''
          TEMPLATE_DIGITISE_MODEL: google/gemini-3.8-flash
          TEMPLATE_DIGITISE_FALLBACK_MODELS: ''
      Policies:
        # lake_put: the result write also appends one manifest line in the bucket it wrote to.
        - Version: '2012-10-17'
          Statement:
            - Effect: Allow
              Action: s3:PutObject
              Resource:
                - !Sub arn:aws:s3:::${DataBucketName}/lake_manifest/*
                - !Sub arn:aws:s3:::${IngestBucketName}/lake_manifest/*
        - Version: '2012-10-17'
          Statement:
            - Effect: Allow
              Action: s3:GetObject
              Resource:
                - !Sub arn:aws:s3:::${IngestBucketName}/template_digitise_requests/*
                - !Sub arn:aws:s3:::${IngestBucketName}/template_sources/*
            - Effect: Allow
              Action: s3:PutObject
              Resource: !Sub arn:aws:s3:::${IngestBucketName}/template_digitise_results/*

```

- [ ] **Step 4: Add the trigger to `scripts/wire-s3-events.sh`**

After the line `SPEAKER_EMBED_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${PREFIX}-speaker-embed"` add:

```bash
TEMPLATE_DIGITISE_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${PREFIX}-template-digitise"
```

After the session-report block's closing `fi` (the line after `echo "NOTE: ${PREFIX}-session-report not deployed — skipping session-report trigger"`), add:

```bash
# NOTE(template digitise, spec 2026-10-16): the non-VPC worker triggers on
# template_digitise_requests/*.json -- org-api's request for one uploaded form. Its own
# output (template_digitise_results/) and the uploads themselves (template_sources/) match
# no rule here, so there is no BUG-13 loop; the prefix is disjoint from every other one.
if fn_exists "${PREFIX}-template-digitise"; then
  WIRE_FNS+=("${PREFIX}-template-digitise")
  DESIRED=$(jq -c --arg arn "$TEMPLATE_DIGITISE_ARN" '. + [
    {"Id":"fs-template-digitise","LambdaFunctionArn":$arn,"Events":["s3:ObjectCreated:*"],
     "Filter":{"Key":{"FilterRules":[{"Name":"prefix","Value":"template_digitise_requests/"},{"Name":"suffix","Value":".json"}]}}}
  ]' <<<"$DESIRED")
else
  echo "NOTE: ${PREFIX}-template-digitise not deployed — skipping template-digitise trigger"
fi
```

Also add one line to the header comment list under `#   Ingest      ← index_vectors/**.json ...`:

```bash
#   TemplateDigitise ← template_digitise_requests/**.json (org-api's request to read an uploaded form)
```

- [ ] **Step 5: Add the lifecycle rules to `scripts/wire-bucket-lifecycle.sh`**

In the `--query` line, replace `` && ID!=`traces-expiry`].ID' `` with:

```
 && ID!=`traces-expiry` && ID!=`template-sources-expiry` && ID!=`template-digitise-requests-expiry` && ID!=`template-digitise-results-expiry`].ID'
```

After the `traces-expiry` rule object (before the closing `    ]`), add:

```json
      ,{
        "ID": "template-sources-expiry",
        "Status": "Enabled",
        "Filter": { "Prefix": "template_sources/" },
        "Expiration": { "Days": 90 }
      }
      ,{
        "ID": "template-digitise-requests-expiry",
        "Status": "Enabled",
        "Filter": { "Prefix": "template_digitise_requests/" },
        "Expiration": { "Days": 90 }
      }
      ,{
        "ID": "template-digitise-results-expiry",
        "Status": "Enabled",
        "Filter": { "Prefix": "template_digitise_results/" },
        "Expiration": { "Days": 90 }
      }
```

And add a line to the header comment: `# template_sources/ and template_digitise_{requests,results}/ (a form a person uploaded to read into a template, spec 2026-10-16) are kept 90 days.`

- [ ] **Step 6: Add the worker to the lake_put caller map**

In `tests/unit/test_lake_ownership_template.py`, inside `LAKE_PUT_CALLERS`, after the `"SessionReportFunction": ...` line add:

```python
    "TemplateDigitiseFunction": "lambda_template_digitise",
```

- [ ] **Step 7: Run the wiring tests and the template-wide guards**

Run: `$PYTEST tests/unit/test_template_digitise_wiring.py tests/unit/test_lake_ownership_template.py tests/unit/test_template_timeout_invariants.py tests/unit/test_template_llm_timeout_ordering.py tests/unit/test_template_workflow_parameter_wiring.py tests/unit/test_search_index_wiring.py tests/unit/test_a_vendor_404_is_retried.py -q`
Expected: all PASS.

- [ ] **Step 8: Lint the template and dry-run the script syntax**

Run: `bash -n scripts/wire-s3-events.sh && bash -n scripts/wire-bucket-lifecycle.sh && echo ok`
Expected: `ok`.
Run the repo's pinned cfn-lint the way `.github/workflows/test.yml` does (read the exact version line there first) on `src/template.yaml`.
Expected: no new errors.

- [ ] **Step 9: Check the deploy role can create the new function before anything is pushed**

A missing deploy-role permission is CREATE_FAILED and a full TEST stack rollback.

```bash
export MSYS_NO_PATHCONV=1
aws iam simulate-principal-policy --profile fieldsight-deployer --region ap-southeast-2 \
  --policy-source-arn arn:aws:iam::509194952652:role/github-actions-fieldsight-deploy \
  --action-names lambda:CreateFunction lambda:TagResource iam:CreateRole iam:PutRolePolicy iam:AttachRolePolicy iam:PassRole \
  --resource-arns arn:aws:lambda:ap-southeast-2:509194952652:function:fieldsight-test-template-digitise \
                  arn:aws:iam::509194952652:role/fieldsight-test-TemplateDigitiseFunctionRole-ABC123 \
  --query "EvaluationResults[].[EvalActionName,EvalResourceName,EvalDecision]" --output table
```

Expected: every row `allowed`. If any is `implicitDeny`, stop and report to the controller (the deploy role is owner-managed).

- [ ] **Step 10: Commit**

```bash
git add src/template.yaml scripts/wire-s3-events.sh scripts/wire-bucket-lifecycle.sh \
  tests/unit/test_template_digitise_wiring.py tests/unit/test_lake_ownership_template.py
git commit -m "$(cat <<'EOF'
template.yaml: TemplateDigitiseFunction, its trigger and 90-day lifecycle rules

Non-VPC, python3.12 with the docx layer, its own empty model chain, read on
sources/requests and write on results only. Trigger fs-template-digitise on
template_digitise_requests/; the three prefixes expire after 90 days.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: org-api routes — upload-url, request, poll — behind `TEMPLATE_DIGITISE`

**Files:**
- Modify: `src/lambda_org_api.py` (import; dispatch block after the `/templates/bindings/{id}` PUT; handlers after `create_report_template`)
- Modify: `src/template.yaml` (Parameter `TemplateDigitise` after `CompanySummaryServe`; OrgApi env; OrgApi IAM)
- Modify: `.github/workflows/deploy.yml`, `.github/workflows/deploy-prod.yml` (one override line each)
- Modify: `tests/unit/test_missing_key_must_read_as_404.py` (three rows)
- Modify: `tests/unit/test_template_digitise_wiring.py` (org-api grants + flag wiring)
- Create: `tests/unit/test_template_digitise_routes.py`

**Interfaces:**
- Consumes: `template_types.EXT_TYPES`, `accepted_exts()`, `ext_of()`, `source_shape_error()`, `MAX_BYTES`, `MAX_IMAGES`; `_template_company`, `_template_name`, `is_cross_company`, `lake_objects.record_best_effort`, `s3()`, `LAKE_BUCKET`, `PRESIGNED_URL_EXPIRY`.
- Produces (HTTP, consumed by Task 7):
  - `POST /api/org/templates/digitise/upload-url` body `{filename, contentType, size}` -> 200 `{uploadUrl, sourceKey, expiresIn}`; 400 on type/size; 404 when off.
  - `POST /api/org/templates/digitise` body `{sourceKeys: [str], name}` -> 202 `{id, status: "pending"}`; 400/404.
  - `GET /api/org/templates/digitise/{id}` -> 200 `{id, status: "pending"}` or `{id, status, name, sections, counts, flags, route, model, pages, error?, reason?, sources: [{key, contentType, url}]}`; 404.
  - All three honour `?company_id=` for platform_admin exactly as `_template_company`.

- [ ] **Step 1: Write the failing route tests**

`tests/unit/test_template_digitise_routes.py`:

```python
"""The digitise routes: who may upload, what may be read, and whose draft is served.

Through dispatch (not the handlers) so the route order is under test too: "digitise"
must never be read as a template id by "/templates/{id}".
"""
import json
from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest

oa = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")
from botocore.exceptions import ClientError  # noqa: E402

COMPANY = "c0000000-0000-0000-0000-000000000001"
OTHER = "c0000000-0000-0000-0000-000000000002"
GM = {"id": "u-gm", "company_id": COMPANY, "global_role": "gm", "cognito_sub": "s-gm"}
WORKER = {"id": "u-wk", "company_id": COMPANY, "global_role": "worker", "cognito_sub": "s-wk"}
PLATFORM = {"id": "u-pa", "company_id": OTHER, "global_role": "platform_admin",
            "cognito_sub": "s-pa"}
RID = "a" * 32


def _missing(op):
    return ClientError({"Error": {"Code": "404" if op == "HeadObject" else "NoSuchKey",
                                  "Message": "missing"}}, op)


class _S3:
    def __init__(self):
        self.objects = {}      # key -> (bytes, LastModified)
        self.puts = {}
        self.presigned = []

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.presigned.append((op, Params, ExpiresIn))
        return "https://s3.example/%s/%s" % (op, Params["Key"])

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _missing("HeadObject")
        return {"ContentLength": len(self.objects[Key][0])}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _missing("GetObject")
        body, modified = self.objects[Key]
        return {"Body": BytesIO(body), "LastModified": modified}

    def put_object(self, Bucket, Key, Body, **kw):
        self.puts[Key] = Body
        return {}


@pytest.fixture
def s3(monkeypatch):
    fake = _S3()
    monkeypatch.setattr(oa, "s3", lambda: fake)
    monkeypatch.setattr(oa, "LAKE_BUCKET", "lake")
    monkeypatch.setenv("TEMPLATE_DIGITISE", "true")
    monkeypatch.setattr(oa, "companies", type("C", (), {"get_company_by_id": staticmethod(
        lambda conn, cid: {"id": cid})})())
    recorded = []
    monkeypatch.setattr(oa.lake_objects, "record_best_effort",
                        lambda conn, key, **kw: recorded.append((key, kw)) or True)
    fake.recorded = recorded
    return fake


def _routed(monkeypatch, caller, method, path, body=None, qs=None):
    monkeypatch.setattr(oa.users, "get_user_by_sub", lambda conn, sub: dict(caller))
    monkeypatch.setattr(oa.device_heartbeat, "parse_headers", lambda h: None)
    monkeypatch.setattr(oa.device_heartbeat, "record", lambda *a, **kw: None)
    ev = {"httpMethod": method, "path": "/api/org" + path, "queryStringParameters": qs,
          "body": json.dumps(body) if body is not None else None,
          "requestContext": {"authorizer": {"claims": {"sub": caller["cognito_sub"]}}}}
    resp = oa.dispatch(None, ev, method, path)
    return resp["statusCode"], json.loads(resp["body"])


def _src(ext="jpg", company=COMPANY, n="b"):
    return f"template_sources/{company}/{n * 32}.{ext}"


# ---- the switch -------------------------------------------------------------------

@pytest.mark.parametrize("method,path,body", [
    ("POST", "/templates/digitise/upload-url", {"filename": "f.pdf", "contentType": "application/pdf", "size": 1}),
    ("POST", "/templates/digitise", {"sourceKeys": [_src()], "name": "x"}),
    ("GET", "/templates/digitise/" + RID, None),
])
def test_THE_off_means_404_on_every_route_and_no_s3_call(s3, monkeypatch, method, path, body):
    monkeypatch.setenv("TEMPLATE_DIGITISE", "false")
    status, _ = _routed(monkeypatch, WORKER, method, path, body)
    assert status == 404
    assert s3.presigned == [] and s3.puts == {}


# ---- upload-url ---------------------------------------------------------------------

def test_upload_url_signs_a_put_under_the_callers_company(s3, monkeypatch):
    status, body = _routed(monkeypatch, WORKER, "POST", "/templates/digitise/upload-url",
                           {"filename": "Site Form.PDF", "contentType": "application/pdf",
                            "size": 1234})
    assert status == 200
    assert body["sourceKey"].startswith(f"template_sources/{COMPANY}/")
    assert body["sourceKey"].endswith(".pdf")
    op, params, expires = s3.presigned[0]
    assert op == "put_object"
    assert params == {"Bucket": "lake", "Key": body["sourceKey"], "ContentType": "application/pdf"}


@pytest.mark.parametrize("payload,why", [
    ({"filename": "notes.md", "contentType": "text/markdown", "size": 10}, "type"),
    ({"filename": "scan.tiff", "contentType": "image/tiff", "size": 10}, "type"),
    ({"filename": "f.pdf", "contentType": "image/png", "size": 10}, "match"),
    ({"filename": "f.pdf", "contentType": "application/pdf", "size": 20 * 1024 * 1024 + 1}, "20 MB"),
    ({"filename": "f.pdf", "contentType": "application/pdf"}, "size"),
])
def test_upload_url_refuses_before_any_signature(s3, monkeypatch, payload, why):
    status, body = _routed(monkeypatch, WORKER, "POST", "/templates/digitise/upload-url", payload)
    assert status == 400 and why in body["error"]
    assert s3.presigned == []


def test_a_platform_admin_uploads_into_the_company_it_names(s3, monkeypatch):
    status, body = _routed(monkeypatch, PLATFORM, "POST", "/templates/digitise/upload-url",
                           {"filename": "f.png", "contentType": "image/png", "size": 5},
                           qs={"company_id": COMPANY})
    assert status == 200 and body["sourceKey"].startswith(f"template_sources/{COMPANY}/")


def test_a_worker_cannot_name_another_company(s3, monkeypatch):
    status, _ = _routed(monkeypatch, WORKER, "POST", "/templates/digitise/upload-url",
                        {"filename": "f.png", "contentType": "image/png", "size": 5},
                        qs={"company_id": OTHER})
    assert status == 403


# ---- request ------------------------------------------------------------------------

def test_a_request_writes_the_artifact_and_records_ownership(s3, monkeypatch):
    keys = [_src("jpg", n="b"), _src("png", n="c")]
    for k in keys:
        s3.objects[k] = (b"img", datetime.now(timezone.utc))
    status, body = _routed(monkeypatch, WORKER, "POST", "/templates/digitise",
                           {"sourceKeys": keys, "name": "Site inspection"})
    assert status == 202 and body["status"] == "pending"
    rid = body["id"]
    req = json.loads(s3.puts[f"template_digitise_requests/{COMPANY}/{rid}.json"])
    assert (req["id"], req["companyId"], req["userId"], req["sourceKeys"], req["name"]) == (
        rid, COMPANY, "u-wk", keys, "Site inspection")
    kinds = {k: kw["kind"] for k, kw in s3.recorded}
    assert kinds[keys[0]] == "template_source" and kinds[keys[1]] == "template_source"
    assert f"template_digitise_requests/{COMPANY}/{rid}.json" in kinds
    assert f"template_digitise_results/{COMPANY}/{rid}.json" in kinds
    assert all(kw["company_id"] == COMPANY for _, kw in s3.recorded)


def test_a_key_from_another_company_is_never_accepted(s3, monkeypatch):
    k = _src(company=OTHER)
    s3.objects[k] = (b"img", datetime.now(timezone.utc))
    status, _ = _routed(monkeypatch, WORKER, "POST", "/templates/digitise",
                        {"sourceKeys": [k], "name": "x"})
    assert status == 400 and s3.puts == {}


@pytest.mark.parametrize("keys", [
    ["template_sources/../x.jpg"],
    [_src("docx", n="b"), _src("jpg", n="c")],
    [_src("jpg", n=c) for c in "bcdefghijkl"],   # 11 images
    [_src("jpg", n="b"), _src("jpg", n="b")],    # the same page twice
    [],
])
def test_a_request_of_the_wrong_shape_is_refused(s3, monkeypatch, keys):
    for k in keys:
        s3.objects[k] = (b"img", datetime.now(timezone.utc))
    status, _ = _routed(monkeypatch, WORKER, "POST", "/templates/digitise",
                        {"sourceKeys": keys, "name": "x"})
    assert status == 400 and s3.puts == {}


def test_a_request_for_a_file_that_was_never_uploaded_is_404(s3, monkeypatch):
    status, _ = _routed(monkeypatch, WORKER, "POST", "/templates/digitise",
                        {"sourceKeys": [_src()], "name": "x"})
    assert status == 404 and s3.puts == {}


def test_an_upload_larger_than_signed_for_is_refused_on_the_head(s3, monkeypatch):
    k = _src("pdf")
    s3.objects[k] = (b"x" * (20 * 1024 * 1024 + 1), datetime.now(timezone.utc))
    status, body = _routed(monkeypatch, WORKER, "POST", "/templates/digitise",
                           {"sourceKeys": [k], "name": "x"})
    assert status == 400 and "20 MB" in body["error"] and s3.puts == {}


def test_a_request_needs_a_name(s3, monkeypatch):
    k = _src()
    s3.objects[k] = (b"img", datetime.now(timezone.utc))
    status, _ = _routed(monkeypatch, WORKER, "POST", "/templates/digitise", {"sourceKeys": [k]})
    assert status == 400


# ---- poll ---------------------------------------------------------------------------

def _req(s3, user="u-wk"):
    s3.objects[f"template_digitise_requests/{COMPANY}/{RID}.json"] = (
        json.dumps({"id": RID, "companyId": COMPANY, "userId": user}).encode(),
        datetime.now(timezone.utc))


def _res(s3, user="u-wk", age=timedelta(0), status="done"):
    result = {"id": RID, "companyId": COMPANY, "userId": user, "status": status,
              "name": "Site inspection", "sourceKeys": [_src("jpg"), _src("pdf", n="c")],
              "sections": [{"title": "General", "items": [{"text": "Gate", "flags": []}],
                            "flags": []}],
              "counts": {"sections": 1, "items": 1}, "flags": [], "route": "vision",
              "model": "google/gemini-3.8-flash", "pages": 2}
    s3.objects[f"template_digitise_results/{COMPANY}/{RID}.json"] = (
        json.dumps(result).encode(), datetime.now(timezone.utc) - age)


def test_the_digitise_id_is_not_read_as_a_template_id(s3, monkeypatch):
    monkeypatch.setattr(oa, "get_report_template",
                        lambda *a, **k: pytest.fail("routed to /templates/{id}"))
    _req(s3)
    status, body = _routed(monkeypatch, WORKER, "GET", "/templates/digitise/" + RID)
    assert status == 200 and body == {"id": RID, "status": "pending"}


def test_poll_pending_until_the_worker_writes(s3, monkeypatch):
    _req(s3)
    assert _routed(monkeypatch, WORKER, "GET", "/templates/digitise/" + RID)[1]["status"] == "pending"


def test_poll_done_serves_the_draft_and_presigns_each_source_for_an_hour(s3, monkeypatch):
    _req(s3)
    _res(s3)
    status, body = _routed(monkeypatch, WORKER, "GET", "/templates/digitise/" + RID)
    assert status == 200 and body["status"] == "done"
    assert body["sections"][0]["items"][0]["text"] == "Gate"
    assert [s["contentType"] for s in body["sources"]] == ["image/jpeg", "application/pdf"]
    gets = [(p["Key"], e) for op, p, e in s3.presigned if op == "get_object"]
    assert gets == [(_src("jpg"), 3600), (_src("pdf", n="c"), 3600)]
    assert "userId" not in body and "companyId" not in body


def test_poll_an_id_that_never_existed_is_404(s3, monkeypatch):
    assert _routed(monkeypatch, WORKER, "GET", "/templates/digitise/" + RID)[0] == 404


@pytest.mark.parametrize("bad", ["digitise", "upload-url", "A" * 32, "a" * 31, "../x"])
def test_poll_a_malformed_id_is_404(s3, monkeypatch, bad):
    assert _routed(monkeypatch, WORKER, "GET", "/templates/digitise/" + bad)[0] == 404


def test_poll_someone_elses_draft_is_404(s3, monkeypatch):
    _req(s3, user="u-other")
    _res(s3, user="u-other")
    assert _routed(monkeypatch, WORKER, "GET", "/templates/digitise/" + RID)[0] == 404
    s3.objects.pop(f"template_digitise_results/{COMPANY}/{RID}.json")
    assert _routed(monkeypatch, WORKER, "GET", "/templates/digitise/" + RID)[0] == 404


def test_poll_a_platform_admin_may_read_it_in_the_company_it_names(s3, monkeypatch):
    _req(s3)
    _res(s3)
    status, body = _routed(monkeypatch, PLATFORM, "GET", "/templates/digitise/" + RID,
                           qs={"company_id": COMPANY})
    assert status == 200 and body["status"] == "done"


def test_poll_a_draft_older_than_seven_days_is_not_served(s3, monkeypatch):
    _req(s3)
    _res(s3, age=timedelta(days=7, minutes=1))
    status, body = _routed(monkeypatch, WORKER, "GET", "/templates/digitise/" + RID)
    assert status == 404 and "expired" in body["error"]


def test_poll_an_error_result_is_served_with_its_message(s3, monkeypatch):
    _req(s3)
    _res(s3, status="error")
    status, body = _routed(monkeypatch, WORKER, "GET", "/templates/digitise/" + RID)
    assert status == 200 and body["status"] == "error"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `$PYTEST tests/unit/test_template_digitise_routes.py -q`
Expected: FAIL (routes absent -> the generic `/templates/{id}` or the not-found fallback answers; status assertions fail).

- [ ] **Step 3: Import `template_types` in org-api**

In `src/lambda_org_api.py`, after the line `import turn_name_overlay` add:

```python
import template_types
```

- [ ] **Step 4: Add the dispatch block**

In `dispatch`, directly after these two lines:

```python
    if m_tb and method == "PUT":
        return set_report_template_binding(conn, caller, m_tb.group(1), event)
```

insert:

```python
    # Template digitise (spec 2026-10-16). Before every "/templates/{id}..." pattern
    # below, or "digitise" is read as a template id.
    if route == "/templates/digitise/upload-url" and method == "POST":
        return digitise_upload_url(conn, caller, event)
    if route == "/templates/digitise" and method == "POST":
        return digitise_request(conn, caller, event)
    m_td = re.match(r"^/templates/digitise/([^/]+)$", route)
    if m_td and method == "GET":
        return digitise_status(conn, caller, m_td.group(1), event)
```

- [ ] **Step 5: Add the handlers**

In `src/lambda_org_api.py`, directly after the end of `create_report_template` (the line `    return ok(_template_payload(fresh, versions=[_version_payload(version)]), 201)`), add:

```python


# ---- Template digitise (spec 2026-10-16) ----------------------------------------------
#
# A person uploads a checklist form; a non-VPC worker reads it into a DRAFT; the person
# checks it and saves it through POST /templates. Nothing here writes a template. org-api
# is in-VPC with no outbound (BUG-36), so the read is handed off through the lake like
# session reports: template_digitise_requests/ -> TemplateDigitiseFunction ->
# template_digitise_results/. Off (the default), every route answers 404: the UI's only
# gate is the route itself, because /me carries no feature list.

_DIGITISE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_DIGITISE_SOURCE_RE = re.compile(
    r"^template_sources/(?P<company>[^/]+)/[0-9a-f]{32}\.(?P<ext>pdf|docx|jpg|jpeg|png)$")
DIGITISE_MAX_AGE = timedelta(days=7)
DIGITISE_PREVIEW_EXPIRY = 3600
_DIGITISE_PUBLIC = ("id", "status", "name", "sections", "counts", "flags", "route", "model",
                    "pages", "error", "reason")


def _template_digitise_on():
    """Read per call so a test, or an emergency env edit, takes effect without a reload."""
    return os.environ.get("TEMPLATE_DIGITISE", "false").lower() == "true"


def _s3_missing(e):
    return e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound")


def digitise_upload_url(conn, caller, event):
    """POST /api/org/templates/digitise/upload-url {filename, contentType, size}"""
    if not _template_digitise_on():
        return error("not found", 404)
    company_id, err = _template_company(conn, caller, event)
    if err is not None:
        return err
    body = parse_body(event)
    if body is None:
        return error("malformed JSON body", 400)
    ext = template_types.ext_of(body.get("filename"))
    allowed = template_types.accepted_exts()
    if ext not in allowed:
        return error("file type must be one of: %s" % ", ".join("." + e for e in allowed), 400)
    content_type = template_types.EXT_TYPES[ext]
    if body.get("contentType") != content_type:
        return error("contentType does not match the file extension (.%s is %s)"
                     % (ext, content_type), 400)
    size = body.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        return error("size (bytes) is required", 400)
    if size > template_types.MAX_BYTES:
        return error("the file is larger than 20 MB", 400)
    key = f"template_sources/{company_id}/{uuid.uuid4().hex}.{ext}"
    url = s3().generate_presigned_url(
        "put_object",
        Params={"Bucket": LAKE_BUCKET, "Key": key, "ContentType": content_type},
        ExpiresIn=PRESIGNED_URL_EXPIRY)
    return ok({"uploadUrl": url, "sourceKey": key, "expiresIn": PRESIGNED_URL_EXPIRY})


def digitise_request(conn, caller, event):
    """POST /api/org/templates/digitise {sourceKeys, name} -> 202 {id, status: pending}"""
    if not _template_digitise_on():
        return error("not found", 404)
    company_id, err = _template_company(conn, caller, event)
    if err is not None:
        return err
    body = parse_body(event)
    if body is None:
        return error("malformed JSON body", 400)
    keys = body.get("sourceKeys")
    if not isinstance(keys, list) or not keys or len(keys) > template_types.MAX_IMAGES \
            or len(set(map(str, keys))) != len(keys):
        return error(template_types.SHAPE_ERROR, 400)
    exts = []
    for k in keys:
        m = _DIGITISE_SOURCE_RE.match(k) if isinstance(k, str) else None
        if m is None or m.group("company") != company_id:
            # Never another company's object, whatever the caller sends.
            return error("sourceKeys must be files uploaded for this company", 400)
        exts.append(m.group("ext"))
    why = template_types.source_shape_error(exts)
    if why:
        return error(why, 400)
    name, err = _template_name(body)
    if err is not None:
        return err
    sizes = {}
    for k in keys:
        try:
            head = s3().head_object(Bucket=LAKE_BUCKET, Key=k)
        except ClientError as e:
            if _s3_missing(e):
                return error("an uploaded file was not found — upload it again", 404)
            raise
        sizes[k] = head.get("ContentLength") or 0
        # A presigned PUT cannot enforce a size; this is where it is enforced.
        if sizes[k] > template_types.MAX_BYTES:
            return error("the file is larger than 20 MB", 400)
    request_id = uuid.uuid4().hex
    request_key = f"template_digitise_requests/{company_id}/{request_id}.json"
    result_key = f"template_digitise_results/{company_id}/{request_id}.json"
    artifact = {"id": request_id, "companyId": company_id, "userId": str(caller["id"]),
                "sourceKeys": keys, "name": name,
                "requestedAt": datetime.now(timezone.utc).isoformat()}
    s3().put_object(Bucket=LAKE_BUCKET, Key=request_key, Body=json.dumps(artifact),
                    ContentType="application/json")
    # lake_keys does not know these families, so the kind is stated (spec section 2).
    for k in keys:
        lake_objects.record_best_effort(conn, k, company_id=company_id, user_id=caller["id"],
                                        source="upload", kind="template_source",
                                        size_bytes=sizes[k])
    for k in (request_key, result_key):
        lake_objects.record_best_effort(conn, k, company_id=company_id, user_id=caller["id"],
                                        source="upload", kind="template_digitise")
    return ok({"id": request_id, "status": "pending"}, 202)


def _digitise_owner_ok(caller, owner_id):
    """A draft is its requester's until Save (ruling R5); platform_admin may act for them."""
    return str(owner_id) == str(caller["id"]) or is_cross_company(caller["global_role"])


def digitise_status(conn, caller, request_id, event):
    """GET /api/org/templates/digitise/{id} -- the poll."""
    if not _template_digitise_on():
        return error("not found", 404)
    company_id, err = _template_company(conn, caller, event)
    if err is not None:
        return err
    if not _DIGITISE_ID_RE.match(request_id or ""):
        return error("not found", 404)
    result_key = f"template_digitise_results/{company_id}/{request_id}.json"
    request_key = f"template_digitise_requests/{company_id}/{request_id}.json"
    try:
        obj = s3().get_object(Bucket=LAKE_BUCKET, Key=result_key)
    except ClientError as e:
        if not _s3_missing(e):
            raise
        try:
            req = json.loads(s3().get_object(Bucket=LAKE_BUCKET, Key=request_key)["Body"]
                             .read().decode("utf-8"))
        except ClientError as e2:
            if _s3_missing(e2):
                return error("not found", 404)
            raise
        if not _digitise_owner_ok(caller, req.get("userId")):
            return error("not found", 404)
        return ok({"id": request_id, "status": "pending"})
    modified = obj.get("LastModified")
    if modified is not None and datetime.now(timezone.utc) - modified > DIGITISE_MAX_AGE:
        return error("this draft has expired — upload the form again", 404)
    result = json.loads(obj["Body"].read().decode("utf-8"))
    if not _digitise_owner_ok(caller, result.get("userId")):
        return error("not found", 404)
    out = {k: result[k] for k in _DIGITISE_PUBLIC if k in result}
    prefix = f"template_sources/{company_id}/"
    out["sources"] = [
        {"key": k, "contentType": template_types.EXT_TYPES.get(template_types.ext_of(k), ""),
         "url": s3().generate_presigned_url("get_object",
                                            Params={"Bucket": LAKE_BUCKET, "Key": k},
                                            ExpiresIn=DIGITISE_PREVIEW_EXPIRY)}
        for k in (result.get("sourceKeys") or []) if isinstance(k, str) and k.startswith(prefix)]
    return ok(out)
```

- [ ] **Step 6: Run the route tests to verify they pass**

Run: `$PYTEST tests/unit/test_template_digitise_routes.py tests/unit/test_report_template_routes.py -q`
Expected: all PASS (the second file proves the existing template routes still win/lose where they did).

- [ ] **Step 7: Write the failing template/workflow tests for org-api**

Append to `tests/unit/test_template_digitise_wiring.py`:

```python
WORKFLOWS = {"test": os.path.join(REPO, ".github", "workflows", "deploy.yml"),
             "prod": os.path.join(REPO, ".github", "workflows", "deploy-prod.yml")}


def _list_prefixes(fn, bucket="IngestBucketName"):
    out = []
    for st in _statements(fn):
        if "s3:ListBucket" not in _as_list(st.get("Action")):
            continue
        if not any(isinstance(r, str) and r.endswith("${%s}" % bucket)
                   for r in _as_list(st.get("Resource"))):
            continue
        out += _as_list(((st.get("Condition") or {}).get("StringLike") or {}).get("s3:prefix"))
    return out


def test_the_switch_exists_defaults_off_and_reaches_org_api():
    tpl = _yaml.load(io.open(TEMPLATE, encoding="utf-8").read(), Loader=_Loader)
    p = tpl["Parameters"]["TemplateDigitise"]
    assert p["Default"] == "false" and p["AllowedValues"] == ["true", "false"]
    env = _fn("OrgApiFunction")["Environment"]["Variables"]
    assert env["TEMPLATE_DIGITISE"] == "TemplateDigitise"   # the !Ref, as the loader reads it


def test_both_workflows_pass_the_switch_and_default_it_off():
    for stage, path in WORKFLOWS.items():
        text = open(path, encoding="utf-8").read()
        line = '"TemplateDigitise=${{ vars.%s_TEMPLATE_DIGITISE || \'false\' }}"' % stage.upper()
        assert line in text, f"{stage} cannot set TemplateDigitise"


def test_org_api_may_do_exactly_what_the_routes_do():
    g = _grants(_fn("OrgApiFunction"))
    assert ("s3:PutObject", "template_digitise_requests/*") in g      # the request
    assert ("s3:PutObject", "template_sources/*") in g                # a presigned PUT is the role's
    assert ("s3:GetObject", "template_sources/*") in g                # HEAD + the preview presign
    assert ("s3:GetObject", "template_digitise_results/*") in g       # the poll
    assert ("s3:GetObject", "template_digitise_requests/*") in g      # pending vs 404
    assert ("s3:PutObject", "template_digitise_results/*") not in g   # the worker's alone


def test_org_api_can_tell_a_missing_key_from_a_denied_one_on_all_three():
    prefixes = _list_prefixes(_fn("OrgApiFunction"))
    for p in ("template_sources/*", "template_digitise_requests/*", "template_digitise_results/*"):
        assert p in prefixes, f"{p}: a missing key would read as AccessDenied (500), not 404"
```

In `tests/unit/test_missing_key_must_read_as_404.py`, add to `MISSING_KEY_IS_NORMAL` (after the `session_report_results/*` row):

```python
    ("OrgApiFunction", "template_digitise_results/*",
     "GET /templates/digitise/{id} -> pending until the worker writes the draft"),
    ("OrgApiFunction", "template_digitise_requests/*",
     "GET /templates/digitise/{id} -> 404 for an id that was never requested"),
    ("OrgApiFunction", "template_sources/*",
     "POST /templates/digitise HEADs each upload; a file never uploaded must be 404"),
```

- [ ] **Step 8: Run them to verify they fail**

Run: `$PYTEST tests/unit/test_template_digitise_wiring.py tests/unit/test_missing_key_must_read_as_404.py -q`
Expected: FAIL (`KeyError: 'TemplateDigitise'`, missing grants and prefixes).

- [ ] **Step 9: Template Parameter, env, IAM; workflow lines**

`src/template.yaml` — after the `CompanySummaryServe` Parameter block (after its `Description` paragraph ending `Owner product decision.`), add:

```yaml

  # Template digitise (spec 2026-10-16): org-api's /templates/digitise routes. Wired in
  # three pieces, pinned by tests/unit/test_template_digitise_wiring.py: this Parameter ->
  # OrgApiFunction env TEMPLATE_DIGITISE -> lambda_org_api._template_digitise_on().
  TemplateDigitise:
    Type: String
    Default: 'false'
    AllowedValues: ['true', 'false']
    Description: >-
      When true, org-api serves /templates/digitise (read a checklist form from a photo,
      PDF or Word file into a draft template). False (default) answers 404 on those
      routes, which is the UI's only gate. Set per environment by the repo variables
      TEST_TEMPLATE_DIGITISE / PROD_TEMPLATE_DIGITISE.
```

OrgApi env — after the line `          COMPANY_SUMMARY_SERVE: !Ref CompanySummaryServe` add:

```yaml
          # off -> /templates/digitise answers 404 (see the TemplateDigitise parameter)
          TEMPLATE_DIGITISE: !Ref TemplateDigitise
```

OrgApi IAM — after the line `              Resource: !Sub arn:aws:s3:::${IngestBucketName}/session_report_requests/*` (the org-api statement; it is the one indented 14 spaces with `Resource: !Sub`) add:

```yaml
            - Effect: Allow
              # Template digitise (spec 2026-10-16). POST /templates/digitise/upload-url
              # presigns a PUT into template_sources/ (a presigned PUT is only valid if
              # this role may PutObject the key); POST /templates/digitise HEADs each
              # upload and writes the request the non-VPC TemplateDigitiseFunction is
              # triggered by; GET /templates/digitise/{id} reads the request (pending vs
              # 404), the result, and presigns the sources for the review screen.
              Action:
                - s3:PutObject
                - s3:GetObject
              Resource:
                - !Sub arn:aws:s3:::${IngestBucketName}/template_sources/*
                - !Sub arn:aws:s3:::${IngestBucketName}/template_digitise_requests/*
            - Effect: Allow
              Action: s3:GetObject
              Resource: !Sub arn:aws:s3:::${IngestBucketName}/template_digitise_results/*
            - Effect: Allow
              # Without ListBucket a missing key answers AccessDenied, not NoSuchKey, and
              # "pending" / "never uploaded" become 500s (test_missing_key_must_read_as_404).
              Action: s3:ListBucket
              Resource: !Sub arn:aws:s3:::${IngestBucketName}
              Condition:
                StringLike:
                  s3:prefix:
                    - template_sources/*
                    - template_digitise_requests/*
                    - template_digitise_results/*
```

`.github/workflows/deploy.yml` — after the line `              "CompanySummaryServe=${{ vars.TEST_COMPANY_SUMMARY_SERVE || 'off' }}" \` add:

```
              "TemplateDigitise=${{ vars.TEST_TEMPLATE_DIGITISE || 'false' }}" \
```

`.github/workflows/deploy-prod.yml` — after the line `              "CompanySummaryServe=${{ vars.PROD_COMPANY_SUMMARY_SERVE || 'off' }}" \` add:

```
              "TemplateDigitise=${{ vars.PROD_TEMPLATE_DIGITISE || 'false' }}" \
```

- [ ] **Step 10: Run the wiring tests and the template-wide guards**

Run: `$PYTEST tests/unit/test_template_digitise_wiring.py tests/unit/test_missing_key_must_read_as_404.py tests/unit/test_template_workflow_parameter_wiring.py tests/unit/test_template_org_api_media_iam.py tests/unit/test_lake_ownership_template.py tests/unit/test_template_timeout_invariants.py -q`
Expected: all PASS.

- [ ] **Step 11: Run the whole backend suite**

Run: `$PYTEST -q`
Expected: PASS. Write down the pass/skip counts; the final task compares them with CI's on the same sha.

- [ ] **Step 12: Commit**

```bash
git add src/lambda_org_api.py src/template.yaml .github/workflows/deploy.yml .github/workflows/deploy-prod.yml \
  tests/unit/test_template_digitise_routes.py tests/unit/test_template_digitise_wiring.py \
  tests/unit/test_missing_key_must_read_as_404.py
git commit -m "$(cat <<'EOF'
org-api: /templates/digitise upload-url, request and poll behind TEMPLATE_DIGITISE

Company resolved as _template_company resolves it; every key checked against the
caller's company prefix and HEADed for existence and size; the draft is served to
its requester only, and not after 7 days. Off (the default on both stages) the
routes answer 404. IAM includes ListBucket on all three prefixes so a missing key
reads as 404, not 500.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Frontend — `templates.create()` sends the sections it is given

Run in `C:\Users\camil\Dropbox\fsui-template-digitise` (create it first, see "Repos and worktrees").

**Files:**
- Modify: `scripts/api/template-store.js` (`create`, the export block)
- Create: `tests/the-template-store-keeps-the-sections-it-is-given.test.js`

**Interfaces:**
- Produces: `window.FS.api.templates.create({scope, report_type, title, description, owner_user_id, sections?, body?})` — `body` (a full backend body) wins, else `sections` (editor/backend sections) go through `toBackendBody`, else today's starting sections. `window.FS.api.templates.DEFAULT_CATCH_ALL` (a copy-safe object, exported).

- [ ] **Step 1: Write the failing test**

`tests/the-template-store-keeps-the-sections-it-is-given.test.js`:

```js
'use strict';

/*
 * create() used to send the starting sections for the report type and DISCARD whatever
 * it was handed -- the API layer rebuilt the body. A digitised form's 115 items would
 * have been dropped on the way to the server with nothing anywhere saying so
 * (spec 2026-10-16 section 5). THE test reads the real request body.
 */
const test = require('node:test');
const assert = require('node:assert');

let calls;

function loadStore() {
  calls = [];
  global.localStorage = { getItem: () => null, setItem: () => {} };
  global.window = {
    FS: {
      api: {
        useMocks: false,
        orgBaseUrl: 'https://org.example/prod/api',
        orgRequest: async function (path, o) {
          calls.push({ path, method: (o && o.method) || 'GET', body: o && o.body });
          return { id: 't-9', company_id: 'c-1', scope: 'personal', slug: 's', name: 'n',
                   description: '', report_type: 'custom', current_version: 1,
                   created_at: 'now', updated_at: 'now' };
        },
      },
    },
  };
  delete require.cache[require.resolve('../scripts/api/template-store.js')];
  require('../scripts/api/template-store.js');
  return global.window.FS.api.templates;
}

const CHECKLIST = {
  sections: [{ key: 'general-1', title: 'General', kind: 'checklist',
               purpose: "Record the inspector's answer to each item from the recording.",
               items: ['Gate locked?', 'Signage up?'] }],
  catch_all: { key: 'other', title: 'Anything else', purpose: 'x' },
  excluded_subjects: [], style: [],
};

test('THE: a full body is sent as given', async () => {
  const api = loadStore();
  await api.create({ scope: 'personal', report_type: 'custom', title: 'Site form', body: CHECKLIST });
  const sent = calls.find((c) => c.path === '/templates' && c.method === 'POST').body;
  assert.deepStrictEqual(sent.body, CHECKLIST);
  assert.strictEqual(sent.report_type, 'custom');
  assert.strictEqual(sent.name, 'Site form');
});

test('sections without a body go through the shape adapter and keep their items', async () => {
  const api = loadStore();
  await api.create({ title: 'x', sections: [{ title: 'General', kind: 'checklist',
    purpose: 'p', items: ['A?'] }] });
  const sent = calls.find((c) => c.method === 'POST').body.body;
  assert.deepStrictEqual(sent.sections[0].items, ['A?']);
  assert.strictEqual(sent.sections[0].kind, 'checklist');
  assert.strictEqual(sent.sections[0].key, 'general');
  assert.deepStrictEqual(sent.catch_all, api.DEFAULT_CATCH_ALL);
});

test('with neither, the starting sections for the report type are still sent', async () => {
  const api = loadStore();
  await api.create({ title: 'x', report_type: 'weekly' });
  const sent = calls.find((c) => c.method === 'POST').body.body;
  assert.strictEqual(sent.sections[0].title, 'Executive Summary');
});

test('the catch-all is exported and cannot be mutated through the export', () => {
  const api = loadStore();
  assert.strictEqual(api.DEFAULT_CATCH_ALL.key, 'other');
  api.DEFAULT_CATCH_ALL.title = 'changed';
  assert.notStrictEqual(api.DEFAULT_CATCH_ALL.title, 'changed');
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `node --test tests/the-template-store-keeps-the-sections-it-is-given.test.js`
Expected: FAIL (`sent.body` is the starting sections; `DEFAULT_CATCH_ALL` undefined).

- [ ] **Step 3: Implement**

In `scripts/api/template-store.js` `create()`, replace the live branch:

```js
    if (!orgLive()) return unavailable('Creating a template');
    var reportType = data.report_type || 'daily';
    var starting = STARTING_SECTIONS[reportType] || STARTING_SECTIONS.daily;
    return api().orgRequest('/templates', {
      method: 'POST',
      retry: false,
      body: {
        scope: data.scope || 'personal',
        report_type: reportType,
        name: data.title || 'New Template',
        description: data.description || '',
        body: toBackendBody({ sections: clone(starting) }),
        change_note: 'Created',
      },
```

with:

```js
    if (!orgLive()) return unavailable('Creating a template');
    var reportType = data.report_type || 'daily';
    /* What the caller made wins. This used to send the starting sections and drop
       whatever it was handed -- a digitised form's items vanished here, between the
       review screen and the server (spec 2026-10-16 section 5). */
    var sections = (Array.isArray(data.sections) && data.sections.length)
      ? clone(data.sections)
      : clone(STARTING_SECTIONS[reportType] || STARTING_SECTIONS.daily);
    return api().orgRequest('/templates', {
      method: 'POST',
      retry: false,
      body: {
        scope: data.scope || 'personal',
        report_type: reportType,
        name: data.title || 'New Template',
        description: data.description || '',
        body: data.body ? clone(data.body) : toBackendBody({ sections: sections }),
        change_note: data.change_note || 'Created',
      },
```

In the mock branch, replace `sections: clone(STARTING_SECTIONS[rt] || STARTING_SECTIONS.daily) })),` with:

```js
                       sections: (Array.isArray(data.sections) && data.sections.length)
                         ? clone(data.sections)
                         : (data.body ? clone(data.body.sections)
                                      : clone(STARTING_SECTIONS[rt] || STARTING_SECTIONS.daily)) })),
```

In the export object `window.FS.api.templates = { ... }`, after `FAVOURITES_CAP:   FAV_CAP,` add:

```js
    /* The UI's catch-all, for callers that build a whole body (template digitise). A
       getter, so nobody can change it for everybody else by assigning to it. */
    get DEFAULT_CATCH_ALL() { return clone(DEFAULT_CATCH_ALL); },
```

- [ ] **Step 4: Run the test and the store's neighbours**

Run: `node --test tests/the-template-store-keeps-the-sections-it-is-given.test.js tests/the-template-library-is-on-the-server.test.js tests/an-org-template-can-be-copied-to-your-library.test.js tests/the-template-version-reaches-the-backend.test.js`
Expected: all PASS.

- [ ] **Step 5: Bump the script version**

In `app-shell-preview.html` change `scripts/api/template-store.js?v=3` to `scripts/api/template-store.js?v=4`.

- [ ] **Step 6: Commit**

```bash
cd C:/Users/camil/Dropbox/fsui-template-digitise
git add scripts/api/template-store.js app-shell-preview.html tests/the-template-store-keeps-the-sections-it-is-given.test.js
git commit -m "$(cat <<'EOF'
template-store: create() sends the sections or body it is given

It used to send the report type's starting sections and discard the rest; a
digitised checklist would have lost every item between the review screen and the
server. DEFAULT_CATCH_ALL is exported for callers that build a whole body.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Frontend — the digitise flow and the save mapping (`api/template-digitise.js`)

**Files:**
- Create: `scripts/api/template-digitise.js`
- Create: `tests/template-digitise-flow.test.js`
- Create: `tests/fixtures/template-digitise.truth-penina.json` (copy of the backend fixture)
- Create: `tests/fixtures/template-digitise.save-body.json` (generated in Step 5)
- Modify: `app-shell-preview.html` (script tag)
- Backend (Step 7): create `C:\Users\camil\Dropbox\fs-template-digitise\tests\fixtures\template_digitise\ui_save_body.json` and `tests/unit/test_the_ui_save_body_is_a_valid_template.py`

**Interfaces:**
- Consumes: HTTP routes from Task 5; `window.FS.api.templates.DEFAULT_CATCH_ALL` (Task 6); `window.FS.api.orgRequest` (returns `{_notFound: true, status: 404}` on 404 and `{_accessDenied: true, error}` on 403 — it does not throw for either).
- Produces: `window.FS.api.templateDigitise = { EXT_TYPES, PURPOSE, BANNER, FLAG_TEXT, POLL_MS, GIVE_UP_MS, typeFor(file), checkFiles(files) -> string|null, digitise(files, name, {onPhase, fetch, sleep, now}) -> Promise<{off:true} | pollResult>, fromResult(result) -> draft, toTemplateBody(draft) -> body, bodyProblem(body) -> string|null, ops: {renameSection, editItem, deleteItem, addItem, moveItem, moveSection, deleteSection, mergeWithNext} }`. A draft is `{name, sections: [{title, items: [{text, flags}], flags}], flags}`.

- [ ] **Step 1: Copy the ground truth for the contract fixture**

```bash
cp C:/Users/camil/Dropbox/fs-template-digitise/tests/fixtures/template_digitise/truth_penina.json \
   C:/Users/camil/Dropbox/fsui-template-digitise/tests/fixtures/template-digitise.truth-penina.json
```

- [ ] **Step 2: Write the failing tests**

`tests/template-digitise-flow.test.js`:

```js
'use strict';

/*
 * Reading a form into a draft template: upload -> request -> poll -> review -> Save
 * (pipeline spec 2026-10-16). The server's routes answer 404 when reading is switched
 * off, and orgRequest hands that back as { _notFound: true } rather than throwing --
 * THE test is that "off" falls back without uploading anything.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

let calls, puts;

function load(reply) {
  calls = []; puts = [];
  global.localStorage = { getItem: () => null, setItem: () => {} };
  global.window = { FS: { api: {
    useMocks: false, orgBaseUrl: 'https://org.example/prod/api',
    orgRequest: async function (p, o) {
      calls.push({ path: p, method: (o && o.method) || 'GET', body: o && o.body, retry: o && o.retry });
      return reply(p, o || {});
    },
  } } };
  for (const f of ['../scripts/api/template-store.js', '../scripts/api/template-digitise.js']) {
    delete require.cache[require.resolve(f)];
    require(f);
  }
  return global.window.FS.api.templateDigitise;
}

function file(name, size, type) { return { name, size: size || 10, type: type === undefined ? 'x' : type }; }
const okFetch = async (url, o) => { puts.push({ url, o }); return { ok: true, status: 200 }; };
const noSleep = async () => {};

function server(results) {
  let n = 0; const seq = results.slice();
  return (p, o) => {
    if (p === '/templates/digitise/upload-url') { n += 1; return { uploadUrl: 'https://s3/put/' + n, sourceKey: 'template_sources/c/' + n + '.jpg', expiresIn: 900 }; }
    if (p === '/templates/digitise') return { id: 'r1', status: 'pending' };
    if (p === '/templates/digitise/r1') return seq.length > 1 ? seq.shift() : seq[0];
    throw new Error('unexpected ' + p);
  };
}

const DONE = { id: 'r1', status: 'done', name: 'Form', flags: [], counts: { sections: 1, items: 1 },
  sections: [{ title: 'General', items: [{ text: 'Gate locked?', flags: [] }], flags: [] }],
  sources: [{ key: 'k', contentType: 'image/jpeg', url: 'https://s3/get/k' }] };

test('THE: reading switched off falls back without uploading anything', async () => {
  const TD = load(() => ({ _notFound: true, status: 404 }));
  const phases = [];
  const out = await TD.digitise([file('a.jpg')], 'Form', { onPhase: (p) => phases.push(p), fetch: okFetch, sleep: noSleep });
  assert.deepStrictEqual(out, { off: true });
  assert.strictEqual(puts.length, 0);
  assert.ok(!phases.includes('reading'), 'never claims to be reading a form it did not send');
});

test('photos are uploaded in order, read, and polled until done', async () => {
  const TD = load(server([{ id: 'r1', status: 'pending' }, { id: 'r1', status: 'pending' }, DONE]));
  const phases = []; const slept = [];
  const out = await TD.digitise([file('p1.jpg'), file('p2.png')], 'Form',
    { onPhase: (p) => phases.push(p), fetch: okFetch, sleep: async (ms) => { slept.push(ms); } });
  assert.strictEqual(out.status, 'done');
  assert.deepStrictEqual(phases, ['uploading', 'reading']);
  assert.deepStrictEqual(puts.map((p) => p.url), ['https://s3/put/1', 'https://s3/put/2']);
  assert.deepStrictEqual(puts.map((p) => p.o.headers['Content-Type']), ['image/jpeg', 'image/png']);
  const req = calls.find((c) => c.path === '/templates/digitise');
  assert.deepStrictEqual(req.body, { sourceKeys: ['template_sources/c/1.jpg', 'template_sources/c/2.jpg'], name: 'Form' });
  assert.strictEqual(req.retry, false, 'a POST is never retried (a 504 is not a failed write)');
  assert.deepStrictEqual(slept, [TD.POLL_MS, TD.POLL_MS]);
});

test('a Word file with no browser type still uploads with the signed type', async () => {
  const TD = load(server([DONE]));
  await TD.digitise([file('Form.DOCX', 10, '')], 'Form', { fetch: okFetch, sleep: noSleep });
  const DOCX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
  assert.strictEqual(calls[0].body.contentType, DOCX);
  assert.strictEqual(puts[0].o.headers['Content-Type'], DOCX);
});

test('the poll gives up after three minutes', async () => {
  const TD = load(server([{ id: 'r1', status: 'pending' }]));
  let t = 0;
  await assert.rejects(
    TD.digitise([file('a.jpg')], 'Form', { fetch: okFetch, sleep: async (ms) => { t += ms; }, now: () => t }),
    /Taking too long — try again\./);
});

test('a draft that has gone (404 on the poll) is said so', async () => {
  const TD = load((p) => (p === '/templates/digitise/r1' ? { _notFound: true, status: 404 } : server([DONE])(p)));
  await assert.rejects(TD.digitise([file('a.jpg')], 'Form', { fetch: okFetch, sleep: noSleep }),
    /no longer available/);
});

test('a failed upload stops before the read is requested', async () => {
  const TD = load(server([DONE]));
  await assert.rejects(TD.digitise([file('a.jpg')], 'Form',
    { fetch: async () => ({ ok: false, status: 403 }), sleep: noSleep }), /Upload of a\.jpg failed/);
  assert.ok(!calls.some((c) => c.path === '/templates/digitise'));
});

test('files are checked before anything is sent', () => {
  const TD = load(server([DONE]));
  assert.match(TD.checkFiles([file('a.md')]), /PDF/);
  assert.match(TD.checkFiles([file('a.pdf'), file('b.jpg')]), /not a mix/);
  assert.match(TD.checkFiles([file('a.pdf'), file('b.pdf')]), /one PDF/);
  assert.match(TD.checkFiles(Array.from({ length: 11 }, (_, i) => file(i + '.jpg'))), /Up to 10/);
  assert.match(TD.checkFiles([file('a.jpg', 20 * 1024 * 1024 + 1)]), /20 MB/);
  assert.strictEqual(TD.checkFiles([file('a.docx')]), null);
});

/* ---- the save mapping ------------------------------------------------------- */

test('the save body is checklist sections of plain strings with the house purpose', () => {
  const TD = load(server([DONE]));
  const body = TD.toTemplateBody({ sections: [
    { title: ' General ', items: [{ text: 'Gate  locked?', flags: ['truncated'] }, { text: '  ', flags: [] }], flags: [] },
    { title: 'Empty', items: [], flags: [] },
    { title: 'General', items: [{ text: 'Lights on?', flags: [] }], flags: ['split_section'] }] });
  assert.deepStrictEqual(body.sections, [
    { key: 'general-1', title: 'General', kind: 'checklist', purpose: TD.PURPOSE, items: ['Gate locked?'] },
    { key: 'general-2', title: 'General', kind: 'checklist', purpose: TD.PURPOSE, items: ['Lights on?'] }]);
  assert.strictEqual(TD.PURPOSE, "Record the inspector's answer to each item from the recording.");
  assert.deepStrictEqual(body.catch_all, window.FS.api.templates.DEFAULT_CATCH_ALL);
  assert.deepStrictEqual([body.excluded_subjects, body.style], [[], []]);
});

test('the body is refused here for every rule the server refuses', () => {
  const TD = load(server([DONE]));
  const sec = (title, n, len) => ({ title, items: Array.from({ length: n }, () => ({ text: 'x'.repeat(len || 5), flags: [] })), flags: [] });
  const P = (sections) => TD.bodyProblem(TD.toTemplateBody({ sections }));
  assert.strictEqual(P([sec('A', 3)]), null);
  assert.match(P([]), /at least one section/);
  assert.match(P(Array.from({ length: 41 }, (_, i) => sec('S' + i, 1))), /40 sections/);
  assert.match(P([sec('', 1)]), /needs a name/);
  assert.match(P([sec('T'.repeat(121), 1)]), /120 characters/);
  assert.match(P([sec('A', 121)]), /more than 120 items/);
  assert.match(P([sec('A', 1, 301)]), /300 characters/);
  assert.match(P(Array.from({ length: 3 }, (_, i) => sec('S' + i, 120, 200))), /longer than a template can hold/);
});

test('the contract: the Penina form saves as exactly the committed body', () => {
  const TD = load(server([DONE]));
  const truth = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'template-digitise.truth-penina.json'), 'utf8'));
  const draft = TD.fromResult({ name: 'Penina', flags: [], sections: truth.sections.map((s) => ({
    title: s.title, flags: [], items: s.items.map((t) => ({ text: t, flags: [] })) })) });
  const body = TD.toTemplateBody(draft);
  const committed = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'template-digitise.save-body.json'), 'utf8'));
  assert.deepStrictEqual(body, committed);
  assert.strictEqual(TD.bodyProblem(body), null);
});

/* ---- review edits ----------------------------------------------------------- */

test('the review edits never mutate the draft they are given', () => {
  const TD = load(server([DONE]));
  const d = { sections: [
    { title: 'A', items: [{ text: 'a1', flags: ['truncated'] }, { text: 'a2', flags: [] }], flags: ['split_section'] },
    { title: 'A', items: [{ text: 'b1', flags: [] }], flags: ['split_section'] }], flags: [] };
  const frozen = JSON.stringify(d);
  const merged = TD.ops.mergeWithNext(d, 0);
  assert.deepStrictEqual(merged.sections.map((s) => s.items.map((i) => i.text)), [['a1', 'a2', 'b1']]);
  assert.deepStrictEqual(merged.sections[0].flags, []);
  assert.deepStrictEqual(TD.ops.editItem(d, 0, 0, 'A1').sections[0].items[0], { text: 'A1', flags: [] });
  assert.deepStrictEqual(TD.ops.moveItem(d, 0, 0, 1).sections[0].items.map((i) => i.text), ['a2', 'a1']);
  assert.deepStrictEqual(TD.ops.moveSection(d, 1, -1).sections.map((s) => s.items[0].text), ['b1', 'a1']);
  assert.strictEqual(TD.ops.addItem(d, 1).sections[1].items.length, 2);
  assert.strictEqual(TD.ops.deleteItem(d, 0, 1).sections[0].items.length, 1);
  assert.strictEqual(TD.ops.deleteSection(d, 0).sections.length, 1);
  assert.deepStrictEqual(TD.ops.renameSection(d, 0, 'Z').sections[0].flags, []);
  assert.strictEqual(JSON.stringify(d), frozen);
});

test('every flag the server can send has words for the person', () => {
  const TD = load(server([DONE]));
  for (const f of ['label_kept', 'near_duplicate_section', 'split_section', 'truncated',
                   'split_for_limit', 'too_many_sections', 'too_long']) {
    assert.ok(TD.FLAG_TEXT[f], f + ' has no wording');
  }
});
```

- [ ] **Step 3: Run them to verify they fail**

Run: `node --test tests/template-digitise-flow.test.js`
Expected: FAIL with `Cannot find module '../scripts/api/template-digitise.js'`.

- [ ] **Step 4: Implement**

`scripts/api/template-digitise.js`:

```js
/* ==========================================================================
   FieldSight · api/template-digitise.js
   --------------------------------------------------------------------------
   Reads a checklist form (a Word file, a PDF, or photos of its pages) into a
   DRAFT template: upload -> request -> poll. The person checks the draft on
   the review screen and saves it through the ordinary POST /templates; nothing
   is a template before Save. Pipeline spec:
   docs/superpowers/specs/2026-10-16-template-digitise-design.md

   "Off" is a VALUE here, never a catch: the routes answer 404 when reading is
   switched off on the server, and orgRequest returns { _notFound: true } for a
   404 instead of throwing.

   Exported to: window.FS.api.templateDigitise
   ========================================================================== */

(function () {
  'use strict';

  var EXT_TYPES = {
    pdf:  'application/pdf',
    docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    jpg:  'image/jpeg',
    jpeg: 'image/jpeg',
    png:  'image/png',
  };
  var IMAGE_EXTS = ['jpg', 'jpeg', 'png'];
  var MAX_BYTES  = 20 * 1024 * 1024;
  var MAX_IMAGES = 10;
  var POLL_MS    = 3000;
  var GIVE_UP_MS = 3 * 60 * 1000;
  /* The server's own limits (report_template.py / checklist.py). Checked here as
     well, not instead: a Save that bounces after a round trip makes the person
     hunt for which of nineteen sections it meant. */
  var LIMITS = { sections: 40, title: 120, items: 120, item: 300, body: 60000 };
  var PURPOSE = "Record the inspector's answer to each item from the recording.";
  var BANNER = 'Check the wording against your form — the reader can correct a spelling without saying so.';
  var FLAG_TEXT = {
    near_duplicate_section: 'Two sections with nearly the same name — one form split across a page?',
    split_section:          'Same heading as the section next to it — probably one table split by a page break. Merge them?',
    label_kept:             'Still starts with a list number or letter — check the wording.',
    truncated:              'Cut to fit the limit — check nothing that matters was lost.',
    split_for_limit:        'Split in two: one checklist holds at most 120 items.',
    too_many_sections:      'More than 40 sections — merge some before saving.',
    too_long:               'This form is longer than one template can hold — remove or merge sections before saving.',
  };

  function api() { return window.FS.api; }
  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  function extOf(name) {
    var m = /\.([a-z0-9]+)$/i.exec(String(name || ''));
    return m ? m[1].toLowerCase() : '';
  }

  /* From the extension, never file.type: some browsers hand a Word file over with
     an empty type, and the server signs the PUT for exactly the type named here. A
     mismatch is a 403 from S3 that reads like a permissions bug. */
  function typeFor(file) { return EXT_TYPES[extOf(file && file.name)] || ''; }

  function checkFiles(files) {
    files = files || [];
    if (!files.length) return 'Choose a file.';
    var exts = files.map(function (f) { return extOf(f.name); });
    for (var i = 0; i < files.length; i++) {
      if (!EXT_TYPES[exts[i]]) return 'Use a PDF, a Word file (.docx), or photos (.jpg, .png).';
      if (files[i].size > MAX_BYTES) return files[i].name + ' is larger than 20 MB.';
    }
    var images = exts.filter(function (e) { return IMAGE_EXTS.indexOf(e) >= 0; }).length;
    if (images && images !== files.length) return 'Upload one Word file, one PDF, or up to 10 photos — not a mix.';
    if (!images && files.length > 1) return 'Upload one Word file or one PDF at a time.';
    if (images > MAX_IMAGES) return 'Up to 10 photos — one per page.';
    return null;
  }

  function refused(res) {
    if (res && res._accessDenied) throw new Error(res.error || 'Access denied.');
    return res;
  }

  async function requestUploadUrl(file) {
    var res = await api().orgRequest('/templates/digitise/upload-url', {
      method: 'POST', retry: false,
      body: { filename: file.name, contentType: typeFor(file), size: file.size },
    });
    if (res && res._notFound) return { off: true };
    return refused(res);
  }

  async function putFile(file, url, fetchImpl) {
    var resp = await (fetchImpl || fetch)(url, {
      method: 'PUT', headers: { 'Content-Type': typeFor(file) }, body: file,
    });
    if (!resp.ok) throw new Error('Upload of ' + file.name + ' failed (' + resp.status + ') — try again.');
  }

  async function startRead(sourceKeys, name) {
    var res = await api().orgRequest('/templates/digitise', {
      method: 'POST', retry: false, body: { sourceKeys: sourceKeys, name: name },
    });
    if (res && res._notFound) return { off: true };
    return refused(res);
  }

  async function waitForDraft(id, opts) {
    var now = opts.now || Date.now;
    var wait = opts.sleep || sleep;
    var started = now();
    for (;;) {
      var res = refused(await api().orgRequest('/templates/digitise/' + encodeURIComponent(id)));
      if (res && res._notFound) throw new Error('This draft is no longer available — upload the form again.');
      if (res && res.status && res.status !== 'pending') return res;
      if (now() - started >= GIVE_UP_MS) throw new Error('Taking too long — try again.');
      await wait(POLL_MS);
    }
  }

  /* Upload each file, ask for the read, wait for the draft. Resolves { off: true }
     when the server does not offer reading (the caller then does today's thing),
     otherwise the poll result. onPhase('reading') fires only once the server has
     ACCEPTED the read -- the screen never says it is reading a form it did not get. */
  async function digitise(files, name, opts) {
    opts = opts || {};
    var onPhase = opts.onPhase || function () {};
    var problem = checkFiles(files);
    if (problem) throw new Error(problem);
    onPhase('uploading');
    var keys = [];
    for (var i = 0; i < files.length; i++) {
      var u = await requestUploadUrl(files[i]);
      if (u.off) return { off: true };
      await putFile(files[i], u.uploadUrl, opts.fetch);
      keys.push(u.sourceKey);
    }
    var started = await startRead(keys, name);
    if (started.off) return { off: true };
    onPhase('reading');
    return waitForDraft(started.id, opts);
  }

  function fromResult(result) {
    return { name: result.name || '', sections: clone(result.sections || []),
             flags: (result.flags || []).slice() };
  }

  function itemText(it) { return typeof it === 'string' ? it : ((it && it.text) || ''); }

  function slug(title, i) {
    var k = String(title || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
    return (k || 'section') + '-' + (i + 1);
  }

  /* The draft's {text, flags} items become the PLAIN STRINGS checklist.validate_items
     accepts; flags never leave the browser. */
  function toTemplateBody(draft) {
    var sections = (draft.sections || []).map(function (s) {
      return {
        title: String(s.title || '').trim(),
        items: (s.items || []).map(itemText)
          .map(function (t) { return t.replace(/\s+/g, ' ').trim(); })
          .filter(Boolean),
      };
    }).filter(function (s) { return s.items.length; });
    return {
      sections: sections.map(function (s, i) {
        return { key: slug(s.title, i), title: s.title, kind: 'checklist', purpose: PURPOSE, items: s.items };
      }),
      catch_all: api().templates.DEFAULT_CATCH_ALL,
      excluded_subjects: [],
      style: [],
    };
  }

  function bodyProblem(body) {
    var secs = body.sections || [];
    if (!secs.length) return 'The template needs at least one section with an item.';
    if (secs.length > LIMITS.sections) return 'A template holds at most 40 sections — merge some first.';
    for (var i = 0; i < secs.length; i++) {
      var s = secs[i]; var where = 'Section ' + (i + 1);
      if (!s.title) return where + ' needs a name.';
      if (s.title.length > LIMITS.title) return where + ': the name is longer than 120 characters.';
      if (s.items.length > LIMITS.items) return where + ' has more than 120 items — split it.';
      for (var j = 0; j < s.items.length; j++) {
        if (s.items[j].length > LIMITS.item) return where + ', item ' + (j + 1) + ' is longer than 300 characters.';
      }
    }
    if (JSON.stringify(body).length > LIMITS.body) return 'This form is longer than a template can hold — remove or merge sections.';
    return null;
  }

  function move(arr, i, delta) {
    var j = i + delta;
    if (j < 0 || j >= arr.length) return;
    var x = arr[i]; arr[i] = arr[j]; arr[j] = x;
  }

  /* Each edit returns a NEW draft: the review screen is controlled by the modal, which
     keeps the draft across a Save the server refuses. Touching a section or item
     clears its flags -- the person has looked at it. */
  var ops = {
    renameSection: function (d, i, title) { d = clone(d); d.sections[i].title = title; d.sections[i].flags = []; return d; },
    editItem:      function (d, i, j, text) { d = clone(d); d.sections[i].items[j] = { text: text, flags: [] }; return d; },
    deleteItem:    function (d, i, j) { d = clone(d); d.sections[i].items.splice(j, 1); return d; },
    addItem:       function (d, i) { d = clone(d); d.sections[i].items.push({ text: '', flags: [] }); return d; },
    moveItem:      function (d, i, j, delta) { d = clone(d); move(d.sections[i].items, j, delta); return d; },
    moveSection:   function (d, i, delta) { d = clone(d); move(d.sections, i, delta); return d; },
    deleteSection: function (d, i) { d = clone(d); d.sections.splice(i, 1); return d; },
    mergeWithNext: function (d, i) {
      d = clone(d);
      if (i + 1 >= d.sections.length) return d;
      d.sections[i].items = d.sections[i].items.concat(d.sections[i + 1].items);
      d.sections[i].flags = [];
      d.sections.splice(i + 1, 1);
      return d;
    },
  };

  if (!window.FS) window.FS = {};
  if (!window.FS.api) window.FS.api = {};
  window.FS.api.templateDigitise = {
    EXT_TYPES: EXT_TYPES, PURPOSE: PURPOSE, BANNER: BANNER, FLAG_TEXT: FLAG_TEXT,
    POLL_MS: POLL_MS, GIVE_UP_MS: GIVE_UP_MS,
    typeFor: typeFor, checkFiles: checkFiles, digitise: digitise,
    fromResult: fromResult, toTemplateBody: toTemplateBody, bodyProblem: bodyProblem, ops: ops,
  };
})();
```

- [ ] **Step 5: Generate the contract fixture from the implementation, then read it**

```bash
cd C:/Users/camil/Dropbox/fsui-template-digitise && node -e "
global.localStorage={getItem:()=>null,setItem:()=>{}};
global.window={FS:{api:{useMocks:false,orgBaseUrl:'x',orgRequest:async()=>({})}}};
require('./scripts/api/template-store.js'); require('./scripts/api/template-digitise.js');
const TD=window.FS.api.templateDigitise;
const t=require('./tests/fixtures/template-digitise.truth-penina.json');
const d=TD.fromResult({flags:[],sections:t.sections.map(s=>({title:s.title,flags:[],items:s.items.map(x=>({text:x,flags:[]}))}))});
require('fs').writeFileSync('tests/fixtures/template-digitise.save-body.json', JSON.stringify(TD.toTemplateBody(d), null, 1)+'\n');
"
```

Open the file and check by eye: 19 sections, `kind: "checklist"`, the house purpose, items are plain strings identical to the truth, `catch_all.key == "other"`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `node --test tests/template-digitise-flow.test.js`
Expected: all PASS.

- [ ] **Step 7: The seam — the backend validates the exact body the UI will send**

```bash
cp C:/Users/camil/Dropbox/fsui-template-digitise/tests/fixtures/template-digitise.save-body.json \
   C:/Users/camil/Dropbox/fs-template-digitise/tests/fixtures/template_digitise/ui_save_body.json
```

`C:\Users\camil\Dropbox\fs-template-digitise\tests\unit\test_the_ui_save_body_is_a_valid_template.py`:

```python
"""The body the Library's review screen sends on Save is one the server accepts.

The fixture is GENERATED by fieldsight-ui's scripts/api/template-digitise.js from the
Penina ground truth (its tests/template-digitise-flow.test.js pins it byte for byte), so
this is the seam between the two repos: the UI's mapping on one side, report_template's
rules on the other. Regenerate it on both sides together, never by hand.
"""
import json
import pathlib

import report_template

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "template_digitise"


def test_the_ui_save_body_passes_validate_body():
    body = json.load(open(FIXTURES / "ui_save_body.json", encoding="utf-8"))
    assert report_template.validate_body(body) is None


def test_it_carries_every_item_of_the_form_as_plain_strings():
    body = json.load(open(FIXTURES / "ui_save_body.json", encoding="utf-8"))
    truth = json.load(open(FIXTURES / "truth_penina.json", encoding="utf-8"))
    assert [s["items"] for s in body["sections"]] == [s["items"] for s in truth["sections"]]
    assert all(s["kind"] == "checklist" for s in body["sections"])
    assert {s["purpose"] for s in body["sections"]} == {
        "Record the inspector's answer to each item from the recording."}
```

Run: `$PYTEST tests/unit/test_the_ui_save_body_is_a_valid_template.py -q` (in the backend worktree)
Expected: PASS.

```bash
cd C:/Users/camil/Dropbox/fs-template-digitise
git add tests/fixtures/template_digitise/ui_save_body.json tests/unit/test_the_ui_save_body_is_a_valid_template.py
git commit -m "$(cat <<'EOF'
tests: the UI's digitised save body passes report_template.validate_body

Fixture generated by fieldsight-ui's template-digitise.js from the Penina ground
truth; pinned byte for byte on that side.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 8: Load the script in the app**

In `app-shell-preview.html`, directly after the line `  <script src="scripts/api/template-store.js?v=4"></script>` add:

```html
  <script src="scripts/api/template-digitise.js?v=1"></script>
```

- [ ] **Step 9: Commit (frontend)**

```bash
cd C:/Users/camil/Dropbox/fsui-template-digitise
git add scripts/api/template-digitise.js app-shell-preview.html tests/template-digitise-flow.test.js \
  tests/fixtures/template-digitise.truth-penina.json tests/fixtures/template-digitise.save-body.json
git commit -m "$(cat <<'EOF'
api/template-digitise: upload, read and poll a form; map the draft to a save body

A 404 from the server means reading is switched off and falls back without
uploading. The draft maps to checklist sections of plain strings with the house
purpose; the Penina contract body is committed here and validated by the backend.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Frontend — the review screen (`TemplateDigitiseReview`)

**Files:**
- Create: `scripts/composites/template-digitise-review.js`
- Modify: `styles/composites.css` (append a `§TEMPLATE-DIGITISE` section)
- Modify: `app-shell-preview.html` (script tag, CSS version)
- Create: `tests/the-digitised-draft-is-checked-before-it-is-saved.test.js`

**Interfaces:**
- Consumes: `window.FS.api.templateDigitise.{ops, FLAG_TEXT, BANNER}` (Task 7); `window.FieldSight.Button`.
- Produces: `window.FieldSight.TemplateDigitiseReview` with props `{draft, sources, error, saving, onChange(draft), onSave(draft), onCancel()}`.

- [ ] **Step 1: Write the failing test**

`tests/the-digitised-draft-is-checked-before-it-is-saved.test.js`:

```js
'use strict';

/*
 * The review screen: the draft beside the form, every edit through the pure ops in
 * api/template-digitise.js, flags in plain words, and the banner about silent
 * spelling fixes (spec 2026-10-16 section 5). Rendered with a minimal createElement
 * double: the repo has no DOM test harness, and what matters here is which ops each
 * control calls and which words reach the screen.
 */
const test = require('node:test');
const assert = require('node:assert');

function h(type, props, ...children) { return { type, props: props || {}, children: children.flat(Infinity) }; }
function walk(node, fn) {
  if (!node || typeof node !== 'object') return;
  fn(node);
  (node.children || []).forEach((c) => walk(c, fn));
  if (typeof node.type === 'function') walk(node.type(Object.assign({}, node.props, { children: node.children })), fn);
}
function texts(node) { const out = []; walk(node, (n) => n.children.forEach((c) => { if (typeof c === 'string') out.push(c); })); return out.join(' | '); }
function find(node, pred) { const out = []; walk(node, (n) => { if (pred(n)) out.push(n); }); return out; }

function load() {
  global.localStorage = { getItem: () => null, setItem: () => {} };
  global.window = { FS: { api: { useMocks: false, orgBaseUrl: 'x', orgRequest: async () => ({}) } },
    FieldSight: { Button: function Button(p) { return h('button', p, ...(p.children || [])); } } };
  global.React = { createElement: h };
  for (const f of ['../scripts/api/template-store.js', '../scripts/api/template-digitise.js',
                   '../scripts/composites/template-digitise-review.js']) {
    delete require.cache[require.resolve(f)]; require(f);
  }
  return window.FieldSight.TemplateDigitiseReview;
}

const DRAFT = { name: 'Penina', flags: ['too_many_sections'], sections: [
  { title: 'Compressed Gasses', flags: ['near_duplicate_section'],
    items: [{ text: 'Bottles upright?', flags: ['truncated'] }, { text: 'Flashback arrestors?', flags: [] }] },
  { title: 'Ladders', flags: [], items: [{ text: 'Footed?', flags: [] }] }] };
const SOURCES = [{ key: 'k1', contentType: 'image/jpeg', url: 'https://s3/get/k1' },
                 { key: 'k2', contentType: 'application/pdf', url: 'https://s3/get/k2' }];

function render(over) {
  const Review = load();
  const seen = { changed: [], saved: [] };
  const tree = Review(Object.assign({ draft: DRAFT, sources: SOURCES, error: '', saving: false,
    onChange: (d) => seen.changed.push(d), onSave: (d) => seen.saved.push(d), onCancel: () => {} }, over || {}));
  return { tree, seen };
}

test('THE: the banner about silent spelling fixes is on the screen', () => {
  const { tree } = render();
  assert.match(texts(tree), /Check the wording against your form — the reader can correct a spelling without saying so\./);
});

test('every flag is shown in words, at the level it belongs to', () => {
  const { tree } = render();
  const t = texts(tree);
  const TD = window.FS.api.templateDigitise;
  for (const f of ['too_many_sections', 'near_duplicate_section', 'truncated']) assert.ok(t.includes(TD.FLAG_TEXT[f]), f);
  assert.ok(find(tree, (n) => /--flagged/.test(n.props.className || '')).length >= 2, 'flagged section and item are highlighted');
});

test('each section shows its item count', () => {
  assert.match(texts(render().tree), /2 items[\s\S]*1 item\b/);
});

test('the source sits beside the draft: page images and the PDF', () => {
  const { tree } = render();
  assert.deepStrictEqual(find(tree, (n) => n.type === 'img').map((n) => n.props.src), ['https://s3/get/k1']);
  assert.strictEqual(find(tree, (n) => n.type === 'iframe')[0].props.src, 'https://s3/get/k2');
});

test('editing an item goes through ops.editItem and reaches onChange', () => {
  const { tree, seen } = render();
  const box = find(tree, (n) => n.type === 'textarea')[0];
  box.props.onChange({ target: { value: 'Bottles upright and secured?' } });
  assert.strictEqual(seen.changed[0].sections[0].items[0].text, 'Bottles upright and secured?');
});

test('merge with next is offered and joins the two sections', () => {
  const { tree, seen } = render();
  const merge = find(tree, (n) => n.type === 'button' && (n.children || []).includes('Merge with next'))[0];
  merge.props.onClick();
  assert.strictEqual(seen.changed[0].sections.length, 1);
  assert.strictEqual(seen.changed[0].sections[0].items.length, 3);
});

test('Save hands the current draft up; it is disabled while saving', () => {
  const { tree, seen } = render();
  find(tree, (n) => n.type === 'button' && (n.children || []).includes('Save template'))[0].props.onClick();
  assert.deepStrictEqual(seen.saved[0], DRAFT);
  const saving = render({ saving: true }).tree;
  assert.ok(find(saving, (n) => n.type === 'button' && (n.children || []).includes('Saving…'))[0].props.disabled);
});

test('a refused save is shown, not swallowed', () => {
  assert.match(texts(render({ error: 'a template called that already exists here' }).tree), /already exists/);
});

test('a Word source says there is no page picture to compare', () => {
  const { tree } = render({ sources: [{ key: 'k', contentType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', url: 'u' }] });
  assert.match(texts(tree), /Word file/);
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `node --test tests/the-digitised-draft-is-checked-before-it-is-saved.test.js`
Expected: FAIL with `Cannot find module '../scripts/composites/template-digitise-review.js'`.

- [ ] **Step 3: Implement**

`scripts/composites/template-digitise-review.js`:

```js
/* ==========================================================================
   FieldSight · TemplateDigitiseReview
   --------------------------------------------------------------------------
   The draft a form was read into, beside the form itself, for a person to
   check before anything is saved (pipeline spec 2026-10-16 section 5).
   Controlled: the draft lives in the upload modal, so a Save the server
   refuses (a name already taken) comes back here with every edit intact.
   Every edit is one of the pure ops in api/template-digitise.js.

   Props:
     draft     { sections:[{title, items:[{text, flags}], flags}], flags:[] }
     sources   [{ key, url, contentType }]   presigned, from the poll
     error     string                        a refused Save, shown above the buttons
     saving    bool
     onChange  fn(draft)   onSave fn(draft)   onCancel fn

   CSS: §TEMPLATE-DIGITISE in styles/composites.css
   Exported to: window.FieldSight.TemplateDigitiseReview
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function el() { return React.createElement.apply(React, arguments); }

  function SourcePane(props) {
    return el('div', { className: 'fs-tpl-digitise__source', 'aria-label': 'Your form' },
      (props.sources || []).map(function (s, i) {
        var ct = s.contentType || '';
        if (ct.indexOf('image/') === 0) {
          return el('img', { key: s.key, className: 'fs-tpl-digitise__page', src: s.url,
                             alt: 'Page ' + (i + 1) + ' of your form' });
        }
        if (ct === 'application/pdf') {
          return el('div', { key: s.key, className: 'fs-tpl-digitise__doc' },
            el('iframe', { className: 'fs-tpl-digitise__pdf', src: s.url, title: 'Your form (PDF)' }),
            el('a', { href: s.url, target: '_blank', rel: 'noopener' }, 'Open the PDF in a new tab'));
        }
        return el('div', { key: s.key, className: 'fs-tpl-digitise__doc' },
          el('p', null, 'Word file — read straight from its tables, so there is no page picture to compare against.'),
          el('a', { href: s.url }, 'Download the Word file'));
      }));
  }

  function Flags(props) {
    var TD = window.FS.api.templateDigitise;
    var flags = props.flags || [];
    if (!flags.length) return null;
    return el('ul', { className: 'fs-tpl-digitise__flags' },
      flags.map(function (f) { return el('li', { key: f }, TD.FLAG_TEXT[f] || f); }));
  }

  function tool(label, aria, disabled, onClick) {
    return el('button', { type: 'button', className: 'fs-tpl-digitise__tool', 'aria-label': aria,
                          title: aria, disabled: disabled, onClick: onClick }, label);
  }

  function TemplateDigitiseReview(props) {
    var Button = window.FieldSight.Button;
    var TD = window.FS.api.templateDigitise;
    var ops = TD.ops;
    var draft = props.draft;
    var change = props.onChange;
    var sections = draft.sections || [];
    var total = sections.reduce(function (n, s) { return n + (s.items || []).length; }, 0);

    function sectionCard(s, i) {
      var last = i === sections.length - 1;
      return el('section', { key: i, className: 'fs-tpl-digitise__section'
                                       + ((s.flags || []).length ? ' fs-tpl-digitise__section--flagged' : '') },
        el('div', { className: 'fs-tpl-digitise__section-head' },
          el('input', { className: 'fs-tpl-upload__input', value: s.title,
                        'aria-label': 'Section ' + (i + 1) + ' name',
                        onChange: function (e) { change(ops.renameSection(draft, i, e.target.value)); } }),
          el('span', { className: 'fs-tpl-digitise__count' },
             s.items.length + (s.items.length === 1 ? ' item' : ' items'))),
        el(Flags, { flags: s.flags }),
        el('ol', { className: 'fs-tpl-digitise__items' },
          s.items.map(function (it, j) {
            return el('li', { key: j, className: 'fs-tpl-digitise__item'
                                         + ((it.flags || []).length ? ' fs-tpl-digitise__item--flagged' : '') },
              el('textarea', { className: 'fs-tpl-upload__textarea fs-tpl-digitise__item-text', rows: 1,
                               value: it.text, 'aria-label': 'Section ' + (i + 1) + ', item ' + (j + 1),
                               onChange: function (e) { change(ops.editItem(draft, i, j, e.target.value)); } }),
              el(Flags, { flags: it.flags }),
              el('div', { className: 'fs-tpl-digitise__item-tools' },
                tool('↑', 'Move item up', j === 0, function () { change(ops.moveItem(draft, i, j, -1)); }),
                tool('↓', 'Move item down', j === s.items.length - 1, function () { change(ops.moveItem(draft, i, j, 1)); }),
                tool('×', 'Delete item', false, function () { change(ops.deleteItem(draft, i, j)); })));
          })),
        el('div', { className: 'fs-tpl-digitise__section-tools' },
          el(Button, { variant: 'ghost', onClick: function () { change(ops.addItem(draft, i)); } }, '+ Add item'),
          el(Button, { variant: 'ghost', disabled: i === 0, onClick: function () { change(ops.moveSection(draft, i, -1)); } }, 'Move up'),
          el(Button, { variant: 'ghost', disabled: last, onClick: function () { change(ops.moveSection(draft, i, 1)); } }, 'Move down'),
          el(Button, { variant: 'ghost', disabled: last, onClick: function () { change(ops.mergeWithNext(draft, i)); } }, 'Merge with next'),
          el(Button, { variant: 'ghost', onClick: function () { change(ops.deleteSection(draft, i)); } }, 'Delete section')));
    }

    return el('div', { className: 'fs-tpl-digitise' },
      el('p', { className: 'fs-tpl-digitise__banner', role: 'note' }, TD.BANNER),
      el(Flags, { flags: draft.flags }),
      el('div', { className: 'fs-tpl-digitise__panes' },
        el(SourcePane, { sources: props.sources }),
        el('div', { className: 'fs-tpl-digitise__draft' },
          el('p', { className: 'fs-tpl-digitise__count' }, sections.length + ' sections · ' + total + ' items'),
          sections.map(sectionCard))),
      props.error ? el('p', { className: 'fs-tpl-upload__error', role: 'alert' }, props.error) : null,
      el('div', { className: 'fs-tpl-upload__footer' },
        el(Button, { variant: 'ghost', onClick: props.onCancel, disabled: props.saving }, 'Cancel'),
        el(Button, { variant: 'primary', disabled: props.saving,
                     onClick: function () { props.onSave(draft); } },
           props.saving ? 'Saving…' : 'Save template')));
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.TemplateDigitiseReview = TemplateDigitiseReview;
})();
```

- [ ] **Step 4: Add the styles**

Append to `styles/composites.css`:

```css
/* ==========================================================================
   §TEMPLATE-DIGITISE · the draft a form was read into, beside the form
   (template-digitise-review.js). Two panes side by side on a wide screen,
   stacked on a tablet/phone. Tokens only; checked in both themes.
   ========================================================================== */

.fs-tpl-digitise {
  width: min(1100px, 100%);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.fs-tpl-digitise__banner {
  margin: 0;
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-md);
  border: 1px solid var(--border-default);
  background: var(--surface-panel-muted);
  color: var(--text-primary);
  font-size: 13px;
}
.fs-tpl-digitise__panes {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: var(--space-4);
  max-height: 65vh;
}
@media (max-width: 900px) {
  .fs-tpl-digitise__panes { grid-template-columns: minmax(0, 1fr); max-height: none; }
}
.fs-tpl-digitise__source,
.fs-tpl-digitise__draft {
  overflow: auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.fs-tpl-digitise__page { width: 100%; height: auto; border: 1px solid var(--border-subtle); border-radius: var(--radius-md); }
.fs-tpl-digitise__pdf { width: 100%; height: 60vh; border: 1px solid var(--border-subtle); border-radius: var(--radius-md); }
.fs-tpl-digitise__doc { color: var(--text-secondary); font-size: 13px; }
.fs-tpl-digitise__count { color: var(--text-secondary); font-size: 12px; margin: 0; white-space: nowrap; }
.fs-tpl-digitise__section {
  border: 1px solid var(--border-default);
  border-radius: var(--radius-lg);
  padding: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  background: var(--surface-panel);
}
.fs-tpl-digitise__section--flagged,
.fs-tpl-digitise__item--flagged { border-color: var(--color-warning-500); }
.fs-tpl-digitise__section-head { display: flex; align-items: center; gap: var(--space-2); }
.fs-tpl-digitise__section-head .fs-tpl-upload__input { flex: 1; font-weight: var(--font-weight-semibold); }
.fs-tpl-digitise__items { margin: 0; padding-left: var(--space-5); display: flex; flex-direction: column; gap: var(--space-1-5); }
.fs-tpl-digitise__item { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: var(--space-1); align-items: start; border: 1px solid transparent; border-radius: var(--radius-md); padding: 2px; }
.fs-tpl-digitise__item .fs-tpl-digitise__flags { grid-column: 1 / -1; }
.fs-tpl-digitise__item-text { min-height: 34px; }
.fs-tpl-digitise__item-tools { display: flex; gap: 2px; }
.fs-tpl-digitise__tool {
  min-width: 32px; min-height: 32px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  background: var(--surface-input);
  color: var(--text-primary);
  cursor: pointer;
}
.fs-tpl-digitise__tool:disabled { color: var(--text-disabled); cursor: default; }
.fs-tpl-digitise__tool:focus-visible { outline: 2px solid var(--color-accent-500); outline-offset: 1px; }
.fs-tpl-digitise__flags { margin: 0; padding-left: var(--space-4); color: var(--text-warning); font-size: 12px; }
.fs-tpl-digitise__section-tools { display: flex; flex-wrap: wrap; gap: var(--space-1); }
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `node --test tests/the-digitised-draft-is-checked-before-it-is-saved.test.js`
Expected: all PASS.

- [ ] **Step 6: Load the component and the new CSS**

In `app-shell-preview.html`: change `styles/composites.css?v=141` to `styles/composites.css?v=142`; directly above the line `  <script type="text/babel" src="scripts/composites/template-upload-modal.js?v=2"></script>` add:

```html
  <script type="text/babel" src="scripts/composites/template-digitise-review.js?v=1"></script>
```

- [ ] **Step 7: Commit**

```bash
git add scripts/composites/template-digitise-review.js styles/composites.css app-shell-preview.html \
  tests/the-digitised-draft-is-checked-before-it-is-saved.test.js
git commit -m "$(cat <<'EOF'
TemplateDigitiseReview: check a read form's draft beside the form before saving

Editable sections and items (rename, edit, add, delete, reorder, merge), each
flag in plain words, item counts per section, and the banner about silent
spelling fixes. Two panes on a wide screen, stacked on a tablet or phone.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Frontend — the upload modal reads the file

**Files:**
- Modify (rewrite): `scripts/composites/template-upload-modal.js`
- Modify: `scripts/pages/library.js` (`handleUploadComplete` takes a second argument)
- Modify: `tests/the-library-says-what-it-actually-does.test.js` (replace the "nothing is read" assertions, as its own header asks)
- Modify: `app-shell-preview.html` (modal version)
- Create: `tests/the-upload-modal-reads-the-form.test.js`

**Interfaces:**
- Consumes: `window.FS.api.templateDigitise.{checkFiles, digitise, fromResult, toTemplateBody, bodyProblem, EXT_TYPES}` (Task 7), `window.FieldSight.TemplateDigitiseReview` (Task 8), `window.FS.api.templates.create` (Task 6).
- Produces: `TemplateUploadModal` props unchanged (`scope, onComplete(row, how), onCancel`); `how` is `{digitised: true}` after a reviewed save, `{readingOff: true}` after the off fallback, `undefined` for a plain create.

- [ ] **Step 1: Write the failing test**

`tests/the-upload-modal-reads-the-form.test.js`:

```js
'use strict';

/*
 * The upload modal reads the file now (spec 2026-10-16 section 5). Its phases and the
 * calls between them are pinned at the source, the way this repo pins composites: no
 * DOM harness, and the logic that can be run lives in api/template-digitise.js.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const SRC = fs.readFileSync(path.join(__dirname, '..', 'scripts', 'composites', 'template-upload-modal.js'), 'utf8')
  .replace(/\r\n/g, '\n');
const LIB = fs.readFileSync(path.join(__dirname, '..', 'scripts', 'pages', 'library.js'), 'utf8')
  .replace(/\r\n/g, '\n');

test('THE: with a file, the modal digitises; with none, it creates from starting sections', () => {
  assert.match(SRC, /TD\.digitise\(files, title\.trim\(\)/);
  assert.match(SRC, /if \(!files\.length\) return createFromStarter\(false\)/);
});

test('reading switched off falls back to today and says nothing was read', () => {
  assert.match(SRC, /if \(result\.off\) return createFromStarter\(true\)/);
  assert.match(LIB, /nothing was read from your file/);
});

test('a refused save keeps the draft: back to review with the message', () => {
  const save = SRC.slice(SRC.indexOf('async function handleSave'), SRC.indexOf('async function handleSave') + 1200);
  assert.match(save, /catch \(e\)[\s\S]*setPhase\('review'\)[\s\S]*setErr\(/);
  assert.ok(!/setDraft\(/.test(save), 'a failed save must not replace the draft');
});

test('Save sends the mapped body as a custom template, after the same checks the server makes', () => {
  assert.match(SRC, /TD\.bodyProblem\(body\)/);
  assert.match(SRC, /report_type:\s*'custom'/);
  assert.match(SRC, /body:\s*body/);
});

test('the picker takes up to ten photos, or one PDF or Word file, and no markdown or tiff', () => {
  assert.match(SRC, /multiple:\s*true/);
  assert.ok(!/'\.md'|'\.tiff'/.test(SRC));
});

test('the reading phase copy is the spec\'s', () => {
  assert.match(SRC, /Reading your form… about half a minute/);
});

test('an unreadable form shows the server\'s reason', () => {
  assert.match(SRC, /result\.reason \|\| result\.error/);
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `node --test tests/the-upload-modal-reads-the-form.test.js`
Expected: FAIL (the patterns are absent).

- [ ] **Step 3: Rewrite the modal**

Replace the whole of `scripts/composites/template-upload-modal.js` with:

```js
/* ==========================================================================
   FieldSight · TemplateUploadModal — Sprint 10 B.2, reads the file since 2026-10-16
   --------------------------------------------------------------------------
   Add a template to the library. With a file attached -- a checklist form as
   a Word file, a PDF, or a photo of each page -- the file is uploaded, read on
   the server into a DRAFT, and shown on a review screen beside the form; it
   becomes a template only when the person presses Save
   (pipeline spec docs/superpowers/specs/2026-10-16-template-digitise-design.md).

   With no file, or when reading is switched off on the server (its routes
   answer 404), the template is created as before, from the starting sections
   for its report type -- and after the off fallback the library says that
   nothing was read from the file.

   History, kept on purpose: until 2026-10-16 this modal checked the file's
   extension and threw it away, behind a spinner reading "AI is extracting
   schema". tests/the-library-says-what-it-actually-does.test.js exists because
   of that.

   Props:
     scope       'org' | 'personal'   which library to add to
     onComplete  fn(row, how)         how = {digitised:true} | {readingOff:true} | undefined
     onCancel    fn

   Phases: idle → uploading → reading → review → saving → (closed)
           idle → creating → (closed)
           any → error

   CSS: §TEMPLATE-UPLOAD + §TEMPLATE-DIGITISE in styles/composites.css
   Exported to: window.FieldSight.TemplateUploadModal
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  var REPORT_TYPES = ['daily', 'weekly', 'monthly', 'incident'];
  var ACCEPT = '.pdf,.docx,.jpg,.jpeg,.png';

  function TemplateUploadModal(props) {
    var fs           = window.FieldSight;
    var ModalOverlay = fs.ModalOverlay;
    var Button       = fs.Button;
    var Review       = fs.TemplateDigitiseReview;
    var TD           = window.FS.api.templateDigitise;
    var templates    = window.FS.api.templates;

    var scope      = props.scope      || 'personal';
    var onComplete = props.onComplete || function () {};
    var onCancel   = props.onCancel   || function () {};
    var caller = (window.AuthMock && window.AuthMock.currentUser) || {};

    var s1 = React.useState('idle');  var phase = s1[0];   var setPhase = s1[1];
    var s2 = React.useState([]);      var files = s2[0];   var setFiles = s2[1];
    var s3 = React.useState('');      var errMsg = s3[0];  var setErr = s3[1];
    var s4 = React.useState('');      var title = s4[0];   var setTitle = s4[1];
    var s5 = React.useState('');      var desc = s5[0];    var setDesc = s5[1];
    var s6 = React.useState('daily'); var rt = s6[0];      var setRt = s6[1];
    var s7 = React.useState(null);    var draft = s7[0];   var setDraft = s7[1];
    var s8 = React.useState([]);      var sources = s8[0]; var setSources = s8[1];

    function pickFiles(list) {
      var picked = Array.prototype.slice.call(list || []);
      var problem = TD.checkFiles(picked);
      if (problem) { setErr(problem); return; }
      setFiles(picked);
      setErr('');
      if (!title) setTitle(picked[0].name.replace(/\.[^.]+$/, '').replace(/[-_]/g, ' '));
    }

    async function createFromStarter(readingOff) {
      setPhase('creating');
      try {
        var row = await templates.create({
          scope: scope, report_type: rt, title: title.trim(), description: desc.trim(),
          owner_user_id: caller.device_id || caller.sub || null,
        });
        onComplete(row, readingOff ? { readingOff: true } : undefined);
      } catch (e) {
        setPhase('error');
        setErr((e && e.message) || 'Creating the template failed — please try again.');
      }
    }

    async function handleSubmit() {
      if (!title.trim()) { setErr('Please enter a template title.'); return; }
      if (!files.length) return createFromStarter(false);
      setErr('');
      try {
        var result = await TD.digitise(files, title.trim(), { onPhase: setPhase });
        if (result.off) return createFromStarter(true);
        if (result.status !== 'done') {
          setPhase('error');
          setErr(result.reason || result.error || 'The form could not be read.');
          return;
        }
        setDraft(TD.fromResult(result));
        setSources(result.sources || []);
        setPhase('review');
      } catch (e) {
        setPhase('error');
        setErr((e && e.message) || 'Upload failed — please try again.');
      }
    }

    async function handleSave(current) {
      var body = TD.toTemplateBody(current);
      var problem = TD.bodyProblem(body);
      if (problem) { setErr(problem); return; }
      setErr('');
      setPhase('saving');
      try {
        var row = await templates.create({
          scope: scope, report_type: 'custom', title: title.trim(), description: desc.trim(),
          body: body, owner_user_id: caller.device_id || caller.sub || null,
        });
        onComplete(row, { digitised: true });
      } catch (e) {
        /* Back to the draft, edits intact: a name already taken (409) is fixed by
           renaming, not by reading the form again. */
        setPhase('review');
        setErr((e && e.message) || 'Saving failed — please try again.');
      }
    }

    var inputId = 'tpl-file-input-' + scope;

    function frame(children, wide) {
      return React.createElement(ModalOverlay, { open: true, onClose: onCancel },
        React.createElement('div', { className: wide ? 'fs-tpl-upload fs-tpl-upload--wide' : 'fs-tpl-upload' },
          React.createElement('h2', { className: 'fs-tpl-upload__title' }, wide ? 'Check your form' : 'New template'),
          children));
    }

    if (phase === 'review' || phase === 'saving') {
      return frame(React.createElement(Review, {
        draft: draft, sources: sources, error: errMsg, saving: phase === 'saving',
        onChange: setDraft, onSave: handleSave, onCancel: onCancel,
      }), true);
    }

    if (phase === 'uploading' || phase === 'reading' || phase === 'creating') {
      var label = phase === 'uploading' ? 'Uploading your form…'
                : phase === 'reading'   ? 'Reading your form… about half a minute'
                : 'Creating…';
      return frame([
        React.createElement('div', { key: 'p', className: 'fs-tpl-upload__progress' },
          React.createElement('div', { className: 'fs-tpl-upload__spinner' }),
          React.createElement('p', { className: 'fs-tpl-upload__phase-label', role: 'status' }, label)),
        React.createElement('div', { key: 'f', className: 'fs-tpl-upload__footer' },
          React.createElement(Button, { variant: 'ghost', onClick: onCancel }, 'Close')),
      ]);
    }

    if (phase === 'error') {
      return frame([
        React.createElement('p', { key: 'e', className: 'fs-tpl-upload__error', role: 'alert' }, errMsg || 'Something went wrong.'),
        React.createElement('div', { key: 'f', className: 'fs-tpl-upload__footer' },
          React.createElement(Button, { variant: 'ghost', onClick: function () { setPhase('idle'); setErr(''); } }, 'Try again'),
          React.createElement(Button, { variant: 'ghost', onClick: onCancel }, 'Cancel')),
      ]);
    }

    /* idle / dragging */
    var hasFiles = files.length > 0;
    return frame([
      React.createElement('p', { key: 'sub', className: 'fs-tpl-upload__subtitle' },
        'Attach your checklist form — a Word file, a PDF, or a photo of each page — to start '
        + 'from its items, or leave it empty for a starting set of sections for the report type.'),
      React.createElement('div', {
        key: 'drop',
        className: 'fs-tpl-upload__dropzone' + (phase === 'dragging' ? ' fs-tpl-upload__dropzone--active' : '')
                   + (hasFiles ? ' fs-tpl-upload__dropzone--filled' : ''),
        onDragOver:  function (e) { e.preventDefault(); setPhase('dragging'); },
        onDragLeave: function () { setPhase('idle'); },
        onDrop:      function (e) { e.preventDefault(); setPhase('idle'); pickFiles(e.dataTransfer.files); },
        onClick:     function () { document.getElementById(inputId).click(); },
        role: 'button', tabIndex: 0,
        onKeyDown:   function (e) { if (e.key === 'Enter' || e.key === ' ') document.getElementById(inputId).click(); },
      },
        React.createElement('input', {
          id: inputId, type: 'file', style: { display: 'none' }, accept: ACCEPT, multiple: true,
          onChange: function (e) { pickFiles(e.target.files); },
        }),
        hasFiles
          ? React.createElement('div', { className: 'fs-tpl-upload__file-row' },
              React.createElement('span', { className: 'fs-tpl-upload__file-icon' }, '📄'),
              React.createElement('span', { className: 'fs-tpl-upload__file-name' },
                files.length === 1 ? files[0].name : files.length + ' pages'),
              React.createElement('button', {
                className: 'fs-tpl-upload__file-clear',
                onClick: function (e) { e.stopPropagation(); setFiles([]); setTitle(''); },
                'aria-label': 'Remove file',
              }, '×'))
          : React.createElement('div', { className: 'fs-tpl-upload__drop-hint' },
              React.createElement('span', { className: 'fs-tpl-upload__drop-icon' }, '⬆'),
              React.createElement('span', null, 'Optional: your checklist form'),
              React.createElement('span', { className: 'fs-tpl-upload__drop-types' },
                'Word (.docx) · PDF · photos (.jpg, .png), up to 10 pages — max 20 MB each'))),
      React.createElement('div', { key: 'fields', className: 'fs-tpl-upload__fields' },
        React.createElement('label', { className: 'fs-tpl-upload__field' },
          React.createElement('span', { className: 'fs-tpl-upload__label' }, 'Title ',
            React.createElement('span', { 'aria-hidden': 'true', style: { color: 'var(--color-danger-600)' } }, '*')),
          React.createElement('input', {
            className: 'fs-tpl-upload__input', value: title,
            onChange: function (e) { setTitle(e.target.value); },
            placeholder: 'e.g. Site Inspection Checklist',
          })),
        hasFiles ? null : React.createElement('div', { className: 'fs-tpl-upload__field-row' },
          React.createElement('label', { className: 'fs-tpl-upload__field' },
            React.createElement('span', { className: 'fs-tpl-upload__label' }, 'Report type'),
            React.createElement('select', {
              className: 'fs-tpl-upload__select', value: rt,
              onChange: function (e) { setRt(e.target.value); },
            }, REPORT_TYPES.map(function (v) {
              return React.createElement('option', { key: v, value: v }, v.charAt(0).toUpperCase() + v.slice(1));
            })))),
        React.createElement('label', { className: 'fs-tpl-upload__field' },
          React.createElement('span', { className: 'fs-tpl-upload__label' }, 'Description (optional)'),
          React.createElement('textarea', {
            className: 'fs-tpl-upload__textarea', rows: 2, value: desc,
            onChange: function (e) { setDesc(e.target.value); },
            placeholder: 'Brief description of this template\'s use case',
          }))),
      errMsg ? React.createElement('p', { key: 'err', className: 'fs-tpl-upload__error', role: 'alert' }, errMsg) : null,
      React.createElement('div', { key: 'foot', className: 'fs-tpl-upload__footer' },
        React.createElement(Button, { variant: 'ghost', onClick: onCancel }, 'Cancel'),
        React.createElement(Button, { variant: 'primary', onClick: handleSubmit, disabled: !title.trim() },
          hasFiles ? 'Read the form' : 'Create template')),
    ]);
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.TemplateUploadModal = TemplateUploadModal;
})();
```

If Task 0 set `PDF_ACCEPTED = False`: change `ACCEPT` to `'.docx,.jpg,.jpeg,.png'` and the drop-types line to `'Word (.docx) · photos (.jpg, .png), up to 10 pages — max 20 MB each'`, and in `scripts/api/template-digitise.js` remove `pdf` from `EXT_TYPES` (adjust the `.md` and mix assertions in `tests/template-digitise-flow.test.js` that use `a.pdf` to use `a.docx`).

Add the wide modifier to `styles/composites.css` right after the `.fs-tpl-upload { ... }` rule:

```css
.fs-tpl-upload--wide { width: min(1140px, 96vw); }
```

- [ ] **Step 4: Tell the library what happened**

In `scripts/pages/library.js`, replace:

```js
    function handleUploadComplete(stub) {
      setUploadFor(null);
      setRetry(function (n) { return n + 1; });
      setSel(stub);
      if (window.FS && window.FS.toast) {
        window.FS.toast.show({ message: 'Template created — edit its sections below', tone: 'info' });
      }
    }
```

with:

```js
    function handleUploadComplete(stub, how) {
      setUploadFor(null);
      setRetry(function (n) { return n + 1; });
      setSel(stub);
      /* Three different things happened, and the toast says which. "readingOff" is the
         server answering 404 on /templates/digitise: the file was NOT read, and saying
         so is the rule tests/the-library-says-what-it-actually-does.test.js keeps. */
      var message = how && how.readingOff
        ? 'Template created. Reading forms is not switched on here, so nothing was read from your file — edit its sections below'
        : how && how.digitised
          ? 'Template saved from your form — it is in your library now'
          : 'Template created — edit its sections below';
      if (window.FS && window.FS.toast) {
        window.FS.toast.show({ message: message, tone: 'info' });
      }
    }
```

- [ ] **Step 5: Replace the "nothing is read" assertions, as that file asks**

In `tests/the-library-says-what-it-actually-does.test.js`:

1. In the header comment, after the paragraph ending `-- not deleted.`, add:

```
 *
 * 2026-10-16: a real extraction exists (template digitise). The assertions that the
 * modal says "Nothing is read" are REPLACED below by ones that check the reading
 * claim is made only on the path that actually read the file -- the rule this file
 * exists for, unchanged.
```

2. Replace the test `the upload screen says what actually decides the sections` with:

```js
test('the reading claim is made only on the path that read the file', () => {
  /* "Reading your form" appears only in the phase entered after the server ACCEPTED
     the read (api/template-digitise.js fires onPhase('reading') after the request
     returned), and the off fallback says nothing was read. */
  assert.match(UPLOAD, /phase === 'reading'\s+\? 'Reading your form… about half a minute'/);
  const TD = read('scripts', 'api', 'template-digitise.js');
  const flow = TD.slice(TD.indexOf('async function digitise('));
  assert.ok(flow.indexOf("onPhase('reading')") > flow.indexOf('await startRead('),
    'the screen would claim to read a form before the server accepted it');
  assert.match(LIBRARY, /nothing was read from your file/);
  assert.match(UPLOAD, /starting set of sections/);
});
```

Run `node --test tests/the-library-says-what-it-actually-does.test.js` and fix any other assertion in that file that pins the old modal text, replacing it with the equivalent claim about the new text (never deleting the "no false claim" check itself).

- [ ] **Step 6: Bump the modal version**

In `app-shell-preview.html` change `scripts/composites/template-upload-modal.js?v=2` to `scripts/composites/template-upload-modal.js?v=3`, and `styles/composites.css?v=142` to `styles/composites.css?v=143`.

- [ ] **Step 7: Run every frontend test**

Run: `node --test tests/`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add scripts/composites/template-upload-modal.js scripts/pages/library.js styles/composites.css app-shell-preview.html \
  tests/the-upload-modal-reads-the-form.test.js tests/the-library-says-what-it-actually-does.test.js
git commit -m "$(cat <<'EOF'
TemplateUploadModal: read the attached form into a draft and review it before saving

With a file: upload, read, poll, then the review screen; Save sends the mapped
checklist body as a custom template and a refused save returns to the draft with
edits intact. Without a file, or with reading switched off on the server, the
template is created from starting sections and the library says nothing was read.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: Integration on TEST, and the prod hand-wiring list

**Files:** none created in the repos (the release notes text goes into the backend PR description).

- [ ] **Step 1: Re-fetch and compare against the moving base**

```bash
cd C:/Users/camil/Dropbox/fs-template-digitise && git fetch origin develop && git log --oneline origin/develop -5
git merge-base --is-ancestor origin/develop HEAD && echo up-to-date || echo "develop moved"
```

If develop moved: `git rebase origin/develop` (no stash; commit or discard first), re-run `$PYTEST -q`, and resolve conflicts in `template.yaml` / workflows by keeping both sides' lines.

- [ ] **Step 2: Full suites, numbers written down**

Backend: `$PYTEST -q` — record passed/skipped. Frontend: `node --test tests/` in the frontend worktree — record passed.

- [ ] **Step 3: Turn the switch on for TEST before the merge, so the merge's deploy carries it**

```bash
gh variable set TEST_TEMPLATE_DIGITISE --body true --repo benzn-tech/fieldsight-pipeline
gh variable list --repo benzn-tech/fieldsight-pipeline | grep TEMPLATE_DIGITISE
```

Expected: `TEST_TEMPLATE_DIGITISE true`. Do **not** set `PROD_TEMPLATE_DIGITISE`.

- [ ] **Step 4: Backend PR to develop; merge when CI is green**

```bash
cd C:/Users/camil/Dropbox/fs-template-digitise && git push -u origin feat/template-digitise
gh pr create --base develop --title "Template digitise: read a checklist form into a draft template" --body-file <file with the summary + the release notes from Step 12, ending with the Claude Code attribution line>
```

When CI passes, quote CI's pass/skip count beside the local one from Step 2. Merge (develop PRs are mine to merge; if the classifier blocks it, ask the owner). Then find the deploy run whose `headSha` is the merge commit (`gh run list --workflow deploy.yml --json headSha,status,conclusion,databaseId -L 5`) and wait for it to finish successfully. A `skipped` deploy job is not a success.

- [ ] **Step 5: Verify what was deployed, not what was merged**

```bash
export MSYS_NO_PATHCONV=1 AWS_PROFILE=fieldsight-deployer AWS_REGION=ap-southeast-2
aws lambda get-function-configuration --function-name fieldsight-test-template-digitise \
  --query "{rt:Runtime,t:Timeout,layers:Layers[].Arn,fb:Environment.Variables.LLM_FALLBACK_MODELS,m:Environment.Variables.TEMPLATE_DIGITISE_MODEL,u:Environment.Variables.QWEN_BASE_URL}"
aws lambda get-function-configuration --function-name fieldsight-test-org-api --query "Environment.Variables.TEMPLATE_DIGITISE"
aws s3api get-bucket-notification-configuration --bucket fieldsight-data-test-509194952652 \
  --query "LambdaFunctionConfigurations[?Id=='fs-template-digitise']"
aws s3api get-bucket-lifecycle-configuration --bucket fieldsight-data-test-509194952652 \
  --query "Rules[?starts_with(ID,'template')].[ID,Status,Expiration.Days]"
```

Expected: python3.12, 900, the docx layer ARN, `""`, `google/gemini-3.8-flash`, `https://openrouter.ai/api/v1`; `"true"`; one notification entry with prefix `template_digitise_requests/`; three rules Enabled, 90 days. If the trigger or lifecycle is missing, read the deploy run's "Wire" step logs before touching the bucket by hand.

- [ ] **Step 6: simulate-principal-policy on both live roles**

```bash
ORG_ROLE=$(aws lambda get-function-configuration --function-name fieldsight-test-org-api --query Role --output text)
WRK_ROLE=$(aws lambda get-function-configuration --function-name fieldsight-test-template-digitise --query Role --output text)
B=arn:aws:s3:::fieldsight-data-test-509194952652
C=c0000000-0000-0000-0000-000000000001
aws iam simulate-principal-policy --policy-source-arn "$ORG_ROLE" \
  --action-names s3:PutObject s3:GetObject \
  --resource-arns "$B/template_sources/$C/x.pdf" "$B/template_digitise_requests/$C/x.json" \
  --query "EvaluationResults[].[EvalActionName,EvalResourceName,EvalDecision]" --output table
aws iam simulate-principal-policy --policy-source-arn "$ORG_ROLE" --action-names s3:GetObject \
  --resource-arns "$B/template_digitise_results/$C/x.json" \
  --query "EvaluationResults[].[EvalActionName,EvalResourceName,EvalDecision]" --output table
for P in template_sources/ template_digitise_requests/ template_digitise_results/; do
  aws iam simulate-principal-policy --policy-source-arn "$ORG_ROLE" --action-names s3:ListBucket \
    --resource-arns "$B" --context-entries "ContextKeyName=s3:prefix,ContextKeyValues=${P}$C/x,ContextKeyType=string" \
    --query "EvaluationResults[].[EvalActionName,EvalDecision]" --output text
done
aws iam simulate-principal-policy --policy-source-arn "$WRK_ROLE" \
  --action-names s3:GetObject \
  --resource-arns "$B/template_sources/$C/x.pdf" "$B/template_digitise_requests/$C/x.json" \
  --query "EvaluationResults[].[EvalActionName,EvalResourceName,EvalDecision]" --output table
aws iam simulate-principal-policy --policy-source-arn "$WRK_ROLE" \
  --action-names s3:PutObject \
  --resource-arns "$B/template_digitise_results/$C/x.json" "$B/lake_manifest/2026/10/16/x.jsonl" \
  --query "EvaluationResults[].[EvalActionName,EvalResourceName,EvalDecision]" --output table
```

Expected: every decision `allowed`. Any `implicitDeny` is a template bug: fix, PR, redeploy, repeat.

- [ ] **Step 7: End to end through the API — the Penina PDF**

Use a TEST user's Cognito sub (Ben's TEST account; find it with `aws cognito-idp list-users --user-pool-id ap-southeast-2_q88pd6XXr --filter 'email = "benlin.chch@gmail.com"' --query "Users[].Attributes[?Name=='sub'].Value"`). All calls with `MSYS_NO_PATHCONV=1` and `AWS_PROFILE=fieldsight-deployer`, from a scratch directory (the script writes `ev.json`/`out.json` to the cwd), checking the **body's** status code, not the invoke's:

```bash
cd "C:/Users/camil/AppData/Local/Temp/claude/C--Users-camil-Dropbox/30a7129b-818e-469b-8fa3-af041f45fcf2/scratchpad"
PDF="C:/Users/camil/AppData/Local/Temp/claude/C--Users-camil-Dropbox/30a7129b-818e-469b-8fa3-af041f45fcf2/scratchpad/template-spike/penina.pdf"
SIZE=$(stat -c %s "$PDF")
python C:/Users/camil/Dropbox/fs-template-digitise/scripts/invoke_org_api.py "$SUB" POST /api/org/templates/digitise/upload-url '' \
  "{\"filename\":\"penina.pdf\",\"contentType\":\"application/pdf\",\"size\":$SIZE}"
```

Then `curl -sS -X PUT -H "Content-Type: application/pdf" --data-binary @"$PDF" "<uploadUrl from out.json>"` (expect HTTP 200), then `POST /api/org/templates/digitise` with `{"sourceKeys":["<sourceKey>"],"name":"Penina Site Inspection (TEST)"}` (expect body status 202), then poll `GET /api/org/templates/digitise/<id>` every few seconds until `status` is not `pending` (expect `done` within ~60 s). Score the result against the truth:

```bash
python -c "import json,sys; r=json.load(open('out.json')); b=json.loads(r['body']); \
print(b['status'], b['counts'], b['flags'], b['route'], b['model'])"
```

Expected: `done {'sections': 19, 'items': 115} [] vision google/gemini-3.8-flash`. Also tail the worker log once to see `docx reader: enabled` and the `qwen done:` line: `aws logs tail /aws/lambda/fieldsight-test-template-digitise --since 10m`.

- [ ] **Step 8: Repeat with one phone photo and with the Word file**

Photo: `penina_p1_photo.jpg` from the spike scratch dir (`image/jpeg`) — expect `done` with the page-1 sections only. Word: copy `C:/Users/camil/Dropbox/ADE/Penina Site Inspection Checklist.docx` to the scratch dir as `penina.docx` first (the space in the path breaks the curl line), contentType the docx type — expect `done`, `route: docx`, `model: null`, counts 19/115.

- [ ] **Step 9: Frontend PR to dev; merge; wait for Amplify**

```bash
cd C:/Users/camil/Dropbox/fsui-template-digitise && git fetch origin dev && git rebase origin/dev && node --test tests/
git push -u origin feat/template-digitise
gh pr create --base dev --title "Library: read a checklist form into a draft template" --body "<summary; the repo has no CI -- node --test tests/ counts from Step 2>"
```

Merge, then confirm the Amplify job for `dev` on app `d2fssznicvuckr` succeeded for the merge commit: `aws amplify list-jobs --app-id d2fssznicvuckr --branch-name dev --max-items 3 --profile fieldsight-deployer --region ap-southeast-2`.

- [ ] **Step 10: Browser check on dev**

Load `Skill(claude-in-chrome)` first. Open `https://dev.d2fssznicvuckr.amplifyapp.com` and, before anything else, read in the console `window.FS.api.orgBaseUrl` — it must be the TEST gateway (`wdsgobb7b0...`). If it points at prod (`ys94qy2tk0...`), stop: the dev frontend is wired to prod and nothing here may be tested through it.
Then: Library → Upload → attach `penina.pdf` → Read the form → review screen shows 19 sections / 115 items, the banner, the PDF beside the draft → edit one item, merge nothing → Save. In the network panel, the `POST .../org/templates` request body has `report_type: "custom"` and `body.sections[*].items` as strings (115 in total). The template opens in the Library with checklist sections. Repeat with two photos (`penina_p1_photo.jpg`, `penina_p2_photo.jpg`). Check the review screen in light and dark themes with transitions off and the banner/flag text contrast measured (not eyeballed). Resize to 820 px wide (iPad) and 390 px (iPhone): panes stack, nothing scrolls sideways.

- [ ] **Step 11: What is left for the owner**

- A spoken check matched to the digitised template (spec's TEST e2e, last step) needs a device recording on TEST: list it for the owner with the template's name.
- Delete the TEST templates created by Steps 7-10 only if the owner asks.

- [ ] **Step 12: Prod hand-wiring list (release notes — NOT executed)**

Put this text in the backend PR description and in the release PR when this goes to `main`:

```
Template digitise -- prod steps at release (owner-approved release only)

1. Trigger: nothing by hand. deploy-prod.yml runs wire-s3-events.sh --apply on
   fieldsight-data-509194952652 because PROD_WIRE_LAKE=true. After the prod deploy,
   verify: aws s3api get-bucket-notification-configuration --bucket fieldsight-data-509194952652
   --query "LambdaFunctionConfigurations[?Id=='fs-template-digitise']"

2. Lifecycle (by hand -- wire-bucket-lifecycle.sh refuses on prod because of
   DeleteOldTranscripts/CleanupPendingDownloads, and put REPLACES the whole config):
     aws s3api get-bucket-lifecycle-configuration --bucket fieldsight-data-509194952652 > current.json
     # append to Rules:
     #  {"ID":"template-sources-expiry","Status":"Enabled","Filter":{"Prefix":"template_sources/"},"Expiration":{"Days":90}}
     #  {"ID":"template-digitise-requests-expiry","Status":"Enabled","Filter":{"Prefix":"template_digitise_requests/"},"Expiration":{"Days":90}}
     #  {"ID":"template-digitise-results-expiry","Status":"Enabled","Filter":{"Prefix":"template_digitise_results/"},"Expiration":{"Days":90}}
     aws s3api put-bucket-lifecycle-configuration --bucket fieldsight-data-509194952652 --lifecycle-configuration file://current.json
     # re-read and diff against current.json: every pre-existing rule still present.

3. CORS (by hand -- the prod bucket allows only GET/HEAD; this is the first browser->S3
   PUT on prod). NEVER run scripts/wire-bucket-cors.sh on prod: it replaces the whole
   config and would delete amplify-read-for-canvas.
     aws s3api get-bucket-cors --bucket fieldsight-data-509194952652 > cors.json
     # add a SECOND rule, leaving amplify-read-for-canvas untouched:
     #  {"ID":"amplify-template-upload","AllowedOrigins":["https://main.d2fssznicvuckr.amplifyapp.com"
     #     (+ any custom domain from: aws amplify list-domain-associations --app-id d2fssznicvuckr)],
     #   "AllowedMethods":["PUT"],"AllowedHeaders":["Content-Type"],"MaxAgeSeconds":3000}
     aws s3api put-bucket-cors --bucket fieldsight-data-509194952652 --cors-configuration file://cors.json
     # re-read: both rules present.

4. Switch: PROD_TEMPLATE_DIGITISE=true (repo variable), then a prod deploy. Until then
   the routes answer 404 and the Library behaves exactly as before.

5. After deploy: simulate-principal-policy on fieldsight-prod-org-api and
   fieldsight-prod-template-digitise for the same actions as TEST (Task 10 Step 6),
   with the prod bucket ARN. PROD_DOCX_LAYER_ARN is already set (python-docx-layer:3).
```

---

## Self-review (done while writing; kept for the reviewer)

- **Spec coverage.** Upload (§1) → Task 5 upload-url + Task 7 client; request (§2) incl. HEAD size, company prefix, lake_objects kinds → Task 5; worker (§3) incl. docx/no-layer/no-table, vision prompt verbatim, own chain, validation, flags, result shape → Tasks 0-3; poll (§4) incl. presigned sources, company scope, 7-day cut-off → Task 5; review screen (§5) incl. banner, flags in words, counts, edit/merge/reorder, Save mapping, `create()` accepting sections → Tasks 6-9; error handling (wrong type 400 before any write, worker error result, zero items unreadable, 3-minute give-up, lifecycle) → Tasks 3, 4, 5, 7; testing (real forms, flags, HTTP-boundary stubs, handler from S3 event, org-api through fake s3, node tests, TEST e2e) → every task + Task 10; rollout (function, trigger, IAM incl. ListBucket, flag default off, CORS, spike dir without keys, release notes) → Tasks 4, 5, 10 and Task 0 Step 15.
- **Names checked across tasks.** `call_llm_parts`, `template_types.{EXT_TYPES, accepted_exts, ext_of, source_shape_error, SHAPE_ERROR, MAX_BYTES, MAX_IMAGES, MAX_PAGES, PDF_ACCEPTED}`, `template_vision.{check, read, build_parts, pdf_page_count, model}`, `template_docx.{AVAILABLE, read}`, `template_draft.clean`, result keys and the draft shape `{title, items: [{text, flags}], flags}` are the same in Tasks 0-9; the frontend reads `sources[].{key, url, contentType}` exactly as Task 5 writes them; `templates.DEFAULT_CATCH_ALL` (Task 6) is what Task 7 reads.
- **Review Focus.** Five lines, each pinned by a named test in its owning task.
