# Template Agent — Phase 0 (Spike) + Phase 1 (Intake + Mapping) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** De-risk LandingAI ADE on real client templates (Phase 0 spike), then ship template *intake* — upload a client report template to S3, analyze its structure with ADE, have Claude propose a field-mapping to the FieldSight item store, and let a human review/confirm/freeze that mapping — so a stored, human-confirmed `report_template` exists to fill from later.

**Architecture:** A client uploads a `.docx`/PDF via a presigned PUT into `report-templates/` (org-api, in-VPC, mirrors `create_recording_upload_url`). A non-VPC `AnalyzeTemplateFunction` (mirrors `ExtractSessionFunction`) downloads the artifact, calls ADE (external HTTPS) to get a visually-grounded structure, normalizes it, then calls Claude (`claude_utils`) to propose a slot→source field-mapping, and hands the result to an in-VPC `TemplateWriterFunction` (mirrors `SuggestionWriterFunction`) that persists `ade_schema` + proposed `field_mapping` to a new Aurora `report_template` table. The mapping-review UI overlays ADE bounding boxes on the template and lets the user edit/confirm; confirmation freezes the mapping in Aurora. **This plan STOPS at the confirmed mapping — no fill, no render (Phase 2).**

**Tech Stack:** Python 3.11/3.12, psycopg3 (`dict_row`, `Jsonb`), AWS SAM (CloudFormation), Aurora PostgreSQL 16, Lambda, `landing-ai/ade-python` (ADE DPT-2) as a Lambda layer, Anthropic Claude via `claude_utils` (urllib3), boto3 (S3 presign + async Lambda invoke), pytest (unit = DB-free monkeypatch; integration = `pgvector/pgvector:pg16` service container), region ap-southeast-2, account 509194952652. UI = single-file browser React prototype (no build step; `node --check` gate).

## Global Constraints

- **ADE + Claude are external HTTPS → NON-VPC.** `AnalyzeTemplateFunction` runs outside the VPC (mirrors `ExtractSessionFunction`/`ReportGeneratorFunction`, which already have egress). No `Condition` — it deploys even without `HasDb`.
- **In-VPC Lambdas ONLY read/write Aurora — never make an outbound call (BUG-36).** `TemplateWriterFunction` and `OrgApiFunction` reuse the EXACT `VpcConfig` (`DbSubnetIds` + `!ImportValue ${DbStackName}-LambdaSG`) + deploy-time `PGHOST/PGDATABASE/PGUSER/PGPASSWORD` env (PGPASSWORD is a CloudFormation `{{resolve:secretsmanager:...}}` deploy-time reference, NEVER a runtime Secrets Manager call). Persisting ADE output to Aurora is done by the in-VPC writer, never by the non-VPC analyzer.
- **ADE key is a SAM secret parameter.** New `AdeApiKey` parameter (`NoEcho: true`, `Default: ''`), threaded to `AnalyzeTemplateFunction`'s `ADE_API_KEY` env EXACTLY as `ClaudeApiKey`→`ANTHROPIC_API_KEY` is threaded today. Supplied from a GitHub Actions secret in the deploy workflows.
- **Data governance:** uploading a client template (a filled sample may carry client content) sends it to LandingAI. One-time per template. Surface this to the user before enabling for a company (spec §5). The plan adds no auto-enable; upload is an explicit user action.
- **`uv run pytest` is the gate** for pipeline code. Unit tests are DB-free (monkeypatch) and run anywhere; integration tests require `TEST_DATABASE_URL` and run under the `test.yml` CI harness. The Windows dev host has **no local Python (BUG-29)** — run pipeline tests in CI or a Linux/devcontainer shell.
- **UI gate = `node --check` on every modified `.js` + bump `?v=N` cache-busters** in the loaded preview HTML. No npm/webpack/vite (no build step). Register any new composite in `components-preview.html`.
- **New dev artifacts (comments/commits/docs) are ENGLISH** per the 2026-07-15 rule.
- **Branch off `develop`; never `git add -A`** on pipeline `develop` (Windows CRLF trap — stage explicit paths only). This plan is executed on a feature branch cut from `develop`.
- **Migrations** are applied via `aws lambda invoke fieldsight-{test,prod}-migrate` AFTER `sam deploy` (idempotent; test + prod share ONE Aurora cluster + ONE `schema_migrations` ledger).
- **Migration number is 0019.** The highest existing migration is `src/migrations/0017_action_item_audit.sql`. **0018 is reserved by the sibling site-location-weather plan** (`0018_site_coordinates.sql`), which ships first — so this template plan takes **0019**. If site-location-weather has NOT merged when this executes, re-verify with `ls src/migrations/` and take the next free number (still avoid 0018 if that plan is in flight).

---

## ADE response shape — ASSUMED interface (pin after Phase 0 spike)

ADE's exact Python return shape is unknown until Phase 0 runs. Every task below codes against a **normalized internal schema we own**, produced by an adapter (`ade_client._normalize`). Only that one adapter function is pinned to ADE's real shape after the spike — everything downstream (storage, mapping agent, UI) consumes the stable normalized shape.

**Normalized `ade_schema` (stored in `report_template.ade_schema` JSONB):**
```json
{
  "ade_version": "dpt-2",
  "page_count": 3,
  "slots": [
    {
      "slot_id": "s1",
      "kind": "heading",
      "text": "Site Progress Summary",
      "page": 1,
      "box": {"l": 0.10, "t": 0.20, "r": 0.90, "b": 0.25},
      "confidence": 0.94,
      "table": null
    },
    {
      "slot_id": "s4",
      "kind": "table",
      "text": "Open Actions",
      "page": 1,
      "box": {"l": 0.10, "t": 0.55, "r": 0.92, "b": 0.80},
      "confidence": 0.88,
      "table": {"n_rows": 5, "n_cols": 4, "headers": ["Action", "Owner", "Due", "Status"]}
    }
  ]
}
```
- `kind` ∈ `{"heading","paragraph","table","field"}` (normalized from ADE's chunk types).
- `box` is normalized 0..1 page coordinates (left/top/right/bottom) — the UI overlay math assumes this.
- `table` is `null` unless `kind == "table"`.
- `confidence` is a float 0..1 (default `0.5` if ADE omits it — the spike confirms whether ADE returns confidence at all).

**ASSUMED ADE raw shape that `_normalize` consumes** (from spec §3.2 stage 1 — "chunks typed as heading/paragraph/table/field, with page + coordinates + confidence"):
```python
# result.chunks: list; each chunk has:
#   chunk.chunk_type -> str   e.g. "title"|"section_header"|"text"|"table"|"form_field"
#   chunk.text       -> str
#   chunk.grounding  -> list of { "page": int, "box": {"l","t","r","b"} }  (normalized 0..1)
#   chunk.confidence -> float | None
#   chunk.table      -> { "num_rows", "num_cols", "headers": [str] } | None
```
**Spike outputs that pin this (Phase 0):** (a) the real module/import + call signature; (b) the real chunk-type vocabulary → our 4 `kind`s mapping table; (c) whether `grounding.box` is normalized 0..1 or pixel (and page size if pixel); (d) whether `confidence` exists; (e) whether ADE returns per-page preview images (drives the UI overlay strategy).

---

## File Structure

**New files (pipeline `C:/Users/camil/Dropbox/fieldsight-pipeline`):**
- `docs/superpowers/spikes/2026-07-19-ade-spike-notes.md` — Phase 0 findings (created by the spike).
- `scripts/ade_spike.py` — throwaway Phase 0 script that runs a real `.docx`/PDF through ADE and dumps the response.
- `src/migrations/0019_report_template.sql` — `report_template` table.
- `src/repositories/report_templates.py` — CRUD + status + analysis + mapping-confirm (psycopg3, `dict_row`).
- `src/ade_client.py` — ADE call + `_normalize(raw) -> ade_schema` adapter (the one spike-pinned unit).
- `src/template_mapping.py` — `DATA_SOURCES` catalogue (§3.3) + `build_mapping_prompt` + `parse_mapping` + `propose_mapping` (Claude via `claude_utils`).
- `src/lambda_analyze_template.py` — non-VPC: S3 get → ADE → Claude mapping → invoke writer.
- `src/lambda_template_writer.py` — in-VPC: persist `ade_schema` + `field_mapping` to `report_template`.
- `infra/ade-layer/requirements.txt` — `ade-python` Lambda layer (mirrors `infra/dashscope-layer/`).
- `tests/integration/test_report_templates_repo.py` — repo integration tests.
- `tests/unit/test_report_templates_repo_unit.py` — repo unit tests (FakeConn, no DB).
- `tests/unit/test_ade_client.py` — `_normalize` unit tests against the assumed raw shape.
- `tests/unit/test_template_mapping.py` — mapping prompt + parse/validate unit tests.
- `tests/unit/test_lambda_template_writer.py` — writer handler unit tests.
- `tests/unit/test_lambda_analyze_template.py` — analyzer handler unit tests.
- `tests/unit/test_org_api_templates.py` — org-api template-route unit tests.

**Modified files (pipeline):**
- `src/lambda_org_api.py` — import `report_templates`; add `report-templates/` presign + 5 routes/handlers (`POST /templates`, `GET /templates`, `GET /templates/{id}`, `POST /templates/{id}/analyze`, `PATCH /templates/{id}/mapping`) + `_template_s3_key`.
- `src/template.yaml` — `AdeApiKey` param; `AdeLayer`; `AnalyzeTemplateFunction` (non-VPC); `TemplateWriterFunction` (in-VPC, `HasDb`); org-api `report-templates/*` S3 grant + `ANALYZE_TEMPLATE_FUNCTION`/`REPORT_TEMPLATES_PREFIX` env + `lambda:InvokeFunction` grant.
- `.github/workflows/deploy.yml` + `.github/workflows/deploy-prod.yml` — pass `AdeApiKey=${{ secrets.ADE_API_KEY }}`.
- `.github/workflows/test.yml` — (no change needed; ADE lib is not imported by unit tests — see Task 6 `importorskip`).

**New/modified files (UI `C:/Users/camil/Dropbox/fieldsight-ui`):**
- `scripts/api/templates-store.js` — swap the localStorage `create`/`get`/`list`/`updateSchema` surface to the real org endpoints when `orgLive()`/`orgWrite()`, keeping mocks as fallback. (The existing `scripts/api/template-store.js` localStorage mock stays as the offline fallback; this new real client is loaded alongside it.)
- `scripts/composites/template-mapping-review.js` — NEW composite: ADE bounding-box overlay + editable mapping rows + confirm button.
- `styles/components.css` — `.fs-tpl-review*` styles for the overlay.
- `components-preview.html` + `app-shell-preview.html` — register the composite + bump `?v=N` cache-busters.

---

## Phase 0 — ADE Spike (investigative, NOT TDD; throwaway)

**Goal:** Prove ADE produces a usable, visually-grounded structure on REAL client templates before building the Phase 1 pipeline. Decide go/no-go and which formats are primary vs fallback. Pin the `_normalize` adapter mapping.

**Prerequisite the HUMAN must supply (call this out and block until provided):**
1. An **ADE free-tier API key** (LandingAI ADE / DPT-2; 1000 free credits, credits = pages×0.5).
2. **1 real client `.docx`** template (blank template or filled sample).
3. **1 real client PDF** template.
Place the two sample files somewhere the spike script can read them and export `ADE_API_KEY`. **Do not commit the client samples** (they may carry client content — data-governance).

> Environment note (BUG-29): the Windows dev host has no local Python. Run the spike in CI, a Linux shell, or a devcontainer where Python + pip are available. `.docx`/PDF sample paths are passed as CLI args; nothing is committed except the notes doc + the (sample-free) script.

Steps (investigative — run, observe, write down; no test cycle):

- [ ] **Step 1 — Create a throwaway venv and install the ADE library.**
  ```bash
  python -m venv /tmp/ade-spike && . /tmp/ade-spike/bin/activate
  pip install ade-python
  python -c "import importlib.metadata as m; print('ade-python', m.version('ade-python'))"
  ```
  Record the exact installed version + the top-level import name(s) (`python -c "import ade_python"` or whatever the wheel actually exposes) in the notes doc. **This resolves the real module name that `src/ade_client.py` must import.**

- [ ] **Step 2 — Write `scripts/ade_spike.py`** (throwaway; committed sample-free so the run is reproducible):
  ```python
  """Phase 0 throwaway: run one document through LandingAI ADE (DPT-2) and dump
  the raw response so we can pin src/ade_client._normalize to the real shape.
  Usage: ADE_API_KEY=... python scripts/ade_spike.py /path/to/template.docx
  NOTE: never commit client sample files — pass them as CLI args at run time."""
  import json
  import os
  import sys

  def main(path: str) -> None:
      api_key = os.environ["ADE_API_KEY"]
      # PIN AFTER SPIKE: the import + call below is the ASSUMED ade-python surface.
      # Replace with the real one discovered in Step 1.
      from ade_python import Client  # assumed; correct in Step 1
      client = Client(api_key=api_key)
      with open(path, "rb") as fh:
          result = client.parse(fh.read(), filename=os.path.basename(path))
      # Dump everything we can see so the shape is fully captured.
      print("TYPE:", type(result))
      print("ATTRS:", [a for a in dir(result) if not a.startswith("_")])
      try:
          print(json.dumps(result.model_dump(), indent=2, default=str)[:20000])
      except Exception:
          print("RAW REPR:", repr(result)[:20000])

  if __name__ == "__main__":
      main(sys.argv[1])
  ```

- [ ] **Step 3 — Run it on the `.docx`, then the PDF.** Capture full stdout to the notes doc:
  ```bash
  ADE_API_KEY=$ADE_API_KEY python scripts/ade_spike.py "$DOCX_SAMPLE"
  ADE_API_KEY=$ADE_API_KEY python scripts/ade_spike.py "$PDF_SAMPLE"
  ```

- [ ] **Step 4 — Inspect and record in `docs/superpowers/spikes/2026-07-19-ade-spike-notes.md`:**
  - The exact **chunk type vocabulary** ADE emits, and the mapping to our 4 `kind`s (`heading`/`paragraph`/`table`/`field`). This becomes `ade_client._KIND_MAP`.
  - The **grounding/box format**: normalized 0..1 vs pixel; if pixel, the page dimensions (needed to normalize in `_normalize`).
  - Whether a **confidence** score is present per chunk (and its range).
  - Whether ADE returns **table structure** (rows/cols/headers) and how.
  - Whether ADE returns **per-page preview images** (drives the UI overlay: real page raster vs boxes-on-blank-canvas).
  - **Per-format quality:** did tables/headings parse cleanly on the `.docx`? on the PDF? Which is trustworthy?

- [ ] **Step 5 — Go/No-Go decision gate (write the verdict in the notes doc):**
  - **GO** if at least one format (expected: `.docx`) yields headings + tables with usable grounding. Record which format is **primary** and which is **fallback-only**.
  - **STOP AND REASSESS WITH THE HUMAN** if ADE quality is poor for BOTH formats (garbled structure, no grounding, or no table detection). Do NOT start Phase 1 in that case — the entire feature rests on ADE structure quality (spec §5). Bring the notes doc to the human and decide whether to (a) try ADE paid tier / different DPT settings, (b) narrow to `.docx`-only, or (c) shelve.

- [ ] **Step 6 — Commit the notes + script (NOT the samples):**
  ```bash
  git add scripts/ade_spike.py docs/superpowers/spikes/2026-07-19-ade-spike-notes.md
  git commit -m "spike: ADE DPT-2 structure quality on real client templates (Phase 0)"
  ```

**After Phase 0:** update the "ADE response shape" section above + the spike-pin flags in Task 3 / Task 6 / Task 11 with the real findings before implementing them.

---

## Phase 1 — Template Intake + Mapping Review

### Task 1: Migration `0019_report_template.sql`

**Files:**
- Create: `src/migrations/0019_report_template.sql`
- Test: `tests/integration/test_report_templates_repo.py` (initial smoke; grows in Task 2)

**Interfaces:**
- Consumes: `db.migrate.apply_migrations` (existing runner — picks up `*.sql`, orders by integer prefix).
- Produces: table `report_template` with columns `id, company_id, site_id, title, original_s3_key, original_format, status, ade_schema, field_mapping, error_note, created_by, confirmed_by, confirmed_at, created_at, updated_at`; indexes `idx_report_template_company`, `idx_report_template_site`.

- [ ] **Step 1: Write the failing integration smoke test** `tests/integration/test_report_templates_repo.py`:
```python
import pytest

pytestmark = pytest.mark.integration

from repositories import companies


def test_report_template_table_exists(db):
    # 0019 creates report_template; a bare INSERT/SELECT proves the schema
    # (company_id FK -> companies, so seed a company first).
    co = companies.create_company(db, "Acme", industry="construction")
    row = db.execute(
        "INSERT INTO report_template (company_id, title, original_s3_key, original_format) "
        "VALUES (%s, 'Daily', 'report-templates/x/original.docx', 'docx') "
        "RETURNING id, status, ade_schema, field_mapping, created_at",
        (co["id"],),
    ).fetchone()
    assert row[0] is not None       # id
    assert row[1] == "uploaded"     # status default
    assert row[2] is None and row[3] is None  # ade_schema / field_mapping null until analyze
    assert row[4] is not None       # created_at default
```

- [ ] **Step 2: Run it, expect FAIL** (`psycopg.errors.UndefinedTable: relation "report_template" does not exist`):
  `uv run pytest tests/integration/test_report_templates_repo.py -v`
  Expected: FAIL (UndefinedTable). (Requires `TEST_DATABASE_URL`; in CI via `test.yml`.)

- [ ] **Step 3: Create `src/migrations/0019_report_template.sql`:**
```sql
-- 0019: report_template — one reusable client-format report template
-- (Template Agent, Phase 1). Stores the uploaded artifact's S3 key, the
-- normalized ADE structure (ade_schema), and the human-confirmed field
-- mapping (field_mapping) — analyze once, confirm mapping once, reuse every
-- period (spec docs/superpowers/specs/2026-07-19-template-agent-design.md
-- §3.1). company_id FK makes it tenant-scoped; site_id is an OPTIONAL
-- narrower scope (NULL = all company sites). ade_schema/field_mapping are
-- jsonb, same convention as findings.impact_evidence / topics.participants.
CREATE TABLE report_template (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id      uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  site_id         uuid REFERENCES sites(id) ON DELETE SET NULL,
  title           text NOT NULL,
  original_s3_key text NOT NULL,
  original_format text NOT NULL CHECK (original_format IN ('docx', 'pdf')),
  status          text NOT NULL DEFAULT 'uploaded'
                    CHECK (status IN ('uploaded','analyzing','mapping_review','confirmed','error')),
  ade_schema      jsonb,
  field_mapping   jsonb,
  error_note      text,
  created_by      text,
  confirmed_by    text,
  confirmed_at    timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_report_template_company ON report_template (company_id);
CREATE INDEX idx_report_template_site ON report_template (site_id) WHERE site_id IS NOT NULL;
```

- [ ] **Step 4: Run it, expect PASS:**
  `uv run pytest tests/integration/test_report_templates_repo.py -v`
  Expected: PASS.

- [ ] **Step 5: Commit:**
```bash
git add src/migrations/0019_report_template.sql tests/integration/test_report_templates_repo.py
git commit -m "Add 0019_report_template migration: report_template table"
```

---

### Task 2: `repositories/report_templates.py`

**Files:**
- Create: `src/repositories/report_templates.py`
- Test: `tests/unit/test_report_templates_repo_unit.py` (FakeConn, DB-free) + extend `tests/integration/test_report_templates_repo.py` (real DB).

**Interfaces:**
- Consumes: `psycopg.rows.dict_row`, `psycopg.types.json.Jsonb`, `psycopg` (for `psycopg.Error` guard), `repositories.companies.create_company` (integration seed).
- Produces:
  - `create_template(conn, company_id, title, original_s3_key, original_format, *, site_id=None, created_by=None) -> dict`
  - `get_template(conn, template_id) -> dict | None` (None on missing or malformed uuid)
  - `list_templates(conn, company_id) -> list[dict]`
  - `set_status(conn, template_id, status, *, error_note=None) -> dict | None`
  - `set_analysis(conn, template_id, ade_schema: dict, field_mapping: dict) -> dict | None` (sets ade_schema+field_mapping, `status='mapping_review'`, `updated_at=now()`)
  - `confirm_mapping(conn, template_id, company_id, field_mapping: dict, confirmed_by) -> dict | None` (tenant-guarded: `WHERE id=%s AND company_id=%s`; sets field_mapping, `status='confirmed'`, confirmed_by, `confirmed_at=now()`, `updated_at=now()`)

- [ ] **Step 1: Write the failing unit tests** `tests/unit/test_report_templates_repo_unit.py`:
```python
"""Unit tests for src/repositories/report_templates.py (Template Agent Phase 1).
FakeConn/FakeCursor record every execute()'s SQL + params so behaviour is
asserted without a real Postgres (mirrors tests/unit/test_findings_repo.py)."""
from psycopg.types.json import Jsonb

from repositories import report_templates as repo


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def execute(self, sql, params=None):
        self.conn.calls.append({"sql": sql, "params": params})
        self._rows = self.conn._pop_result()
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, results=None):
        self.calls = []
        self._results = list(results or [])

    def _pop_result(self):
        return self._results.pop(0) if self._results else []

    def cursor(self, row_factory=None):
        return FakeCursor(self)


def _row(**over):
    r = {"id": "t-1", "company_id": "c-1", "site_id": None, "title": "Daily",
         "original_s3_key": "report-templates/c-1/abc/original.docx",
         "original_format": "docx", "status": "uploaded", "ade_schema": None,
         "field_mapping": None, "error_note": None, "created_by": "sub-1",
         "confirmed_by": None, "confirmed_at": None,
         "created_at": "2026-07-19T00:00:00Z", "updated_at": "2026-07-19T00:00:00Z"}
    r.update(over)
    return r


def test_create_template_inserts_and_returns_row():
    conn = FakeConn(results=[[_row()]])
    out = repo.create_template(conn, "c-1", "Daily",
                               "report-templates/c-1/abc/original.docx", "docx",
                               created_by="sub-1")
    assert out == _row()
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "INSERT INTO report_template" in sql and "RETURNING" in sql
    assert params == ("c-1", None, "Daily",
                      "report-templates/c-1/abc/original.docx", "docx", "sub-1")


def test_set_analysis_jsonb_wrapped_and_status_review():
    conn = FakeConn(results=[[_row(status="mapping_review")]])
    ade = {"slots": [{"slot_id": "s1"}]}
    mapping = {"version": 1, "mappings": []}
    out = repo.set_analysis(conn, "t-1", ade, mapping)
    assert out["status"] == "mapping_review"
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "UPDATE report_template SET" in sql
    assert "status='mapping_review'" in sql and "updated_at=now()" in sql
    jsonb = [p for p in params if isinstance(p, Jsonb)]
    assert len(jsonb) == 2 and jsonb[0].obj == ade and jsonb[1].obj == mapping
    assert params[-1] == "t-1"  # WHERE id


def test_confirm_mapping_is_tenant_guarded():
    conn = FakeConn(results=[[_row(status="confirmed", confirmed_by="sub-1")]])
    out = repo.confirm_mapping(conn, "t-1", "c-1", {"version": 1, "mappings": []}, "sub-1")
    assert out["status"] == "confirmed"
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "WHERE id=%s AND company_id=%s" in sql
    assert "status='confirmed'" in sql and "confirmed_at=now()" in sql
    # last two params are the WHERE keys: id then company_id
    assert params[-2] == "t-1" and params[-1] == "c-1"


def test_get_template_bad_uuid_returns_none():
    class Boom(FakeConn):
        def cursor(self, row_factory=None):
            raise _pgerror()
    out = repo.get_template(Boom(), "not-a-uuid")
    assert out is None


def _pgerror():
    import psycopg
    return psycopg.Error("invalid input syntax for type uuid")
```

- [ ] **Step 2: Run it, expect FAIL** (`ModuleNotFoundError: repositories.report_templates`):
  `uv run pytest tests/unit/test_report_templates_repo_unit.py -v`
  Expected: FAIL (module missing).

- [ ] **Step 3: Create `src/repositories/report_templates.py`:**
```python
"""Repository for report_template (migration 0019) — Template Agent Phase 1.

One reusable client-format report template: uploaded artifact S3 key +
normalized ADE structure (ade_schema) + human-confirmed field mapping
(field_mapping) + tenant/site scope. Style mirrors
src/repositories/findings.py / action_items.py (module-level SQL,
conn.cursor(row_factory=dict_row).execute(...).fetchone()/.fetchall(),
jsonb bound via Jsonb() per chunks.py). Repositories NEVER commit — the
caller owns the transaction (repositories/__init__.py)."""
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

_COLS = ("id, company_id, site_id, title, original_s3_key, original_format, "
         "status, ade_schema, field_mapping, error_note, created_by, "
         "confirmed_by, confirmed_at, created_at, updated_at")


def create_template(conn, company_id, title, original_s3_key, original_format,
                    *, site_id=None, created_by=None) -> dict:
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO report_template (company_id, site_id, title, original_s3_key, "
        f"original_format, created_by) VALUES (%s,%s,%s,%s,%s,%s) RETURNING {_COLS}",
        (company_id, site_id, title, original_s3_key, original_format, created_by),
    ).fetchone()


def get_template(conn, template_id) -> dict | None:
    """One template row, or None if not found / template_id is not a valid UUID
    (malformed id == missing, 404 semantics — mirrors action_items.get_action_item)."""
    try:
        return conn.cursor(row_factory=dict_row).execute(
            f"SELECT {_COLS} FROM report_template WHERE id=%s", (template_id,)
        ).fetchone()
    except psycopg.Error:
        conn.rollback()
        return None


def list_templates(conn, company_id) -> list[dict]:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM report_template WHERE company_id=%s "
        f"ORDER BY created_at DESC",
        (company_id,),
    ).fetchall()


def set_status(conn, template_id, status, *, error_note=None) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE report_template SET status=%s, error_note=%s, updated_at=now() "
        f"WHERE id=%s RETURNING {_COLS}",
        (status, error_note, template_id),
    ).fetchone()


def set_analysis(conn, template_id, ade_schema: dict, field_mapping: dict) -> dict | None:
    """Persist the ADE-normalized structure + the agent-proposed mapping and
    move the template into human review. status forced to 'mapping_review'
    (the one human-in-the-loop gate). Both jsonb bound via Jsonb (chunks.py
    convention). None if the row vanished (rowcount 0), never raises."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE report_template SET ade_schema=%s, field_mapping=%s, "
        f"status='mapping_review', updated_at=now() WHERE id=%s RETURNING {_COLS}",
        (Jsonb(ade_schema), Jsonb(field_mapping), template_id),
    ).fetchone()


def confirm_mapping(conn, template_id, company_id, field_mapping: dict,
                    confirmed_by) -> dict | None:
    """Freeze the human-confirmed mapping. Tenant-guarded (WHERE id AND
    company_id) so a caller can only confirm their own company's template —
    None on missing/cross-tenant (rowcount 0). WHERE params are id then
    company_id (asserted by the unit test)."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE report_template SET field_mapping=%s, status='confirmed', "
        f"confirmed_by=%s, confirmed_at=now(), updated_at=now() "
        f"WHERE id=%s AND company_id=%s RETURNING {_COLS}",
        (Jsonb(field_mapping), confirmed_by, template_id, company_id),
    ).fetchone()
```

- [ ] **Step 4: Run the unit tests, expect PASS:**
  `uv run pytest tests/unit/test_report_templates_repo_unit.py -v`
  Expected: PASS.

- [ ] **Step 5: Add integration tests** to `tests/integration/test_report_templates_repo.py` (real round-trip):
```python
from repositories import report_templates as repo


def test_create_get_list_and_confirm_roundtrip(db):
    co = companies.create_company(db, "Beta Build", industry="construction")
    t = repo.create_template(db, co["id"], "Weekly",
                             "report-templates/beta/w/original.pdf", "pdf",
                             created_by="sub-9")
    assert t["status"] == "uploaded" and t["original_format"] == "pdf"

    got = repo.get_template(db, t["id"])
    assert got["id"] == t["id"]

    analyzed = repo.set_analysis(db, t["id"], {"slots": [{"slot_id": "s1"}]},
                                 {"version": 1, "mappings": []})
    assert analyzed["status"] == "mapping_review"
    assert analyzed["ade_schema"] == {"slots": [{"slot_id": "s1"}]}

    # cross-tenant confirm is refused (rowcount 0 -> None)
    assert repo.confirm_mapping(db, t["id"], "00000000-0000-0000-0000-000000000000",
                                {"version": 1, "mappings": []}, "sub-9") is None

    ok = repo.confirm_mapping(db, t["id"], co["id"],
                              {"version": 1, "mappings": [{"slot_id": "s1"}]}, "sub-9")
    assert ok["status"] == "confirmed" and ok["confirmed_by"] == "sub-9"

    rows = repo.list_templates(db, co["id"])
    assert any(r["id"] == t["id"] for r in rows)
```

- [ ] **Step 6: Run integration tests, expect PASS** (CI / `TEST_DATABASE_URL`):
  `uv run pytest tests/integration/test_report_templates_repo.py -v`
  Expected: PASS.

- [ ] **Step 7: Commit:**
```bash
git add src/repositories/report_templates.py tests/unit/test_report_templates_repo_unit.py tests/integration/test_report_templates_repo.py
git commit -m "Add report_templates repository (create/get/list/status/analysis/confirm)"
```

---

### Task 3: `ade_client.py` — ADE call + normalize adapter

> **SPIKE-PIN:** `_KIND_MAP`, the box-normalization, and the `analyze_document` import/call are pinned to the real ADE shape from Phase 0. The code below is against the **assumed** raw shape (see "ADE response shape" section). The unit test targets `_normalize` (pure, offline); `analyze_document` (network) is thin and mocked in the analyzer's tests.

**Files:**
- Create: `src/ade_client.py`
- Test: `tests/unit/test_ade_client.py`

**Interfaces:**
- Consumes: `ADE_API_KEY` (passed in, not read from env here); the `ade-python` library (imported lazily inside `analyze_document` so unit tests never need it).
- Produces:
  - `analyze_document(data: bytes, *, filename: str, api_key: str) -> dict` — returns normalized `ade_schema`.
  - `_normalize(raw_chunks: list, page_count: int) -> dict` — pure adapter (spike-pinned).

- [ ] **Step 1: Write the failing unit test** `tests/unit/test_ade_client.py`:
```python
"""Unit tests for src/ade_client._normalize (Template Agent Phase 1).
Targets the PURE adapter, not the network call. The `raw_chunks` fixtures use
the ASSUMED ADE raw shape (spec §3.2 stage1) — PIN to the real shape after the
Phase 0 spike, then update these fixtures + _KIND_MAP together."""
import ade_client


def _chunk(ctype, text, box, conf=0.9, table=None, page=1):
    return {"chunk_type": ctype, "text": text, "confidence": conf, "table": table,
            "grounding": [{"page": page, "box": box}]}


def test_normalize_maps_kinds_and_assigns_slot_ids():
    raw = [
        _chunk("section_header", "Site Progress", {"l": .1, "t": .2, "r": .9, "b": .25}),
        _chunk("text", "Narrative body", {"l": .1, "t": .26, "r": .9, "b": .4}),
        _chunk("table", "Open Actions", {"l": .1, "t": .5, "r": .92, "b": .8},
               table={"num_rows": 5, "num_cols": 4, "headers": ["Action", "Owner", "Due", "Status"]}),
    ]
    out = ade_client._normalize(raw, page_count=1)
    assert out["ade_version"] == "dpt-2" and out["page_count"] == 1
    slots = out["slots"]
    assert [s["slot_id"] for s in slots] == ["s1", "s2", "s3"]
    assert [s["kind"] for s in slots] == ["heading", "paragraph", "table"]
    assert slots[0]["page"] == 1 and slots[0]["box"] == {"l": .1, "t": .2, "r": .9, "b": .25}
    assert slots[2]["table"] == {"n_rows": 5, "n_cols": 4,
                                 "headers": ["Action", "Owner", "Due", "Status"]}
    assert slots[1]["table"] is None


def test_normalize_unknown_kind_defaults_to_field_and_missing_conf_defaults():
    raw = [{"chunk_type": "weird_new_type", "text": "x", "table": None,
            "grounding": [{"page": 2, "box": {"l": 0, "t": 0, "r": 1, "b": 1}}]}]
    out = ade_client._normalize(raw, page_count=2)
    assert out["slots"][0]["kind"] == "field"      # unknown -> 'field'
    assert out["slots"][0]["confidence"] == 0.5     # missing confidence -> 0.5
    assert out["slots"][0]["page"] == 2


def test_normalize_chunk_without_grounding_is_skipped():
    raw = [{"chunk_type": "text", "text": "no box", "grounding": []}]
    out = ade_client._normalize(raw, page_count=1)
    assert out["slots"] == []
```

- [ ] **Step 2: Run it, expect FAIL** (`ModuleNotFoundError: ade_client`):
  `uv run pytest tests/unit/test_ade_client.py -v`
  Expected: FAIL.

- [ ] **Step 3: Create `src/ade_client.py`:**
```python
"""ade_client.py — LandingAI ADE (DPT-2) call + normalize adapter (Template
Agent Phase 1). Non-VPC: called only by lambda_analyze_template (external
HTTPS, BUG-36). Everything downstream consumes the NORMALIZED ade_schema, so
_normalize is the single unit pinned to ADE's real response shape after the
Phase 0 spike.

SPIKE-PIN markers below (### PIN) are the exact lines to reconcile with
docs/superpowers/spikes/2026-07-19-ade-spike-notes.md."""
import logging

logger = logging.getLogger()

# ### PIN: real ADE chunk_type vocabulary -> our 4 kinds (from spike Step 4).
_KIND_MAP = {
    "title": "heading",
    "section_header": "heading",
    "heading": "heading",
    "text": "paragraph",
    "paragraph": "paragraph",
    "table": "table",
    "form_field": "field",
    "field": "field",
    "key_value": "field",
}


def _normalize(raw_chunks: list, page_count: int) -> dict:
    """Pure adapter: ADE raw chunks -> our stored ade_schema. A chunk with no
    grounding is skipped (can't be shown in the review overlay). Unknown
    chunk_type -> 'field' (fail-open). Missing confidence -> 0.5."""
    slots = []
    for chunk in raw_chunks:
        grounding = (chunk.get("grounding") or [])
        if not grounding:
            continue
        g0 = grounding[0]
        table = chunk.get("table")
        norm_table = None
        if table:
            norm_table = {
                "n_rows": table.get("num_rows"),
                "n_cols": table.get("num_cols"),
                "headers": table.get("headers") or [],
            }
        slots.append({
            "slot_id": f"s{len(slots) + 1}",
            "kind": _KIND_MAP.get(chunk.get("chunk_type"), "field"),
            "text": chunk.get("text") or "",
            "page": g0.get("page", 1),
            "box": g0.get("box"),  ### PIN: normalize to 0..1 here if spike says pixel
            "confidence": chunk.get("confidence") if chunk.get("confidence") is not None else 0.5,
            "table": norm_table,
        })
    return {"ade_version": "dpt-2", "page_count": page_count, "slots": slots}


def analyze_document(data: bytes, *, filename: str, api_key: str) -> dict:
    """Send one artifact to ADE and return the normalized ade_schema. The
    ade-python import is lazy so unit tests (which target _normalize) never
    need the library installed. ### PIN the import + call + how page_count and
    chunks are read off the real result object (spike Step 1-4)."""
    from ade_python import Client  ### PIN: real module/class from spike Step 1
    client = Client(api_key=api_key)
    result = client.parse(data, filename=filename)  ### PIN: real call signature
    raw_chunks = [c.model_dump() for c in result.chunks]  ### PIN: real accessor
    page_count = getattr(result, "page_count", None) or _infer_page_count(raw_chunks)
    return _normalize(raw_chunks, page_count)


def _infer_page_count(raw_chunks: list) -> int:
    pages = [g.get("page", 1) for c in raw_chunks for g in (c.get("grounding") or [])]
    return max(pages) if pages else 1
```

- [ ] **Step 4: Run the unit tests, expect PASS:**
  `uv run pytest tests/unit/test_ade_client.py -v`
  Expected: PASS.

- [ ] **Step 5: Commit:**
```bash
git add src/ade_client.py tests/unit/test_ade_client.py
git commit -m "Add ade_client: ADE call + normalize adapter (spike-pinned _normalize)"
```

---

### Task 4: `template_mapping.py` — data-source catalogue + Claude mapping proposer

**Files:**
- Create: `src/template_mapping.py`
- Test: `tests/unit/test_template_mapping.py`

**Interfaces:**
- Consumes: `claude_utils.call_claude(prompt, max_tokens)`, `claude_utils.extract_json(raw_text)`.
- Produces:
  - `DATA_SOURCES: list[dict]` — the §3.3 catalogue (`source`, `kind`, `description`).
  - `VALID_SOURCES: set[str]` — the allowed `source` tokens + `"none"`.
  - `build_mapping_prompt(ade_schema: dict) -> str`
  - `parse_mapping(claude_text: str, ade_schema: dict) -> dict` — `extract_json` + validate/clamp → `{version:1, confirmed:false, mappings:[{slot_id, kind, source, instruction, confidence}]}`
  - `propose_mapping(ade_schema: dict) -> tuple[dict | None, str | None]` — wraps `call_claude` + `parse_mapping`; returns `(mapping, None)` or `(None, error)`.

- [ ] **Step 1: Write the failing unit test** `tests/unit/test_template_mapping.py`:
```python
"""Unit tests for src/template_mapping.py (Template Agent Phase 1).
No network: call_claude is monkeypatched (mirrors test_lambda_extract_session's
posture). Targets the prompt builder + the parse/validate/clamp of the agent's
JSON proposal."""
import json

import template_mapping as tm

ADE = {"ade_version": "dpt-2", "page_count": 1, "slots": [
    {"slot_id": "s1", "kind": "heading", "text": "Site Progress", "box": {}, "confidence": .9, "table": None},
    {"slot_id": "s2", "kind": "table", "text": "Open Actions", "box": {}, "confidence": .8,
     "table": {"n_rows": 3, "n_cols": 4, "headers": ["Action", "Owner", "Due", "Status"]}},
]}


def test_prompt_includes_catalogue_and_slots():
    p = tm.build_mapping_prompt(ADE)
    assert "action_items" in p and "findings" in p and "weather" in p  # catalogue
    assert "s1" in p and "s2" in p                                     # slots
    assert "Open Actions" in p


def test_parse_mapping_validates_slot_and_source_and_clamps_confidence():
    raw = json.dumps({"mappings": [
        {"slot_id": "s1", "kind": "narrative", "source": "topics", "instruction": "Summarise progress", "confidence": 1.4},
        {"slot_id": "s2", "kind": "structured", "source": "action_items", "instruction": "Fill open actions", "confidence": 0.7},
        {"slot_id": "s2", "kind": "structured", "source": "made_up_source", "instruction": "x", "confidence": 0.5},
        {"slot_id": "s99", "kind": "structured", "source": "topics", "instruction": "orphan", "confidence": 0.5},
    ]})
    out = tm.parse_mapping(raw, ADE)
    assert out["version"] == 1 and out["confirmed"] is False
    by_slot = {(m["slot_id"], m["source"]): m for m in out["mappings"]}
    assert by_slot[("s1", "topics")]["confidence"] == 1.0        # clamped 1.4 -> 1.0
    assert ("s2", "action_items") in by_slot
    assert by_slot[("s2", "none")]["source"] == "none"           # unknown source -> 'none'
    # slot_id not present in the ADE schema is dropped entirely
    assert all(m["slot_id"] != "s99" for m in out["mappings"])


def test_parse_mapping_bad_json_returns_empty_mappings():
    out = tm.parse_mapping("not json at all", ADE)
    assert out == {"version": 1, "confirmed": False, "mappings": []}


def test_propose_mapping_surfaces_claude_error(monkeypatch):
    monkeypatch.setattr(tm.claude_utils, "call_claude", lambda *a, **k: (None, "boom"))
    mapping, err = tm.propose_mapping(ADE)
    assert mapping is None and err == "boom"


def test_propose_mapping_happy_path(monkeypatch):
    raw = json.dumps({"mappings": [
        {"slot_id": "s1", "kind": "narrative", "source": "topics", "instruction": "Summarise", "confidence": 0.9}]})
    monkeypatch.setattr(tm.claude_utils, "call_claude", lambda *a, **k: (raw, None))
    mapping, err = tm.propose_mapping(ADE)
    assert err is None and mapping["mappings"][0]["slot_id"] == "s1"
```

- [ ] **Step 2: Run it, expect FAIL** (`ModuleNotFoundError: template_mapping`):
  `uv run pytest tests/unit/test_template_mapping.py -v`
  Expected: FAIL.

- [ ] **Step 3: Create `src/template_mapping.py`:**
```python
"""template_mapping.py — Stage 2 of the Template Agent (spec §3.2): given the
normalized ADE schema + a catalogue of FieldSight item-store data sources,
Claude PROPOSES a field mapping (each template slot -> a source + fill
instruction + confidence). The proposal is ALWAYS human-reviewed before use
(spec §3.5) — so parse_mapping is defensive but fail-open: it never trusts
Claude's shape, drops slots/sources it can't validate, and clamps confidence.
Non-VPC: called only by lambda_analyze_template. Mirrors lambda_extract_session's
claude_utils.call_claude + extract_json usage."""
import json
import logging

import claude_utils

logger = logging.getLogger()

# The FieldSight item-store data sources a slot may map to (spec §3.3). Kept in
# code (not the DB) — it's the fixed vocabulary the agent chooses from and the
# UI's source dropdown renders from (Phase 1 UI reads it via GET /templates/{id}
# echo, or hardcodes the same list).
DATA_SOURCES = [
    {"source": "topics", "kind": "narrative",
     "description": "Site walk topics: title, summary, category, time_range, participants."},
    {"source": "action_items", "kind": "structured",
     "description": "Action items: text, responsible, deadline, priority, status."},
    {"source": "findings", "kind": "structured",
     "description": "Rich findings: observation, domain, severity, entity, recommended_action, programme impact."},
    {"source": "safety_observations", "kind": "structured",
     "description": "Safety observations: observation, risk_level, location, status."},
    {"source": "topic_photos", "kind": "structured",
     "description": "Site photos with captions."},
    {"source": "weather", "kind": "structured",
     "description": "Per (site, date) weather block."},
    {"source": "site_meta", "kind": "structured",
     "description": "Site/company metadata: name, address, client, period dates."},
]
VALID_SOURCES = {d["source"] for d in DATA_SOURCES} | {"none"}
VALID_KINDS = {"narrative", "structured"}


def build_mapping_prompt(ade_schema: dict) -> str:
    catalogue = "\n".join(
        f'- {d["source"]} ({d["kind"]}): {d["description"]}' for d in DATA_SOURCES)
    slots = "\n".join(
        f'- {s["slot_id"]} [{s["kind"]}] "{s.get("text", "")[:120]}"'
        + (f' table headers={s["table"]["headers"]}' if s.get("table") else "")
        for s in ade_schema.get("slots", []))
    return (
        "You are mapping a client report TEMPLATE onto FieldSight construction "
        "site data. For EACH template slot below, choose the single best-matching "
        "FieldSight data source and write a short fill instruction.\n\n"
        f"FieldSight data sources (pick `source` from these exact tokens, or "
        f'"none" if nothing fits):\n{catalogue}\n\n'
        f"Template slots:\n{slots}\n\n"
        "Return ONLY JSON of the form:\n"
        '{"mappings":[{"slot_id":"s1","kind":"narrative|structured",'
        '"source":"<one token>","instruction":"<how to fill it>",'
        '"confidence":0.0-1.0}]}\n'
        "Use `narrative` for prose sections filled by an LLM, `structured` for "
        "tables/single fields filled deterministically. Map every slot exactly once."
    )


def parse_mapping(claude_text: str, ade_schema: dict) -> dict:
    """extract_json + validate. Drops any mapping whose slot_id is not in the
    ADE schema; coerces an unknown source to 'none' and an unknown kind to
    'structured'; clamps confidence into [0,1]. Bad/absent JSON -> empty
    mappings (the human then maps from scratch in the review UI)."""
    empty = {"version": 1, "confirmed": False, "mappings": []}
    parsed = claude_utils.extract_json(claude_text)
    if not isinstance(parsed, dict):
        return empty
    valid_slot_ids = {s["slot_id"] for s in ade_schema.get("slots", [])}
    out = []
    for m in parsed.get("mappings", []):
        if not isinstance(m, dict) or m.get("slot_id") not in valid_slot_ids:
            continue
        source = m.get("source")
        if source not in VALID_SOURCES:
            source = "none"
        kind = m.get("kind") if m.get("kind") in VALID_KINDS else "structured"
        try:
            conf = float(m.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))
        out.append({"slot_id": m["slot_id"], "kind": kind, "source": source,
                    "instruction": str(m.get("instruction") or ""), "confidence": conf})
    return {"version": 1, "confirmed": False, "mappings": out}


def propose_mapping(ade_schema: dict):
    """Returns (mapping_dict, None) or (None, error_str)."""
    raw, err = claude_utils.call_claude(build_mapping_prompt(ade_schema), max_tokens=4096)
    if err:
        logger.error("mapping proposal Claude call failed: %s", err)
        return None, err
    return parse_mapping(raw, ade_schema), None
```

- [ ] **Step 4: Run the unit tests, expect PASS:**
  `uv run pytest tests/unit/test_template_mapping.py -v`
  Expected: PASS.

- [ ] **Step 5: Commit:**
```bash
git add src/template_mapping.py tests/unit/test_template_mapping.py
git commit -m "Add template_mapping: data-source catalogue + Claude mapping proposer"
```

---

### Task 5: `lambda_template_writer.py` — in-VPC writer

**Files:**
- Create: `src/lambda_template_writer.py`
- Test: `tests/unit/test_lambda_template_writer.py`

**Interfaces:**
- Consumes: `db.connection.get_connection`, `repositories.report_templates.set_analysis` / `set_status`.
- Produces: `lambda_handler(event, context) -> dict`. Event payload from `AnalyzeTemplateFunction`:
  - success: `{"template_id": str, "ade_schema": dict, "field_mapping": dict}`
  - failure: `{"template_id": str, "error_note": str}` (sets `status='error'`).
  - Returns `{"ok": True, "template_id": ..., "status": ...}`.

- [ ] **Step 1: Write the failing unit test** `tests/unit/test_lambda_template_writer.py`:
```python
"""Unit tests for src/lambda_template_writer.py (Template Agent Phase 1).
In-VPC writer hop (BUG-36): the non-VPC analyzer async-invokes this to land
ADE output in Aurora. get_connection + repo are monkeypatched (DB-free)."""
import pytest

tw = pytest.importorskip("lambda_template_writer", reason="requires psycopg (installed in CI)")


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_success_path_calls_set_analysis(monkeypatch):
    monkeypatch.setattr(tw, "get_connection", lambda *a, **k: FakeConn())
    seen = {}
    monkeypatch.setattr(tw.report_templates, "set_analysis",
                        lambda conn, tid, ade, mapping: seen.update(
                            {"tid": tid, "ade": ade, "mapping": mapping}) or {"id": tid, "status": "mapping_review"})
    out = tw.lambda_handler({"template_id": "t-1", "ade_schema": {"slots": []},
                             "field_mapping": {"version": 1, "mappings": []}}, None)
    assert out["ok"] is True and out["status"] == "mapping_review"
    assert seen["tid"] == "t-1" and seen["ade"] == {"slots": []}


def test_error_payload_sets_status_error(monkeypatch):
    monkeypatch.setattr(tw, "get_connection", lambda *a, **k: FakeConn())
    seen = {}
    monkeypatch.setattr(tw.report_templates, "set_status",
                        lambda conn, tid, status, error_note=None: seen.update(
                            {"tid": tid, "status": status, "note": error_note}) or {"id": tid, "status": status})
    out = tw.lambda_handler({"template_id": "t-1", "error_note": "ADE failed"}, None)
    assert out["status"] == "error" and seen["note"] == "ADE failed"


def test_missing_template_id_raises(monkeypatch):
    monkeypatch.setattr(tw, "get_connection", lambda *a, **k: FakeConn())
    with pytest.raises(ValueError):
        tw.lambda_handler({}, None)
```

- [ ] **Step 2: Run it, expect FAIL** (`ModuleNotFoundError: lambda_template_writer`):
  `uv run pytest tests/unit/test_lambda_template_writer.py -v`
  Expected: FAIL.

- [ ] **Step 3: Create `src/lambda_template_writer.py`:**
```python
"""Lambda: fieldsight-template-writer v1.0 — Template Agent Phase 1.

In-VPC (psycopg -> Aurora). The non-VPC lambda_analyze_template async-invokes
this to LAND the ADE structure + proposed mapping in Aurora (BUG-36: the
analyzer can't touch Aurora, this writer can't touch the internet). Mirrors
SuggestionWriterFunction's role exactly (a pure in-VPC write hop for a non-VPC
producer). `with get_connection() as conn:` commits on clean exit.

Event payload:
  success: {template_id, ade_schema, field_mapping}
  failure: {template_id, error_note}   -> status='error'
"""
import logging

from db.connection import get_connection
from repositories import report_templates

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    template_id = (event or {}).get("template_id")
    if not template_id:
        raise ValueError("template_id is required")
    with get_connection() as conn:
        if event.get("error_note"):
            row = report_templates.set_status(conn, template_id, "error",
                                              error_note=event["error_note"])
        else:
            row = report_templates.set_analysis(
                conn, template_id, event.get("ade_schema") or {},
                event.get("field_mapping") or {"version": 1, "confirmed": False, "mappings": []})
    status = row["status"] if row else "missing"
    logger.info("template-writer: template_id=%s status=%s", template_id, status)
    return {"ok": True, "template_id": template_id, "status": status}
```

- [ ] **Step 4: Run the unit tests, expect PASS:**
  `uv run pytest tests/unit/test_lambda_template_writer.py -v`
  Expected: PASS.

- [ ] **Step 5: Commit:**
```bash
git add src/lambda_template_writer.py tests/unit/test_lambda_template_writer.py
git commit -m "Add lambda_template_writer: in-VPC persist of ADE schema + mapping"
```

---

### Task 6: `lambda_analyze_template.py` — non-VPC analyzer

> **SPIKE-PIN:** this handler's only ADE coupling is the `ade_client.analyze_document` call, already isolated in Task 3. No further pinning here.

**Files:**
- Create: `src/lambda_analyze_template.py`
- Test: `tests/unit/test_lambda_analyze_template.py`

**Interfaces:**
- Consumes: `ade_client.analyze_document`, `template_mapping.propose_mapping`, boto3 `s3` (get artifact) + `lambda` (invoke writer). Env: `S3_BUCKET`, `ADE_API_KEY`, `ANTHROPIC_API_KEY`/`CLAUDE_MODEL` (via `claude_utils`), `TEMPLATE_WRITER_FUNCTION`, `REPORT_TEMPLATES_PREFIX`.
- Produces: `lambda_handler(event, context)`. Event (async-invoked by org-api): `{"template_id","company_id","original_s3_key","original_format"}`. Effect: writes `report-templates/{company}/{id}/analysis.json` (audit, mirrors the report path's `*_debug.json`) and RequestResponse-invokes the writer with `{template_id, ade_schema, field_mapping}` — or `{template_id, error_note}` on failure.

- [ ] **Step 1: Write the failing unit test** `tests/unit/test_lambda_analyze_template.py`:
```python
"""Unit tests for src/lambda_analyze_template.py (Template Agent Phase 1).
Non-VPC analyzer. ade_client / template_mapping / boto3 clients are all
monkeypatched — no real ADE, Claude, S3 or Lambda call. Mirrors
test_lambda_extract_session's FakeS3 + dummy-env posture."""
import io
import json
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")
os.environ.setdefault("ADE_API_KEY", "ade-test-dummy")
os.environ.setdefault("TEMPLATE_WRITER_FUNCTION", "fieldsight-test-template-writer")

lat = pytest.importorskip("lambda_analyze_template", reason="requires boto3 (installed in CI)")


class FakeS3:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.put_calls = []

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, **kw):
        self.put_calls.append(kw)
        self.objects[kw["Key"]] = kw["Body"]
        return {}


class FakeLambda:
    def __init__(self):
        self.invokes = []

    def invoke(self, **kw):
        self.invokes.append(kw)
        return {"StatusCode": 200}


EVENT = {"template_id": "t-1", "company_id": "c-1",
         "original_s3_key": "report-templates/c-1/abc/original.docx",
         "original_format": "docx"}


def _wire(monkeypatch, s3, lam):
    monkeypatch.setattr(lat, "_s3_client", s3)
    monkeypatch.setattr(lat, "_lambda_client", lam)


def test_happy_path_invokes_writer_with_schema_and_mapping(monkeypatch):
    s3 = FakeS3({EVENT["original_s3_key"]: b"DOCXBYTES"})
    lam = FakeLambda()
    _wire(monkeypatch, s3, lam)
    monkeypatch.setattr(lat.ade_client, "analyze_document",
                        lambda data, filename, api_key: {"ade_version": "dpt-2", "page_count": 1,
                                                         "slots": [{"slot_id": "s1"}]})
    monkeypatch.setattr(lat.template_mapping, "propose_mapping",
                        lambda schema: ({"version": 1, "confirmed": False,
                                         "mappings": [{"slot_id": "s1", "source": "topics"}]}, None))
    out = lat.lambda_handler(EVENT, None)
    assert out["ok"] is True
    payload = json.loads(lam.invokes[0]["Payload"])
    assert payload["template_id"] == "t-1"
    assert payload["ade_schema"]["slots"] == [{"slot_id": "s1"}]
    assert payload["field_mapping"]["mappings"][0]["source"] == "topics"
    # analysis.json audit artifact written under the template's prefix
    assert any(k["Key"].endswith("/analysis.json") for k in s3.put_calls)


def test_ade_failure_invokes_writer_with_error_note(monkeypatch):
    s3 = FakeS3({EVENT["original_s3_key"]: b"DOCXBYTES"})
    lam = FakeLambda()
    _wire(monkeypatch, s3, lam)

    def boom(*a, **k):
        raise RuntimeError("ADE 500")
    monkeypatch.setattr(lat.ade_client, "analyze_document", boom)
    out = lat.lambda_handler(EVENT, None)
    assert out["ok"] is False
    payload = json.loads(lam.invokes[0]["Payload"])
    assert payload["template_id"] == "t-1" and "ADE 500" in payload["error_note"]


def test_mapping_error_invokes_writer_with_error_note(monkeypatch):
    s3 = FakeS3({EVENT["original_s3_key"]: b"DOCXBYTES"})
    lam = FakeLambda()
    _wire(monkeypatch, s3, lam)
    monkeypatch.setattr(lat.ade_client, "analyze_document",
                        lambda data, filename, api_key: {"slots": []})
    monkeypatch.setattr(lat.template_mapping, "propose_mapping",
                        lambda schema: (None, "claude boom"))
    out = lat.lambda_handler(EVENT, None)
    assert out["ok"] is False
    payload = json.loads(lam.invokes[0]["Payload"])
    assert "claude boom" in payload["error_note"]
```

- [ ] **Step 2: Run it, expect FAIL** (`ModuleNotFoundError: lambda_analyze_template`):
  `uv run pytest tests/unit/test_lambda_analyze_template.py -v`
  Expected: FAIL.

- [ ] **Step 3: Create `src/lambda_analyze_template.py`:**
```python
"""Lambda: fieldsight-analyze-template v1.0 — Template Agent Phase 1, Stage 1+2.

NON-VPC (talks to ADE + Claude over external HTTPS — BUG-36: an in-VPC Lambda
cannot reach either). Async-invoked by lambda_org_api's POST /templates/{id}/
analyze. On each invocation:
  1. Download the uploaded artifact from S3.
  2. ade_client.analyze_document -> normalized ade_schema (external ADE call).
  3. template_mapping.propose_mapping -> proposed field mapping (Claude call).
  4. Write report-templates/{company}/{id}/analysis.json (audit, mirrors the
     report path's *_debug.json).
  5. RequestResponse-invoke the in-VPC TemplateWriterFunction to persist the
     schema + mapping to Aurora (mirrors matcher -> suggestion-writer).
Any failure invokes the writer with {error_note} so the template row lands in
status='error' (never silently stuck in 'analyzing').

Environment Variables:
    S3_BUCKET                 - data bucket holding report-templates/
    ADE_API_KEY               - LandingAI ADE key (SAM AdeApiKey param)
    ANTHROPIC_API_KEY / CLAUDE_MODEL - read by claude_utils
    TEMPLATE_WRITER_FUNCTION  - in-VPC writer Lambda name
    REPORT_TEMPLATES_PREFIX   - default report-templates/
"""
import json
import logging
import os

import boto3

import ade_client
import template_mapping

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
ADE_API_KEY = os.environ.get("ADE_API_KEY", "")
TEMPLATE_WRITER_FUNCTION = os.environ.get("TEMPLATE_WRITER_FUNCTION", "")
REPORT_TEMPLATES_PREFIX = os.environ.get("REPORT_TEMPLATES_PREFIX", "report-templates/")

_s3_client = None
_lambda_client = None


def s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def lambda_client():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    return _lambda_client


def _invoke_writer(payload: dict):
    lambda_client().invoke(
        FunctionName=TEMPLATE_WRITER_FUNCTION,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8"),
    )


def lambda_handler(event, context):
    template_id = event["template_id"]
    company_id = event["company_id"]
    key = event["original_s3_key"]
    fmt = event.get("original_format", "docx")
    try:
        data = s3().get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
        ade_schema = ade_client.analyze_document(
            data, filename=f"{template_id}.{fmt}", api_key=ADE_API_KEY)
        mapping, err = template_mapping.propose_mapping(ade_schema)
        if err:
            raise RuntimeError(f"mapping proposal failed: {err}")
        # Audit artifact (never re-triggers anything — report-templates/ has no
        # S3 event wired; the analyze hop is user-invoked, not event-driven).
        analysis_key = f"{REPORT_TEMPLATES_PREFIX}{company_id}/{template_id}/analysis.json"
        s3().put_object(Bucket=S3_BUCKET, Key=analysis_key,
                        Body=json.dumps({"ade_schema": ade_schema, "field_mapping": mapping},
                                        default=str).encode("utf-8"),
                        ContentType="application/json")
        _invoke_writer({"template_id": template_id, "ade_schema": ade_schema,
                        "field_mapping": mapping})
        logger.info("analyze-template ok: template_id=%s slots=%d", template_id,
                    len(ade_schema.get("slots", [])))
        return {"ok": True, "template_id": template_id}
    except Exception as e:  # noqa: BLE001 — any failure must land the row in 'error'
        logger.exception("analyze-template failed: template_id=%s", template_id)
        _invoke_writer({"template_id": template_id, "error_note": str(e)[:500]})
        return {"ok": False, "template_id": template_id, "error": str(e)}
```

- [ ] **Step 4: Run the unit tests, expect PASS:**
  `uv run pytest tests/unit/test_lambda_analyze_template.py -v`
  Expected: PASS.

- [ ] **Step 5: Commit:**
```bash
git add src/lambda_analyze_template.py tests/unit/test_lambda_analyze_template.py
git commit -m "Add lambda_analyze_template: non-VPC ADE + Claude mapping proposer"
```

---

### Task 7: org-api routes — create / list / get + presigned PUT

**Files:**
- Modify: `src/lambda_org_api.py` (import `report_templates`; add `RECORD`-style constants; `_template_s3_key`; `create_template_endpoint`, `list_templates_endpoint`, `get_template_endpoint`; dispatch entries).
- Test: `tests/unit/test_org_api_templates.py`

**Interfaces:**
- Consumes: `report_templates.create_template` / `list_templates` / `get_template`; `sites.get_site`; `s3().generate_presigned_url`; module env `S3_BUCKET`, `PRESIGNED_URL_EXPIRY`, new `REPORT_TEMPLATES_PREFIX`.
- Produces (routes under `/api/org`):
  - `POST /templates` → `{templateId, uploadUrl, s3Key}` (creates row status `uploaded`, presigns PUT). Body: `{title, contentType, siteId?}`. `contentType` ∈ `{application/vnd.openxmlformats-officedocument.wordprocessingml.document → docx, application/pdf → pdf}`.
  - `GET /templates` → `{templates:[...]}` (company-scoped).
  - `GET /templates/{id}` → the template row + a presigned GET `originalUrl` for the artifact (tenant-guarded).

- [ ] **Step 1: Write the failing unit test** `tests/unit/test_org_api_templates.py`:
```python
"""Unit tests for the org-api template routes (Template Agent Phase 1).
Reuses the FakeConn/FakeS3 posture of tests/unit/test_recordings_api.py."""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeS3:
    def generate_presigned_url(self, op, Params=None, ExpiresIn=0):
        self.last = {"op": op, "params": Params}
        return "https://s3.example/" + Params["Key"]


CALLER = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-1", "email": "a@x.nz",
          "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
          "global_role": "pm", "created_at": "2026-07-19", "archived_at": None}


def make_event(method, path, sub="sub-1", body=None):
    return {"httpMethod": method, "path": path, "queryStringParameters": None,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"claims": {"sub": sub}}}}


def body_of(res):
    return json.loads(res["body"])


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER) if sub == "sub-1" else None)
    fake = FakeS3()
    monkeypatch.setattr(org, "_s3_client", fake)
    return monkeypatch, fake


def test_create_template_presigns_and_creates_row(wired):
    mp, fake = wired
    created = {}
    mp.setattr(org.report_templates, "create_template",
               lambda conn, company_id, title, key, fmt, site_id=None, created_by=None:
               created.update({"company_id": company_id, "key": key, "fmt": fmt}) or
               {"id": "t-1", "status": "uploaded"})
    res = org.lambda_handler(make_event(
        "POST", "/api/org/templates",
        body={"title": "Daily", "contentType": "application/pdf"}), None)
    assert res["statusCode"] == 200
    b = body_of(res)
    assert b["templateId"] == "t-1" and b["s3Key"].startswith("report-templates/c-1/")
    assert b["s3Key"].endswith("/original.pdf") and b["uploadUrl"].startswith("https://s3.example/")
    assert created["fmt"] == "pdf" and created["company_id"] == "c-1"


def test_create_template_rejects_bad_content_type(wired):
    res = org.lambda_handler(make_event(
        "POST", "/api/org/templates", body={"title": "X", "contentType": "image/png"}), None)
    assert res["statusCode"] == 400


def test_create_template_requires_title(wired):
    res = org.lambda_handler(make_event(
        "POST", "/api/org/templates", body={"contentType": "application/pdf"}), None)
    assert res["statusCode"] == 400


def test_list_templates_company_scoped(wired):
    mp, _ = wired
    mp.setattr(org.report_templates, "list_templates",
               lambda conn, company_id: [{"id": "t-1", "company_id": company_id}])
    res = org.lambda_handler(make_event("GET", "/api/org/templates"), None)
    assert res["statusCode"] == 200 and body_of(res)["templates"][0]["id"] == "t-1"


def test_get_template_tenant_guard_404_cross_company(wired):
    mp, _ = wired
    mp.setattr(org.report_templates, "get_template",
               lambda conn, tid: {"id": tid, "company_id": "OTHER",
                                  "original_s3_key": "report-templates/OTHER/x/original.docx"})
    res = org.lambda_handler(make_event("GET", "/api/org/templates/t-9"), None)
    assert res["statusCode"] == 404


def test_get_template_returns_row_with_original_url(wired):
    mp, _ = wired
    mp.setattr(org.report_templates, "get_template",
               lambda conn, tid: {"id": tid, "company_id": "c-1",
                                  "original_s3_key": "report-templates/c-1/x/original.docx"})
    res = org.lambda_handler(make_event("GET", "/api/org/templates/t-1"), None)
    assert res["statusCode"] == 200
    b = body_of(res)
    assert b["template"]["id"] == "t-1" and b["originalUrl"].startswith("https://s3.example/")
```

- [ ] **Step 2: Run it, expect FAIL** (routes not found → 404, assertions fail):
  `uv run pytest tests/unit/test_org_api_templates.py -v`
  Expected: FAIL.

- [ ] **Step 3: Add the import + constants** near the top of `src/lambda_org_api.py`. Add `report_templates` to the existing `from repositories import (...)` tuple (line ~64-66), and after `VOICE_PREFIX`/`ALLOWED_VOICE_TYPES` (line ~109-111) add:
```python
# Template Agent (Phase 1): client report templates uploaded for ADE analysis.
REPORT_TEMPLATES_PREFIX = os.environ.get("REPORT_TEMPLATES_PREFIX", "report-templates/")
ANALYZE_TEMPLATE_FUNCTION = os.environ.get("ANALYZE_TEMPLATE_FUNCTION", "")
_TEMPLATE_CONTENT_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/pdf": "pdf",
}
```
Also add a module-level lambda client accessor (used by Task 8) after `cognito()` (line ~128):
```python
_lambda_client = None


def lambda_client():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    return _lambda_client
```

- [ ] **Step 4: Add dispatch entries.** In `dispatch(...)`, just before `return error("not found", 404)` (line ~287), add:
```python
    if route == "/templates":
        if method == "POST":
            return create_template_endpoint(conn, caller, parse_body(event))
        if method == "GET":
            return list_templates_endpoint(conn, caller)
    m_tg = re.match(r"^/templates/([^/]+)$", route)
    if m_tg and method == "GET":
        return get_template_endpoint(conn, caller, m_tg.group(1))
    m_ta = re.match(r"^/templates/([^/]+)/analyze$", route)
    if m_ta and method == "POST":
        return analyze_template_endpoint(conn, caller, m_ta.group(1))
    m_tm = re.match(r"^/templates/([^/]+)/mapping$", route)
    if m_tm and method == "PATCH":
        return confirm_template_mapping_endpoint(conn, caller, m_tm.group(1), parse_body(event))
```
(The `analyze`/`mapping` handlers are implemented in Task 8; add all four routes now so the dispatch table is written once.)

- [ ] **Step 5: Add the handlers.** Append a new section to `src/lambda_org_api.py` (near the recordings section, after `list_site_voice`):
```python
# ----------------------------------------------------------
# /templates — Template Agent (Phase 1): upload + ADE analyze + mapping review
# ----------------------------------------------------------
def _template_s3_key(company_id, template_folder, ext):
    # report-templates/{company}/{uuid}/original.{ext} — a DEDICATED prefix
    # that matches NO wire-s3-events.sh trigger (BUG-13): the analyze hop is
    # user-invoked, never S3-event-driven, so an upload here ingests nothing.
    return f"{REPORT_TEMPLATES_PREFIX}{_safe_seg(str(company_id))}/{template_folder}/original.{ext}"


def create_template_endpoint(conn, caller, body):
    if body is None:
        return error("malformed JSON body", 400)
    title = (body.get("title") or "").strip()
    if not title:
        return error("title is required", 400)
    content_type = body.get("contentType")
    ext = _TEMPLATE_CONTENT_TYPES.get(content_type) if isinstance(content_type, str) else None
    if ext is None:
        return error(f"contentType must be one of {sorted(_TEMPLATE_CONTENT_TYPES)}", 400)
    site_id = body.get("siteId")
    if site_id:
        site = sites.get_site(conn, site_id)
        if site is None or site["company_id"] != caller["company_id"]:
            return error("site not accessible", 403)
    template_folder = uuid.uuid4().hex
    key = _template_s3_key(caller["company_id"], template_folder, ext)
    row = report_templates.create_template(
        conn, caller["company_id"], title, key, ext,
        site_id=site_id, created_by=caller["cognito_sub"])
    url = s3().generate_presigned_url(
        "put_object",
        Params={"Bucket": S3_BUCKET, "Key": key, "ContentType": content_type},
        ExpiresIn=PRESIGNED_URL_EXPIRY,
    )
    return ok({"templateId": row["id"], "uploadUrl": url, "s3Key": key,
               "status": row["status"]})


def list_templates_endpoint(conn, caller):
    return ok({"templates": report_templates.list_templates(conn, caller["company_id"])})


def _load_own_template(conn, caller, template_id):
    """get_template + tenant guard. Returns (row, None) or (None, error_response)."""
    row = report_templates.get_template(conn, template_id)
    if row is None or row["company_id"] != caller["company_id"]:
        return None, error("template not found", 404)
    return row, None


def get_template_endpoint(conn, caller, template_id):
    row, err = _load_own_template(conn, caller, template_id)
    if err:
        return err
    original_url = s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": row["original_s3_key"]},
        ExpiresIn=PRESIGNED_URL_EXPIRY,
    )
    return ok({"template": row, "originalUrl": original_url})
```

- [ ] **Step 6: Run the unit tests, expect PASS** (create/list/get subset — the analyze/mapping tests live in Task 8):
  `uv run pytest tests/unit/test_org_api_templates.py -k "create or list or get" -v`
  Expected: PASS.

- [ ] **Step 7: Commit:**
```bash
git add src/lambda_org_api.py tests/unit/test_org_api_templates.py
git commit -m "org-api: POST/GET /templates (create+presign, list, get) — Template Agent intake"
```

---

### Task 8: org-api routes — analyze trigger + confirm mapping

**Files:**
- Modify: `src/lambda_org_api.py` (`analyze_template_endpoint`, `confirm_template_mapping_endpoint`).
- Test: extend `tests/unit/test_org_api_templates.py`.

**Interfaces:**
- Consumes: `report_templates.set_status` / `confirm_mapping`; `lambda_client().invoke`; `template_mapping.VALID_SOURCES` (validation); env `ANALYZE_TEMPLATE_FUNCTION`.
- Produces:
  - `POST /templates/{id}/analyze` → sets status `analyzing`, async-invokes `AnalyzeTemplateFunction` (`InvocationType="Event"`), returns `{ok, status:"analyzing"}`.
  - `PATCH /templates/{id}/mapping` → validates the edited mapping, `report_templates.confirm_mapping` (tenant-guarded), returns the confirmed row. Body: `{mappings:[{slot_id, kind, source, instruction, confidence}]}`.

- [ ] **Step 1: Write the failing unit tests** (append to `tests/unit/test_org_api_templates.py`):
```python
def test_analyze_sets_status_and_invokes_function(wired, monkeypatch):
    mp, _ = wired
    monkeypatch.setenv("ANALYZE_TEMPLATE_FUNCTION", "fieldsight-test-analyze-template")
    mp.setattr(org, "ANALYZE_TEMPLATE_FUNCTION", "fieldsight-test-analyze-template")
    mp.setattr(org.report_templates, "get_template",
               lambda conn, tid: {"id": tid, "company_id": "c-1", "status": "uploaded",
                                  "original_s3_key": "report-templates/c-1/x/original.docx",
                                  "original_format": "docx"})
    mp.setattr(org.report_templates, "set_status",
               lambda conn, tid, status, error_note=None: {"id": tid, "status": status})

    class FakeLambda:
        def __init__(self):
            self.invokes = []

        def invoke(self, **kw):
            self.invokes.append(kw)
            return {"StatusCode": 202}
    fake_lambda = FakeLambda()
    mp.setattr(org, "_lambda_client", fake_lambda)

    res = org.lambda_handler(make_event("POST", "/api/org/templates/t-1/analyze"), None)
    assert res["statusCode"] == 200 and body_of(res)["status"] == "analyzing"
    inv = fake_lambda.invokes[0]
    assert inv["InvocationType"] == "Event"
    payload = json.loads(inv["Payload"])
    assert payload["template_id"] == "t-1" and payload["company_id"] == "c-1"


def test_analyze_cross_company_404(wired, monkeypatch):
    mp, _ = wired
    mp.setattr(org.report_templates, "get_template",
               lambda conn, tid: {"id": tid, "company_id": "OTHER"})
    res = org.lambda_handler(make_event("POST", "/api/org/templates/t-9/analyze"), None)
    assert res["statusCode"] == 404


def test_confirm_mapping_validates_and_freezes(wired, monkeypatch):
    mp, _ = wired
    mp.setattr(org.report_templates, "get_template",
               lambda conn, tid: {"id": tid, "company_id": "c-1", "status": "mapping_review"})
    seen = {}
    mp.setattr(org.report_templates, "confirm_mapping",
               lambda conn, tid, company_id, mapping, confirmed_by:
               seen.update({"mapping": mapping, "by": confirmed_by, "company": company_id}) or
               {"id": tid, "status": "confirmed"})
    res = org.lambda_handler(make_event(
        "PATCH", "/api/org/templates/t-1/mapping",
        body={"mappings": [{"slot_id": "s1", "kind": "narrative", "source": "topics",
                            "instruction": "Summarise", "confidence": 0.9}]}), None)
    assert res["statusCode"] == 200 and body_of(res)["template"]["status"] == "confirmed"
    assert seen["by"] == "sub-1" and seen["company"] == "c-1"
    assert seen["mapping"]["mappings"][0]["source"] == "topics"


def test_confirm_mapping_rejects_unknown_source(wired, monkeypatch):
    mp, _ = wired
    mp.setattr(org.report_templates, "get_template",
               lambda conn, tid: {"id": tid, "company_id": "c-1", "status": "mapping_review"})
    res = org.lambda_handler(make_event(
        "PATCH", "/api/org/templates/t-1/mapping",
        body={"mappings": [{"slot_id": "s1", "source": "not_a_source"}]}), None)
    assert res["statusCode"] == 400


def test_confirm_mapping_requires_list(wired):
    res = org.lambda_handler(make_event(
        "PATCH", "/api/org/templates/t-1/mapping", body={"mappings": "nope"}), None)
    assert res["statusCode"] == 400
```

- [ ] **Step 2: Run it, expect FAIL** (handlers not defined → 404/500):
  `uv run pytest tests/unit/test_org_api_templates.py -k "analyze or confirm" -v`
  Expected: FAIL.

- [ ] **Step 3: Add the handlers** to the `/templates` section of `src/lambda_org_api.py`, and add the `template_mapping` import at the top (module import, used only for its `VALID_SOURCES`/`VALID_KINDS` constants — pure Python, no network):
```python
import template_mapping  # VALID_SOURCES / VALID_KINDS for mapping validation
```
Then the handlers:
```python
def analyze_template_endpoint(conn, caller, template_id):
    row, err = _load_own_template(conn, caller, template_id)
    if err:
        return err
    if not ANALYZE_TEMPLATE_FUNCTION:
        return error("template analysis is not configured", 503)
    report_templates.set_status(conn, template_id, "analyzing")
    lambda_client().invoke(
        FunctionName=ANALYZE_TEMPLATE_FUNCTION,
        InvocationType="Event",  # fire-and-forget: the user request returns immediately
        Payload=json.dumps({
            "template_id": template_id,
            "company_id": str(caller["company_id"]),
            "original_s3_key": row["original_s3_key"],
            "original_format": row["original_format"],
        }).encode("utf-8"),
    )
    return ok({"ok": True, "status": "analyzing"})


def _validate_confirmed_mapping(body):
    """Human-edited mapping from the review UI. Fail-CLOSED here (unlike the
    agent's fail-open proposal): a confirmed mapping is frozen and reused every
    period, so a bad source/kind/slot must be rejected, not silently dropped.
    Returns (mapping_dict, None) or (None, message)."""
    if not isinstance(body, dict):
        return None, "malformed JSON body"
    mappings = body.get("mappings")
    if not isinstance(mappings, list):
        return None, "mappings must be a list"
    clean = []
    for m in mappings:
        if not isinstance(m, dict) or not m.get("slot_id"):
            return None, "each mapping needs a slot_id"
        if m.get("source") not in template_mapping.VALID_SOURCES:
            return None, f"unknown source: {m.get('source')}"
        if m.get("kind") not in template_mapping.VALID_KINDS:
            return None, f"unknown kind: {m.get('kind')}"
        try:
            conf = max(0.0, min(1.0, float(m.get("confidence", 0.0))))
        except (TypeError, ValueError):
            conf = 0.0
        clean.append({"slot_id": m["slot_id"], "kind": m["kind"], "source": m["source"],
                      "instruction": str(m.get("instruction") or ""), "confidence": conf})
    return {"version": 1, "confirmed": True, "mappings": clean}, None


def confirm_template_mapping_endpoint(conn, caller, template_id, body):
    row, err = _load_own_template(conn, caller, template_id)
    if err:
        return err
    mapping, msg = _validate_confirmed_mapping(body)
    if msg:
        return error(msg, 400)
    confirmed = report_templates.confirm_mapping(
        conn, template_id, caller["company_id"], mapping, caller["cognito_sub"])
    if confirmed is None:
        return error("template not found", 404)
    return ok({"template": confirmed})
```

- [ ] **Step 4: Run the whole template test file, expect PASS:**
  `uv run pytest tests/unit/test_org_api_templates.py -v`
  Expected: PASS (all create/list/get/analyze/confirm tests).

- [ ] **Step 5: Commit:**
```bash
git add src/lambda_org_api.py tests/unit/test_org_api_templates.py
git commit -m "org-api: POST /templates/{id}/analyze + PATCH /templates/{id}/mapping (confirm+freeze)"
```

---

### Task 9: SAM wiring — `AdeApiKey`, layer, two functions, org-api env + IAM

**Files:**
- Modify: `src/template.yaml` (parameter, layer, 2 functions, org-api env/policy).
- Create: `infra/ade-layer/requirements.txt`.
- Modify: `.github/workflows/deploy.yml`, `.github/workflows/deploy-prod.yml` (pass `AdeApiKey`).

**Interfaces:**
- Consumes: existing `ClaudeApiKey`/`ANTHROPIC_API_KEY` threading pattern; `PsycopgLayer` + `DbSubnetIds` + `!ImportValue ${DbStackName}-LambdaSG` (in-VPC pattern); `ExtractSessionFunction` shape (non-VPC).
- Produces: `AnalyzeTemplateFunction` (non-VPC, unconditional), `TemplateWriterFunction` (in-VPC, `HasDb`), `AdeLayer`; org-api gains `ANALYZE_TEMPLATE_FUNCTION` + `REPORT_TEMPLATES_PREFIX` env, `report-templates/*` S3 grant, and `lambda:InvokeFunction` on `AnalyzeTemplateFunction`.

- [ ] **Step 1: Add the `AdeApiKey` parameter** after `ClaudeApiKey` (template.yaml line ~141):
```yaml
  AdeApiKey:
    Type: String
    Description: LandingAI ADE (DPT-2) API key for template structure extraction (Template Agent)
    NoEcho: true
    Default: ''
```

- [ ] **Step 2: Create `infra/ade-layer/requirements.txt`:**
```
ade-python
```
> NOTE (SPIKE-PIN): pin the exact package name + version discovered in Phase 0 Step 1 (e.g. `ade-python==X.Y.Z`). If the DPT-2 wheel is too large for a Lambda layer, the fallback (decided at spike time) is to drop this layer and implement `ade_client.analyze_document` as a direct urllib3 HTTPS call (the `claude_utils` pattern), since the spike also captures the raw HTTP contract.

- [ ] **Step 3: Add the `AdeLayer`** near `DashScopeLayer` (template.yaml ~line 723). Unconditional (AnalyzeTemplateFunction is unconditional):
```yaml
  # ----------------------------------------------------------
  # Lambda Layer: LandingAI ADE (DPT-2) SDK for AnalyzeTemplateFunction.
  # Same sam-build pattern as DashScopeLayer/PsycopgLayer. Unconditional (the
  # analyzer is non-VPC and always deploys, like ExtractSessionFunction).
  # ----------------------------------------------------------
  AdeLayer:
    Type: AWS::Serverless::LayerVersion
    Properties:
      LayerName: !Sub ["${P}-ade-layer", {P: !FindInMap [StageConfig, !Ref Stage, Prefix]}]
      Description: LandingAI ADE (DPT-2) SDK (ade-python) for AnalyzeTemplateFunction
      ContentUri: infra/ade-layer/
      CompatibleRuntimes:
        - python3.11
    Metadata:
      BuildMethod: python3.11
```

- [ ] **Step 4: Add `AnalyzeTemplateFunction`** (non-VPC) after `ExtractSessionFunction` (template.yaml ~line 1027). Mirrors ExtractSessionFunction's non-VPC shape + ReportGeneratorFunction's ANTHROPIC env:
```yaml
  # ----------------------------------------------------------
  # Lambda: Analyze Template (Template Agent Phase 1, Stage 1+2) — NON-VPC.
  # Async-invoked by OrgApiFunction (POST /templates/{id}/analyze). Calls ADE
  # (external HTTPS) + Claude (claude_utils) then RequestResponse-invokes the
  # in-VPC TemplateWriterFunction to persist to Aurora (BUG-36 split, mirrors
  # matcher -> suggestion-writer). Unconditional (no Aurora access here), like
  # ExtractSessionFunction. TEMPLATE_WRITER_FUNCTION Refs a HasDb resource; the
  # top-of-file W1001 suppression covers that cross-condition Ref (inert in
  # practice — both real deploy targets set DbStackName).
  # ----------------------------------------------------------
  AnalyzeTemplateFunction:
    Type: AWS::Serverless::Function
    Properties:
      FunctionName: !Sub ["${P}-analyze-template", {P: !FindInMap [StageConfig, !Ref Stage, Prefix]}]
      CodeUri: src/
      Handler: lambda_analyze_template.lambda_handler
      Timeout: 300
      MemorySize: 512
      Layers:
        - !Ref AdeLayer
      Environment:
        Variables:
          S3_BUCKET: !Ref DataBucketName
          ADE_API_KEY: !Ref AdeApiKey
          ANTHROPIC_API_KEY: !Ref ClaudeApiKey
          CLAUDE_MODEL: claude-sonnet-4-6
          REPORT_TEMPLATES_PREFIX: report-templates/
          TEMPLATE_WRITER_FUNCTION: !Ref TemplateWriterFunction
      Policies:
        - Version: '2012-10-17'
          Statement:
            - Effect: Allow
              Action:
                - s3:GetObject
                - s3:PutObject
              Resource: !Sub arn:aws:s3:::${DataBucketName}/report-templates/*
        - LambdaInvokePolicy:
            FunctionName: !Ref TemplateWriterFunction
```

- [ ] **Step 5: Add `TemplateWriterFunction`** (in-VPC, `HasDb`) after `SuggestionWriterFunction` (template.yaml ~line 1256). Mirrors SuggestionWriterFunction verbatim (in-VPC write hop, no S3, no Events):
```yaml
  # ----------------------------------------------------------
  # Lambda: Template Writer (Template Agent Phase 1) — in-VPC write hop.
  # Async-invoked by the non-VPC AnalyzeTemplateFunction to LAND the ADE schema
  # + proposed mapping in Aurora report_template (BUG-36). Mirrors
  # SuggestionWriterFunction's Layers/VpcConfig/PG env verbatim. HasDb. No Events.
  # ----------------------------------------------------------
  TemplateWriterFunction:
    Type: AWS::Serverless::Function
    Condition: HasDb
    Properties:
      FunctionName: !Sub ["${P}-template-writer", {P: !FindInMap [StageConfig, !Ref Stage, Prefix]}]
      CodeUri: src/
      Handler: lambda_template_writer.lambda_handler
      Timeout: 60
      MemorySize: 512
      Layers:
        - !Ref PsycopgLayer
      VpcConfig:
        SubnetIds: !Ref DbSubnetIds
        SecurityGroupIds:
          - !ImportValue
            Fn::Sub: "${DbStackName}-LambdaSG"
      Environment:
        Variables:
          PGHOST: !ImportValue
            Fn::Sub: "${DbStackName}-ClusterEndpoint"
          PGDATABASE: !ImportValue
            Fn::Sub: "${DbStackName}-DbName"
          PGUSER: postgres
          PGPASSWORD: !Sub '{{resolve:secretsmanager:${DbSecretArn}:SecretString:password}}'
      Policies:
        - VPCAccessPolicy: {}
```

- [ ] **Step 6: Wire the org-api function.** In `OrgApiFunction.Environment.Variables` (template.yaml ~line 849, after `GRADED_ROLES`) add:
```yaml
          # Template Agent (Phase 1): async-invoke target + upload prefix.
          ANALYZE_TEMPLATE_FUNCTION: !Ref AnalyzeTemplateFunction
          REPORT_TEMPLATES_PREFIX: report-templates/
```
In `OrgApiFunction.Policies` (after the `voice/*` statement, ~line 887) add an S3 grant + a Lambda invoke grant:
```yaml
            - Effect: Allow
              # Template Agent: presigned PUT/GET of client report templates
              # under a DEDICATED report-templates/ prefix (matches no
              # wire-s3-events.sh trigger). Signing is offline, but the role
              # must be able to Put/Get there for the signed URL to be valid.
              Action:
                - s3:PutObject
                - s3:GetObject
              Resource: !Sub arn:aws:s3:::${DataBucketName}/report-templates/*
        - LambdaInvokePolicy:
            FunctionName: !Ref AnalyzeTemplateFunction
```
> NOTE: `LambdaInvokePolicy` is a SAM policy template and sits at the same list level as the inline `Version: '2012-10-17'` policy block — append it as a sibling list item under `Policies`, not inside the `Statement` array.

- [ ] **Step 7: Pass `AdeApiKey` in the deploy workflows.** In `.github/workflows/deploy.yml` and `.github/workflows/deploy-prod.yml`, add to the `sam deploy ... --parameter-overrides` line (mirroring how `ClaudeApiKey=${{ secrets.ANTHROPIC_API_KEY }}` is passed):
```
AdeApiKey=${{ secrets.ADE_API_KEY }}
```
(Add the `ADE_API_KEY` GitHub Actions secret to the repo — note this as a human prerequisite in the PR description.)

- [ ] **Step 8: Validate the template** (run where AWS SAM CLI is available — CI, or a Linux/devcontainer shell; the Windows host has no local Python, BUG-29):
  `sam validate --lint --template src/template.yaml`
  Expected: "template is valid" (the only expected warnings are the pre-suppressed W1001/W1030; the new cross-condition `TEMPLATE_WRITER_FUNCTION`/`AnalyzeTemplateFunction` Refs fall under the existing top-of-file W1001 suppression).

- [ ] **Step 9: Commit:**
```bash
git add src/template.yaml infra/ade-layer/requirements.txt .github/workflows/deploy.yml .github/workflows/deploy-prod.yml
git commit -m "SAM: AdeApiKey + AdeLayer + AnalyzeTemplate/TemplateWriter functions + org-api template wiring"
```

---

### Task 10: UI — real backend client (`templates-store.js`)

> UI is the browser React prototype (no build step, no test runner). The gate is `node --check` on every modified `.js` + a `?v=N` cache-buster bump. Real-browser verification is deferred to the user (state it explicitly).

**Files:**
- Create: `scripts/api/templates-store.js` (real org-endpoint client; loaded alongside the existing localStorage mock `scripts/api/template-store.js`).
- Modify: `app-shell-preview.html` (add `<script src="scripts/api/templates-store.js?v=1">` after `api/org.js`; bump the changed file's cache-buster).

**Interfaces:**
- Consumes: `window.FS.api.orgRequest`, `window.FS.api.orgLive`/`orgWrite` gating (via the same `!api.useMocks && !!api.orgBaseUrl` check org.js uses); the presigned-PUT-then-plain-`fetch` upload pattern from `org.js:uploadImage`.
- Produces `window.FS.api.reportTemplates`:
  - `create({title, contentType, siteId})` → `POST /templates` → `{templateId, uploadUrl, s3Key}`
  - `uploadArtifact(uploadUrl, file)` → plain `fetch` PUT (no auth/JSON headers — S3 signature)
  - `analyze(id)` → `POST /templates/{id}/analyze`
  - `list()` → `GET /templates`
  - `get(id)` → `GET /templates/{id}` → `{template, originalUrl}`
  - `confirmMapping(id, mappings)` → `PATCH /templates/{id}/mapping`

- [ ] **Step 1: Create `scripts/api/templates-store.js`:**
```javascript
/* ==========================================================================
   api/templates-store.js — Template Agent Phase 1 real backend client.
   Talks to the org API (/api/org/templates*) when org LIVE; otherwise no-ops
   so the localStorage prototype (api/template-store.js) stays the fallback.
   Load AFTER api/index.js, _fetch.js, api/org.js.
   ========================================================================== */
(function () {
  'use strict';
  if (!window.FS) window.FS = {};
  if (!window.FS.api) window.FS.api = {};
  var api = window.FS.api;

  function orgLive()  { return !api.useMocks && !!api.orgBaseUrl; }
  function orgWrite() { return orgLive() && !!api.orgWrites; }

  var CONTENT_TYPES = {
    docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    pdf:  'application/pdf'
  };

  async function create(opts) {
    opts = opts || {};
    if (!orgWrite()) return { templateId: null, uploadUrl: null, s3Key: null };
    return api.orgRequest('/templates', { method: 'POST', body: {
      title: opts.title, contentType: opts.contentType, siteId: opts.siteId || null
    }});
  }

  async function uploadArtifact(uploadUrl, file) {
    // Plain fetch (NOT orgRequest): a presigned PUT must not carry an
    // Authorization header or JSON body/Content-Type override, or the S3
    // signature breaks (same rule as org.js uploadImage).
    var resp = await fetch(uploadUrl, {
      method: 'PUT', headers: { 'Content-Type': file.type }, body: file });
    if (!resp.ok) throw new Error('Template upload failed (' + resp.status + ')');
    return true;
  }

  async function analyze(id) {
    if (!orgWrite()) return { ok: false, status: 'mocked' };
    return api.orgRequest('/templates/' + encodeURIComponent(id) + '/analyze', { method: 'POST' });
  }

  async function list() {
    if (!orgLive()) return { templates: [] };
    return api.orgRequest('/templates');
  }

  async function get(id) {
    if (!orgLive()) return { template: null, originalUrl: null };
    return api.orgRequest('/templates/' + encodeURIComponent(id));
  }

  async function confirmMapping(id, mappings) {
    if (!orgWrite()) return { template: null };
    return api.orgRequest('/templates/' + encodeURIComponent(id) + '/mapping',
      { method: 'PATCH', body: { mappings: mappings } });
  }

  api.reportTemplates = {
    CONTENT_TYPES: CONTENT_TYPES,
    create: create, uploadArtifact: uploadArtifact, analyze: analyze,
    list: list, get: get, confirmMapping: confirmMapping
  };
})();
```

- [ ] **Step 2: Register the script** in `app-shell-preview.html` — add after the `api/org.js` line:
```html
    <script src="scripts/api/templates-store.js?v=1"></script>
```

- [ ] **Step 3: Syntax-check:**
  `node --check scripts/api/templates-store.js`
  Expected: no output (exit 0).

- [ ] **Step 4: Commit:**
```bash
git add scripts/api/templates-store.js app-shell-preview.html
git commit -m "UI: real org-API template client (create/upload/analyze/list/get/confirmMapping)"
```

---

### Task 11: UI — mapping-review composite (bounding-box overlay + confirm)

> **SPIKE-PIN:** the overlay's background depends on whether ADE returns per-page preview images (Phase 0 Step 4). This composite renders boxes over a page-preview `<img>` when `originalUrl`/a page image is available, and **falls back to a boxed list on a plain canvas** when it is not. Pin the image source after the spike.

**Files:**
- Create: `scripts/composites/template-mapping-review.js`.
- Modify: `styles/components.css` (append `.fs-tpl-review*`), `components-preview.html` (register + smoke render + `?v=N` bump), `app-shell-preview.html` (load the composite + bump).

**Interfaces:**
- Consumes: `window.FS.api.reportTemplates.get` / `confirmMapping` (Task 10); `template_mapping.DATA_SOURCES` sources — mirrored as a JS constant `SOURCES` (kept in sync with `src/template_mapping.py` `DATA_SOURCES`); the stored `ade_schema.slots[].box` normalized 0..1 coords.
- Produces `window.FieldSight.TemplateMappingReview` — a React component `props: { template, onConfirmed }` that renders each slot's box over the page, a mapping row per slot (source `<select>` + instruction `<input>` + confidence badge, low-confidence flagged), and a Confirm button calling `confirmMapping`.

- [ ] **Step 1: Create `scripts/composites/template-mapping-review.js`:**
```javascript
/* ==========================================================================
   composites/template-mapping-review.js — Template Agent Phase 1.
   Renders the ADE-detected slots as bounding boxes over the template page(s)
   and an editable mapping row per slot; Confirm freezes the mapping via
   FS.api.reportTemplates.confirmMapping. Low-confidence slots are flagged.
   SPIKE-PIN: page background image source depends on ADE preview-image output.
   ========================================================================== */
(function () {
  'use strict';
  var e = React.createElement;

  // Keep in sync with src/template_mapping.py DATA_SOURCES.
  var SOURCES = ['none', 'topics', 'action_items', 'findings',
                 'safety_observations', 'topic_photos', 'weather', 'site_meta'];
  var KINDS = ['narrative', 'structured'];
  var LOW_CONF = 0.6;

  function boxStyle(box) {
    if (!box) return { display: 'none' };
    // box is normalized 0..1 (l,t,r,b); percentage-position over the page frame.
    return {
      position: 'absolute',
      left:   (box.l * 100) + '%',
      top:    (box.t * 100) + '%',
      width:  ((box.r - box.l) * 100) + '%',
      height: ((box.b - box.t) * 100) + '%'
    };
  }

  function TemplateMappingReview(props) {
    var template = props.template || {};
    var slots = ((template.ade_schema || {}).slots) || [];
    var proposed = ((template.field_mapping || {}).mappings) || [];
    var initial = {};
    slots.forEach(function (s) {
      var m = proposed.find(function (x) { return x.slot_id === s.slot_id; }) || {};
      initial[s.slot_id] = {
        slot_id: s.slot_id,
        kind: KINDS.indexOf(m.kind) >= 0 ? m.kind : 'structured',
        source: SOURCES.indexOf(m.source) >= 0 ? m.source : 'none',
        instruction: m.instruction || '',
        confidence: typeof m.confidence === 'number' ? m.confidence : 0
      };
    });
    var st = React.useState(initial);
    var rows = st[0], setRows = st[1];
    var busy = React.useState(false);
    var isBusy = busy[0], setBusy = busy[1];

    function update(slotId, field, value) {
      setRows(Object.assign({}, rows, (function () {
        var next = {}; next[slotId] = Object.assign({}, rows[slotId]); next[slotId][field] = value; return next;
      })()));
    }

    async function confirm() {
      setBusy(true);
      try {
        var payload = slots.map(function (s) { return rows[s.slot_id]; });
        var res = await window.FS.api.reportTemplates.confirmMapping(template.id, payload);
        if (props.onConfirmed) props.onConfirmed(res && res.template);
      } catch (err) {
        window.alert('Confirm failed: ' + (err && err.message || err));
      } finally { setBusy(false); }
    }

    var pageFrame = e('div', { className: 'fs-tpl-review__page' },
      // SPIKE-PIN: use a page preview <img> when ADE returns one; else boxes on blank canvas.
      (props.pageImageUrl ? e('img', { src: props.pageImageUrl, className: 'fs-tpl-review__page-img', alt: 'template page' }) : null),
      slots.map(function (s) {
        return e('div', { key: s.slot_id, className: 'fs-tpl-review__box', style: boxStyle(s.box), title: s.text },
          e('span', { className: 'fs-tpl-review__box-tag' }, s.slot_id));
      })
    );

    var mappingRows = slots.map(function (s) {
      var r = rows[s.slot_id];
      var low = r.confidence < LOW_CONF;
      return e('div', { key: s.slot_id, className: 'fs-tpl-review__row' + (low ? ' fs-tpl-review__row--low' : '') },
        e('div', { className: 'fs-tpl-review__slot' }, s.slot_id + ' · ' + (s.kind || '') , e('span', { className: 'fs-tpl-review__slot-text' }, ' ' + (s.text || '').slice(0, 60))),
        e('select', { value: r.source, disabled: isBusy,
          onChange: function (ev) { update(s.slot_id, 'source', ev.target.value); } },
          SOURCES.map(function (src) { return e('option', { key: src, value: src }, src); })),
        e('select', { value: r.kind, disabled: isBusy,
          onChange: function (ev) { update(s.slot_id, 'kind', ev.target.value); } },
          KINDS.map(function (k) { return e('option', { key: k, value: k }, k); })),
        e('input', { type: 'text', value: r.instruction, placeholder: 'fill instruction', disabled: isBusy,
          onChange: function (ev) { update(s.slot_id, 'instruction', ev.target.value); } }),
        e('span', { className: 'fs-tpl-review__conf' + (low ? ' fs-tpl-review__conf--low' : '') }, Math.round(r.confidence * 100) + '%')
      );
    });

    return e('div', { className: 'fs-tpl-review' },
      e('div', { className: 'fs-tpl-review__canvas' }, pageFrame),
      e('div', { className: 'fs-tpl-review__panel' },
        e('h3', null, 'Review field mapping'),
        mappingRows,
        e('button', { className: 'fs-btn fs-btn--primary', disabled: isBusy, onClick: confirm },
          isBusy ? 'Confirming…' : 'Confirm mapping')
      )
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.TemplateMappingReview = TemplateMappingReview;
})();
```

- [ ] **Step 2: Append styles** to `styles/components.css` (tokens only — no hardcoded hex; use existing `--surface-*`/`--border-*`/`--color-danger-*` tokens):
```css
/* Template Agent — mapping review overlay (Phase 1) */
.fs-tpl-review { display: grid; grid-template-columns: minmax(0, 1fr) minmax(320px, 420px); gap: var(--space-4); }
.fs-tpl-review__page { position: relative; width: 100%; aspect-ratio: 1 / 1.414; background: var(--surface-panel); border: 1px solid var(--border-default); }
.fs-tpl-review__page-img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; }
.fs-tpl-review__box { position: absolute; border: 2px solid var(--color-accent-500); background: rgba(255, 107, 53, 0.08); }
.fs-tpl-review__box-tag { position: absolute; top: -1.2em; left: 0; font-size: 11px; background: var(--color-accent-500); color: #fff; padding: 0 4px; }
.fs-tpl-review__row { display: grid; grid-template-columns: 1fr 1fr auto; gap: var(--space-2); align-items: center; padding: var(--space-2) 0; border-bottom: 1px solid var(--border-subtle); }
.fs-tpl-review__row--low { background: var(--color-danger-50); }
.fs-tpl-review__conf--low { color: var(--color-danger-700); font-weight: 600; }
```
> NOTE: if a referenced token name differs in `tokens.css`, use the nearest existing token (grep `--color-accent`, `--surface-panel`, `--border-subtle` first). Do not introduce new tokens for Phase 1.

- [ ] **Step 3: Register in `components-preview.html`** (add the `<script src="scripts/composites/template-mapping-review.js?v=1">` line + a smoke render with a 2-slot fixture template) and load it in `app-shell-preview.html`; bump the `?v=N` on both changed files.

- [ ] **Step 4: Syntax-check every modified JS:**
  `node --check scripts/composites/template-mapping-review.js`
  Expected: no output (exit 0).

- [ ] **Step 5: Commit:**
```bash
git add scripts/composites/template-mapping-review.js styles/components.css components-preview.html app-shell-preview.html
git commit -m "UI: template mapping-review composite (ADE box overlay + editable mapping + confirm)"
```

---

## Out of scope for THIS plan — Phase 2 (Fill + Render)

Deliberately deferred: per-(site, period) agentic fill (deterministic for structured slots, per-section grounded Claude for narrative slots), `.docx` fill-into render (pixel-faithful) + non-`.docx` structure-regeneration fallback, and S3 output + download (spec §3.2 stage 3-4, §3.5, D3/D4). **Phase 2 gets its own plan** once Phase 1 + the Phase 0 spike have settled the real ADE schema shape (the fill logic reads `ade_schema.slots` + the confirmed `field_mapping`, both of whose concrete shapes this plan pins).

---

## Self-Review

**1. Spec coverage (Phase 0 + Phase 1 only):**
- §6 Phase 0 spike (obtain key, run `.docx`+PDF, inspect structure/confidence, go/no-go, primary vs fallback, throwaway, human supplies samples) → **Phase 0** section (prerequisite callout + 6 investigative steps + explicit STOP-and-reassess gate). ✅
- §3.1 `report_template` entity (original artifact S3, ADE schema, field-mapping, scope) → Task 1 (table) + Task 2 (repo). ✅
- §3.2 Stage 1 Analyze (ADE, non-VPC, zero-shot) → Task 3 (ade_client) + Task 6 (non-VPC analyzer). ✅
- §3.2 Stage 2 Map (Claude proposes given ADE schema + data-source catalogue; human confirms; low-confidence flagged; frozen) → Task 4 (catalogue + proposer) + Task 8 (confirm/freeze) + Task 11 (review UI, low-conf flag). ✅
- §3.3 FieldSight data sources catalogue → `template_mapping.DATA_SOURCES` (Task 4), mirrored in UI `SOURCES` (Task 11). ✅
- §3.4 VPC split (ADE/Claude non-VPC; Aurora write in-VPC; no in-VPC outbound — BUG-36) → Task 5 (in-VPC writer) + Task 6 (non-VPC analyzer) + Task 9 (SAM: non-VPC AnalyzeTemplate, in-VPC TemplateWriter). ✅
- §3.6 storage (artifact in S3 reports-adjacent prefix; report_template in Aurora) → `report-templates/` prefix (Task 7) + `report_template` table (Task 1). ✅
- Upload (presigned PUT, reuse recordings pattern) → Task 7 `create_template_endpoint` + Task 10 `uploadArtifact`. ✅
- Mapping review UI with ADE bounding boxes → Task 11. ✅
- ADE key as SAM secret (mirror ClaudeApiKey) → Task 9 `AdeApiKey`. ✅
- Data-governance surfacing → Global Constraints + Phase 0 prerequisite note. ✅
- Phase 2 explicitly out of scope → dedicated "Out of scope" section. ✅

**2. Placeholder scan:** No "TBD"/"implement later"/"add error handling"/"similar to Task N". Every code step carries complete code; every test step carries real assertions; every run step carries an exact command + expected result. The only intentional markers are the clearly-labelled `### PIN` / "SPIKE-PIN" flags (required by the brief: concrete code against a stated assumed interface, flagged for post-spike reconciliation) — these are executable as written, not blanks. ✅

**3. Type/name consistency:**
- `report_templates` function names identical across Task 2 (def), Task 5 (`set_analysis`/`set_status`), Task 7/8 (`create_template`/`list_templates`/`get_template`/`confirm_mapping`). ✅
- Normalized `ade_schema` shape (`slots[].slot_id/kind/box/confidence/table`) consistent across Task 3 (producer), Task 4 (`build_mapping_prompt`/`parse_mapping` consumer), Task 11 (UI consumer). ✅
- `field_mapping` shape (`{version, confirmed, mappings:[{slot_id,kind,source,instruction,confidence}]}`) consistent across Task 4 (proposer), Task 8 (`_validate_confirmed_mapping`), Task 11 (UI rows). ✅
- Analyzer→writer payload (`{template_id, ade_schema, field_mapping}` / `{template_id, error_note}`) matches between Task 6 (`_invoke_writer`) and Task 5 (`lambda_handler`). ✅
- org-api→analyzer payload (`{template_id, company_id, original_s3_key, original_format}`) matches between Task 8 (`analyze_template_endpoint`) and Task 6 (`lambda_handler` event reads). ✅
- `VALID_SOURCES`/`VALID_KINDS` defined in Task 4, imported/used in Task 8. ✅
- Migration number: **0019** used consistently (Task 1, File Structure, Global Constraints); 0018 reserved by the sibling site-location-weather plan. ✅

No inconsistencies found.
