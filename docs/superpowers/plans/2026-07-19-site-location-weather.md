# Site Location & Weather Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drive weather from each site's real address — store `latitude`/`longitude` on `sites`, resolve them via Photon geocoding (autocomplete on create/edit + non-VPC backfill for existing rows), point the UI weather indicator at the real coordinate, and inject a factual weather block into the AI daily report so the AI correlates weather with observations.

**Architecture:** Two nullable coordinate columns on Aurora `sites`, threaded through the psycopg3 repo and the in-VPC org-api create/patch handlers (persist-only — the in-VPC org-api never calls out, BUG-36). Coordinates are produced entirely outside the VPC: the browser's Photon autocomplete on site create/edit (primary), and a non-VPC backfill helper for legacy coord-less sites (secondary). Weather is fetched only in a non-VPC context (Open-Meteo, keyless): the UI indicator fetches client-side for the active site's real coord, and the already-non-VPC `ReportGeneratorFunction` fetches once per (site, date) and injects the block into its Claude prompt.

**Tech Stack:** Python 3.12 Lambdas, psycopg3 (`dict_row`), `db/migrate.py` versioned SQL runner, `urllib3` for HTTP (Photon + Open-Meteo), pytest (`uv run pytest`, `pythonpath=["src"]`), no-build browser React UI (`fieldsight-ui`, `node --check` gate).

## Global Constraints

*(Every task's requirements implicitly include this section. Values copied verbatim from the spec and repo memory.)*

- **In-VPC org-api NEVER makes an outbound call (BUG-36).** `OrgApiFunction` has `VpcConfig` and no NAT/egress; any external HTTP (Photon/Open-Meteo) black-holes until timeout with zero logs. Coordinates arrive already-resolved in the request body; the handler only persists. All geocoding/weather HTTP happens in the browser or a non-VPC Lambda.
- **Providers are keyless + cached.** Photon (`https://photon.komoot.io/api?q=<query>&limit=5&lang=en`) and Open-Meteo (`api.open-meteo.com` / `archive-api.open-meteo.com`) — no API key, no billing. Geocode **once per site**; weather cached **once per (site, date)** (report weather lives on the report record; UI weather uses the existing client-side `weatherFetchCache`).
- **Pipeline test gate:** `uv run pytest` (config: `pyproject.toml` `[tool.pytest.ini_options]`, `pythonpath=["src"]`, `testpaths=["tests"]`). Integration tests are marked `pytest.mark.integration` and auto-SKIP unless `TEST_DATABASE_URL` is set (`tests/conftest.py`). Unit tests use `FakeConn`/`FakeCursor` doubles or `pytest.importorskip`.
- **UI has no test runner / no build step.** `fieldsight-ui/CLAUDE.md`: "No tests, no linter, no formatter configured." Do NOT introduce npm/webpack/jest. UI verification = `node --check <file.js>` + grep pre-/post-checks + a manual verification checklist. Bump `?v=N` cache-busters in preview HTMLs whenever a loaded `.js` changes. Respect **BUG-19** (never `new Date('YYYY-MM-DD')`; use `FS.api.todayNZDT()`) and **BUG-20** (a 200 `text/html` body is the SPA shell, not JSON — the existing `_fetch.js` guard handles it; don't bypass).
- **New dev artifacts (comments, commit messages, docs, code) in ENGLISH** (memory: from 2026-07-15).
- **Pipeline git hygiene:** branch off `develop` (NOT off the `docs/*` branch this plan lives on). Never `git add -A` on the pipeline repo — stage explicit paths only. Windows `autocrlf=true`; use **single-line Edit anchors** to avoid CRLF match failures. Commit per task.

---

## File Structure

| Repo | File | Create/Modify | Responsibility |
|---|---|---|---|
| pipeline | `src/migrations/0018_site_coordinates.sql` | Create | Add nullable `sites.latitude` / `sites.longitude` (double precision). |
| pipeline | `src/repositories/sites.py` | Modify | Add `latitude`/`longitude` to `_COLS`, `create_site(...)`, `update_site(...)`. |
| pipeline | `tests/integration/test_core_repositories.py` | Modify | Roundtrip: create/update persists coords. |
| pipeline | `tests/integration/test_migrations_apply.py` | Modify | Assert `sites` has the two new columns after migration. |
| pipeline | `src/lambda_org_api.py` | Modify | `create_org_site` / `patch_org_site` validate + thread `latitude`/`longitude` from body (persist-only). |
| pipeline | `tests/unit/test_lambda_org_api.py` | Modify | Handlers pass coords to repo; reject non-numeric / out-of-range. |
| pipeline | `src/geocode.py` | Create | Pure `parse_photon_features()` + `geocode(query, http=None)` → `{formatted, lat, lng}` (Photon, non-VPC). |
| pipeline | `tests/unit/test_geocode.py` | Create | Parse GeoJSON; geocode via injected FakeHTTP. |
| pipeline | `src/backfill_site_coords.py` | Create | Pure `plan_coordinate_backfill(sites, geocode_fn)` + thin non-VPC runner (PATCHes org-api). |
| pipeline | `tests/unit/test_backfill_site_coords.py` | Create | Planner skips coord-less/address-less/miss cases; emits updates. |
| pipeline | `src/weather.py` | Create | Pure `normalize_weather()`, `weather_prompt_block()`, `WMO_LABELS`, `fetch_weather(lat,lng,date,today_iso,http=None)` (Open-Meteo, non-VPC). |
| pipeline | `tests/unit/test_weather.py` | Create | Normalize daily block; prompt text; archive-vs-forecast URL selection. |
| pipeline | `src/lambda_report_generator.py` | Modify | `build_weather_block_for_site()`; fetch weather from `sites_info` coord; set `report['weather']`; append `weather_prompt_block` to the Claude prompt. |
| pipeline | `tests/unit/test_lambda_report_generator.py` | Create | `build_weather_block_for_site` graceful-null + coord passthrough. |
| ui | `fieldsight-ui/scripts/app-shell.js` | Modify | `WeatherIndicator` resolves the active site's real `{latitude, longitude}` via `org.getOrgSites()` (cached), falls back to fixture coord then default. |
| ui | `fieldsight-ui/scripts/pages/sites.js` | Modify | `NewProjectModal` + `EditProjectModal` address field → Photon type-ahead; on pick fill address + stash `latitude`/`longitude`; submit them. |
| ui | `fieldsight-ui/scripts/api/org.js` | Modify | `createOrgSite`/`updateOrgSite` forward `latitude`/`longitude` (verify passthrough; add a small `geocodeAddress()` Photon helper). |
| ui | `fieldsight-ui/app-shell-preview.html` | Modify | Bump `?v=N` cache-busters for the two changed scripts. |

---

## Decisions locked during investigation

- **D-COORD — how the non-VPC report generator gets the site coordinate (spec §3.4's one open wiring choice): OPTION (a) — read `latitude`/`longitude` from the `sites` block of `config/user_mapping.json`.**
  Justification from the trigger path: the daily report is fired by an **EventBridge cron** (`ReportGeneratorFunction` → `DailyReportSchedule`, `cron(0 16 * * ? *)`, `Input: '{"report_type":"daily"}'` — `src/template.yaml:567-573`) with a **static payload**. No in-VPC step invokes the generator holding Aurora coords, so option (b) (coords in the invocation payload) would require rewiring a working cron into an in-VPC per-(site,date) fan-out — disproportionate for a coordinate hand-off. The generator **already** loads `sites_info = full.get('sites', {})` and resolves `user_site_info = sites_info.get(user_site_id, {})` (`lambda_report_generator.py:333-335,1272-1274`); reading `user_site_info.get('latitude')/.get('longitude')` adds **zero new read-side plumbing**.
  Populating that config: `config/user_mapping.json` is a hand-maintained S3 file (nothing in code writes it — confirmed by grep). Coords enter its `sites` block via the **non-VPC backfill helper** (Task 3) and/or hand-maintenance alongside the existing per-site `name`. **Known MVP gap (accepted, per spec §2 non-goal "no historical backfill"):** a site created via the UI gets coords in Aurora immediately (weather indicator works), but its AI report includes weather only once the coord is present in `config/user_mapping.json`'s `sites` block (next backfill run / config update). **Alternative not chosen:** option (b) — rewire the daily trigger to an in-VPC fan-out passing `lat/lng` per invocation.
- **Weather-fetch locus (spec §3.4):** confirmed `ReportGeneratorFunction` is **non-VPC** (no `VpcConfig` in `src/template.yaml:531-587`; it already makes the daily Claude call via `urllib3`). It is the single home for the Open-Meteo fetch + AI correlation. `OrgApiFunction` (`:823`) and `ItemWriterFunction` (`:1172`) are in-VPC; `ExtractSessionFunction` is non-VPC.
- **UI weather indicator source:** `_toPageSite` (`org.js:58`) is `Object.assign({}, s, {…})`, so it already passes `latitude`/`longitude` through once the backend returns them. The indicator resolves the active site's coord from `org.getOrgSites()`.

---

## Task 1: Migration 0018 + `sites` repo coordinate columns

**Files:**
- Create: `src/migrations/0018_site_coordinates.sql`
- Modify: `src/repositories/sites.py:3` (`_COLS`), `:6-12` (`create_site`), `:105-120` (`update_site`)
- Test: `tests/integration/test_migrations_apply.py`, `tests/integration/test_core_repositories.py`

**Interfaces:**
- Produces: `sites.create_site(conn, company_id, name, location=None, client=None, industry=None, icon_s3_key=None, slug=None, address=None, latitude=None, longitude=None) -> dict` (returned dict now includes `latitude`, `longitude`).
- Produces: `sites.update_site(conn, site_id, company_id, name=None, location=None, client=None, industry=None, address=None, latitude=None, longitude=None) -> dict | None` (None = leave column unchanged, COALESCE semantics).

- [ ] **Step 1: Write the failing integration tests**

Add to `tests/integration/test_migrations_apply.py`:

```python
def test_sites_has_coordinate_columns():
    # 0018: nullable lat/lng for weather + map features.
    conn = _fresh_conn()
    try:
        apply_migrations(conn, MIGRATIONS_DIR)
        cols = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='sites'"
            ).fetchall()
        }
        assert {"latitude", "longitude"} <= cols
    finally:
        conn.close()
```

Add to `tests/integration/test_core_repositories.py` (inside the existing module, after `test_company_user_site_roundtrip`):

```python
def test_site_coordinates_create_and_update(db):
    co = companies.create_company(db, "GeoCo")
    s = sites.create_site(db, co["id"], "Depot", address="1 Colombo St",
                          latitude=-43.5321, longitude=172.6362)
    assert s["latitude"] == -43.5321 and s["longitude"] == 172.6362
    got = sites.get_site(db, s["id"])
    assert got["latitude"] == -43.5321 and got["longitude"] == 172.6362

    # update_site: None leaves a column unchanged (COALESCE), a value overwrites
    upd = sites.update_site(db, s["id"], co["id"], latitude=-41.2865, longitude=174.7762)
    assert upd["latitude"] == -41.2865 and upd["longitude"] == 174.7762
    assert upd["address"] == "1 Colombo St"  # untouched column preserved
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_migrations_apply.py::test_sites_has_coordinate_columns tests/integration/test_core_repositories.py::test_site_coordinates_create_and_update -v`
Expected (with `TEST_DATABASE_URL` set): FAIL — migration test fails `assert {'latitude','longitude'} <= cols`; roundtrip fails with `create_site() got an unexpected keyword argument 'latitude'`.
Expected (without `TEST_DATABASE_URL`): both SKIP ("TEST_DATABASE_URL not set; skipping DB integration test") — that is the expected local outcome; run with the CI DB to see them fail-then-pass.

- [ ] **Step 3: Write the migration file**

Create `src/migrations/0018_site_coordinates.sql`:

```sql
-- 0018 (next free number; 0017 is the action_item audit): site coordinates for
-- weather + map features. Nullable — existing rows predate coordinates and are
-- backfilled via Photon (non-VPC). Populated on create/edit by the UI's Photon
-- autocomplete pick; the in-VPC org-api only persists (never geocodes).
ALTER TABLE sites ADD COLUMN latitude  double precision;
ALTER TABLE sites ADD COLUMN longitude double precision;
```

- [ ] **Step 4: Extend the repo `_COLS` (single-line Edit anchor)**

In `src/repositories/sites.py` replace the exact line:

```python
_COLS = "id, company_id, name, location, client, industry, icon_s3_key, created_at, archived_at, slug, address"
```

with:

```python
_COLS = "id, company_id, name, location, client, industry, icon_s3_key, created_at, archived_at, slug, address, latitude, longitude"
```

- [ ] **Step 5: Extend `create_site`**

In `src/repositories/sites.py` replace the `create_site` body (lines 6-12) with:

```python
def create_site(conn, company_id, name, location=None, client=None,
                industry=None, icon_s3_key=None, slug=None, address=None,
                latitude=None, longitude=None) -> dict:
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO sites (company_id, name, location, client, industry, icon_s3_key, slug, address, latitude, longitude) "
        f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_COLS}",
        (company_id, name, location, client, industry, icon_s3_key, slug, address, latitude, longitude),
    ).fetchone()
```

- [ ] **Step 6: Extend `update_site`**

In `src/repositories/sites.py` replace the `update_site` function (lines 105-120) with:

```python
def update_site(conn, site_id, company_id, name=None, location=None,
                client=None, industry=None, address=None,
                latitude=None, longitude=None) -> dict | None:
    """None = leave unchanged (same semantics as users.update_profile).
    Company-guarded; archived sites are not editable."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE sites SET "
        f"  name=COALESCE(%(name)s, name), "
        f"  location=COALESCE(%(loc)s, location), "
        f"  client=COALESCE(%(client)s, client), "
        f"  industry=COALESCE(%(ind)s, industry), "
        f"  address=COALESCE(%(addr)s, address), "
        f"  latitude=COALESCE(%(lat)s, latitude), "
        f"  longitude=COALESCE(%(lng)s, longitude) "
        f"WHERE id=%(sid)s AND company_id=%(cid)s AND archived_at IS NULL "
        f"RETURNING {_COLS}",
        {"sid": site_id, "cid": company_id, "name": name, "loc": location,
         "client": client, "ind": industry, "addr": address,
         "lat": latitude, "lng": longitude},
    ).fetchone()
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_migrations_apply.py::test_sites_has_coordinate_columns tests/integration/test_core_repositories.py::test_site_coordinates_create_and_update -v`
Expected (CI DB): PASS (2 passed). Locally without `TEST_DATABASE_URL`: SKIPPED — acceptable; the CI gate exercises them.
Also run the whole repo/migration suites to prove nothing regressed: `uv run pytest tests/integration/test_core_repositories.py tests/integration/test_migrations_apply.py -q`.

- [ ] **Step 8: Commit**

```bash
git add src/migrations/0018_site_coordinates.sql src/repositories/sites.py tests/integration/test_migrations_apply.py tests/integration/test_core_repositories.py
git commit -m "feat(sites): add nullable latitude/longitude columns + repo threading (0018)"
```

---

## Task 2: org-api create/patch handlers accept + persist coordinates

**Files:**
- Modify: `src/lambda_org_api.py:523-562` (`create_org_site`), `:565-597` (`patch_org_site`)
- Test: `tests/unit/test_lambda_org_api.py`

**Interfaces:**
- Consumes: `sites.create_site(..., latitude=, longitude=)` and `sites.update_site(..., latitude=, longitude=)` from Task 1.
- Produces: `POST /api/org/sites` and `PATCH /api/org/sites/{id}` accept optional numeric `latitude`/`longitude` in the JSON body, validate range, and pass them to the repo. A module-level helper `_coerce_coord(value, lo, hi, label) -> (float | None, error_or_None)`.
- **Constraint reminder:** org-api is in-VPC — this handler must NOT geocode or make any outbound call; it persists the coords the UI already resolved via Photon.

- [ ] **Step 1: Write the failing unit tests**

Add to `tests/unit/test_lambda_org_api.py` (after `test_create_site_admin_ok`, reusing the file's `wired`, `make_event`, `body_of`, `CALLER`):

```python
def test_create_site_persists_coordinates(wired):
    created = {}

    def fake_create(conn, company_id, name, location=None, client=None,
                    industry=None, icon_s3_key=None, address=None,
                    latitude=None, longitude=None):
        created.update(latitude=latitude, longitude=longitude, address=address)
        return {"id": "s-geo", "company_id": company_id, "name": name,
                "latitude": latitude, "longitude": longitude}

    wired.setattr(org.sites, "create_site", fake_create)
    res = org.lambda_handler(make_event("POST", "/api/org/sites", body={
        "name": "Geo Site", "address": "1 Colombo St",
        "latitude": -43.5321, "longitude": 172.6362}), None)
    assert res["statusCode"] == 201
    assert created == {"latitude": -43.5321, "longitude": 172.6362,
                       "address": "1 Colombo St"}
    assert body_of(res)["latitude"] == -43.5321


def test_create_site_rejects_non_numeric_latitude(wired):
    called = []
    wired.setattr(org.sites, "create_site",
                  lambda *a, **k: called.append(1) or {"id": "x"})
    res = org.lambda_handler(make_event("POST", "/api/org/sites", body={
        "name": "Bad", "latitude": "not-a-number", "longitude": 10}), None)
    assert res["statusCode"] == 400
    assert called == []  # never reached the repo


def test_create_site_rejects_out_of_range_longitude(wired):
    wired.setattr(org.sites, "create_site", lambda *a, **k: {"id": "x"})
    res = org.lambda_handler(make_event("POST", "/api/org/sites", body={
        "name": "Bad", "latitude": -43.5, "longitude": 999}), None)
    assert res["statusCode"] == 400


def test_patch_site_persists_coordinates(wired):
    seen = {}

    def fake_update(conn, site_id, company_id, name=None, location=None,
                    client=None, industry=None, address=None,
                    latitude=None, longitude=None):
        seen.update(latitude=latitude, longitude=longitude)
        return {"id": site_id, "latitude": latitude, "longitude": longitude}

    wired.setattr(org.sites, "update_site", fake_update)
    res = org.lambda_handler(make_event("PATCH", "/api/org/sites/s-1", body={
        "latitude": -41.2865, "longitude": 174.7762}), None)
    assert res["statusCode"] == 200
    assert seen == {"latitude": -41.2865, "longitude": 174.7762}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_lambda_org_api.py -k "coordinates or non_numeric or out_of_range" -v`
Expected: FAIL — `test_create_site_persists_coordinates` fails because `create_org_site` calls `create_site` without `latitude`/`longitude` (so `created` stays `{... 'address': ...}` without the coord keys / the fake's defaults are `None`); the two rejection tests fail because the handler returns 201 instead of 400.

- [ ] **Step 3: Add the coordinate coercion helper**

In `src/lambda_org_api.py`, immediately above `def create_org_site(conn, caller, body):` (line 523), insert:

```python
def _coerce_coord(value, lo, hi, label):
    """Validate an optional coordinate from a request body. Returns
    (coord_or_None, error_response_or_None). org-api is in-VPC — this only
    validates; it never geocodes (BUG-36)."""
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, error(f"{label} must be a number", 400)
    if not (lo <= value <= hi):
        return None, error(f"{label} must be between {lo} and {hi}", 400)
    return float(value), None
```

- [ ] **Step 4: Thread coords through `create_org_site`**

In `src/lambda_org_api.py`, in `create_org_site`, immediately before the `row = sites.create_site(` call (currently line 550), insert:

```python
    lat, lat_err = _coerce_coord(body.get("latitude"), -90.0, 90.0, "latitude")
    if lat_err:
        return lat_err
    lng, lng_err = _coerce_coord(body.get("longitude"), -180.0, 180.0, "longitude")
    if lng_err:
        return lng_err
```

Then replace the `create_site(...)` call block:

```python
    row = sites.create_site(
        conn, target_company_id, name,
        location=body.get("location"), client=body.get("client"),
        industry=body.get("industry"), icon_s3_key=None,
        address=body.get("address"),
    )
```

with:

```python
    row = sites.create_site(
        conn, target_company_id, name,
        location=body.get("location"), client=body.get("client"),
        industry=body.get("industry"), icon_s3_key=None,
        address=body.get("address"), latitude=lat, longitude=lng,
    )
```

- [ ] **Step 5: Thread coords through `patch_org_site`**

In `src/lambda_org_api.py`, in `patch_org_site`, immediately before the `row = sites.update_site(` call (currently line 580), insert:

```python
    lat, lat_err = _coerce_coord(body.get("latitude"), -90.0, 90.0, "latitude")
    if lat_err:
        return lat_err
    lng, lng_err = _coerce_coord(body.get("longitude"), -180.0, 180.0, "longitude")
    if lng_err:
        return lng_err
```

Then replace the `update_site(...)` call block:

```python
    row = sites.update_site(
        conn, site_id, caller["company_id"],
        name=name, location=body.get("location"),
        client=body.get("client"), industry=body.get("industry"),
        address=body.get("address"),
    )
```

with:

```python
    row = sites.update_site(
        conn, site_id, caller["company_id"],
        name=name, location=body.get("location"),
        client=body.get("client"), industry=body.get("industry"),
        address=body.get("address"), latitude=lat, longitude=lng,
    )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_lambda_org_api.py -k "coordinates or non_numeric or out_of_range or create_site or patch_site" -v`
Expected: PASS (new coord tests green; existing `test_create_site_admin_ok`, `test_patch_site_updates_fields`, `test_create_site_requires_name`, etc. still pass — the fakes in those tests use `**kwargs` or explicit signatures that already tolerate the new kwargs).

- [ ] **Step 7: Commit**

```bash
git add src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "feat(org-api): accept + validate latitude/longitude on site create/patch (persist-only, BUG-36)"
```

---

## Task 3: Photon geocode helper + non-VPC coordinate backfill

**Files:**
- Create: `src/geocode.py`, `src/backfill_site_coords.py`
- Test: `tests/unit/test_geocode.py`, `tests/unit/test_backfill_site_coords.py`

**Interfaces:**
- Produces: `geocode.parse_photon_features(geojson: dict) -> list[dict]` — each `{formatted, lat, lng, raw}`; Photon `geometry.coordinates` is `[lng, lat]`.
- Produces: `geocode.geocode(query: str, http=None, limit=5) -> dict | None` — best feature `{formatted, lat, lng}` or `None`. Uses `urllib3.PoolManager` when `http` is None (non-VPC only).
- Produces: `backfill_site_coords.plan_coordinate_backfill(sites: list[dict], geocode_fn=geocode.geocode) -> list[dict]` — for each site with a non-empty `address` and null `latitude`/`longitude` and a successful geocode, emits `{"site_id", "address", "latitude", "longitude", "formatted"}`.
- Consumes: `PATCH /api/org/sites/{id}` (Task 2) as the write-back path for the thin runner.
- **Constraint reminder:** this module is the **non-VPC** geocoding path. It must be deployed as a Lambda/CLI **without** `VpcConfig` (mirrors `ExtractSessionFunction`). It never touches Aurora directly.

- [ ] **Step 1: Write the failing geocode tests**

Create `tests/unit/test_geocode.py`:

```python
import json

import pytest

geocode = pytest.importorskip("geocode", reason="requires urllib3 (installed in CI)")


class FakeResp:
    def __init__(self, status, payload):
        self.status = status
        self.data = json.dumps(payload).encode("utf-8")


class FakeHTTP:
    def __init__(self, resp):
        self._resp = resp
        self.last_url = None

    def request(self, method, url, **kw):
        self.last_url = url
        return self._resp


_PHOTON = {
    "type": "FeatureCollection",
    "features": [
        {
            "geometry": {"type": "Point", "coordinates": [172.6362, -43.5321]},
            "properties": {"name": "13 Colombo Street", "housenumber": "13",
                           "street": "Colombo Street", "city": "Christchurch",
                           "postcode": "8011", "countrycode": "NZ"},
        }
    ],
}


def test_parse_photon_features_extracts_latlng_and_formatted():
    feats = geocode.parse_photon_features(_PHOTON)
    assert len(feats) == 1
    assert feats[0]["lat"] == -43.5321
    assert feats[0]["lng"] == 172.6362
    assert "Colombo" in feats[0]["formatted"]


def test_parse_photon_empty_features_returns_empty():
    assert geocode.parse_photon_features({"features": []}) == []
    assert geocode.parse_photon_features({}) == []


def test_geocode_returns_best_feature():
    http = FakeHTTP(FakeResp(200, _PHOTON))
    res = geocode.geocode("13 Colombo Street Christchurch", http=http)
    assert res["lat"] == -43.5321 and res["lng"] == 172.6362
    assert "photon.komoot.io/api" in http.last_url
    assert "q=13" in http.last_url  # query url-encoded


def test_geocode_no_results_returns_none():
    http = FakeHTTP(FakeResp(200, {"features": []}))
    assert geocode.geocode("nowhere at all", http=http) is None


def test_geocode_http_error_returns_none():
    http = FakeHTTP(FakeResp(503, {}))
    assert geocode.geocode("anything", http=http) is None
```

- [ ] **Step 2: Run geocode tests to verify they fail**

Run: `uv run pytest tests/unit/test_geocode.py -v`
Expected: FAIL at collection — `ModuleNotFoundError: No module named 'geocode'` (importorskip resolves once the module exists; until then the whole file errors/ is skipped). Treat "module not found" as the red state.

- [ ] **Step 3: Write `src/geocode.py`**

Create `src/geocode.py`:

```python
"""Photon (OSM/Komoot) geocoder — free, keyless, autocomplete-capable.

NON-VPC ONLY. Photon is a public HTTP endpoint; calling it from an in-VPC
Lambda with no egress black-holes until timeout (BUG-36). This module is used
by the browser (see fieldsight-ui) and by the non-VPC backfill helper.

Photon returns GeoJSON FeatureCollection; geometry.coordinates is [lng, lat].
"""
import json
from urllib.parse import quote

PHOTON_URL = "https://photon.komoot.io/api"


def parse_photon_features(geojson) -> list:
    features = (geojson or {}).get("features") or []
    out = []
    for f in features:
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        lng, lat = coords[0], coords[1]
        out.append({
            "lat": lat,
            "lng": lng,
            "formatted": _format(f.get("properties") or {}),
            "raw": f,
        })
    return out


def _format(props) -> str:
    parts = []
    hn, street = props.get("housenumber"), props.get("street")
    if hn and street:
        parts.append(f"{hn} {street}")
    elif street:
        parts.append(street)
    elif props.get("name"):
        parts.append(props["name"])
    for key in ("city", "postcode", "state", "country"):
        if props.get(key):
            parts.append(props[key])
    return ", ".join(parts)


def geocode(query, http=None, limit=5):
    """Return the best {formatted, lat, lng} for `query`, or None."""
    if not query or not str(query).strip():
        return None
    if http is None:
        import urllib3
        http = urllib3.PoolManager()
    url = f"{PHOTON_URL}?q={quote(str(query))}&limit={limit}&lang=en"
    try:
        resp = http.request("GET", url, timeout=10.0)
        if resp.status != 200:
            return None
        feats = parse_photon_features(json.loads(resp.data.decode("utf-8")))
    except Exception:
        return None
    if not feats:
        return None
    top = feats[0]
    return {"formatted": top["formatted"], "lat": top["lat"], "lng": top["lng"]}
```

- [ ] **Step 4: Run geocode tests to verify they pass**

Run: `uv run pytest tests/unit/test_geocode.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Write the failing backfill tests**

Create `tests/unit/test_backfill_site_coords.py`:

```python
import pytest

bf = pytest.importorskip("backfill_site_coords",
                         reason="requires urllib3 (installed in CI)")


def _fake_geocode(query, http=None, limit=5):
    if "Colombo" in query:
        return {"formatted": "13 Colombo Street, Christchurch",
                "lat": -43.5321, "lng": 172.6362}
    return None


def test_plan_skips_sites_with_existing_coords():
    sites = [{"id": "s1", "address": "13 Colombo Street",
              "latitude": -43.5, "longitude": 172.6}]
    assert bf.plan_coordinate_backfill(sites, geocode_fn=_fake_geocode) == []


def test_plan_skips_sites_without_address():
    sites = [{"id": "s2", "address": None, "latitude": None, "longitude": None},
             {"id": "s3", "address": "", "latitude": None, "longitude": None}]
    assert bf.plan_coordinate_backfill(sites, geocode_fn=_fake_geocode) == []


def test_plan_geocodes_address_and_emits_update():
    sites = [{"id": "s4", "address": "13 Colombo Street",
              "latitude": None, "longitude": None}]
    plan = bf.plan_coordinate_backfill(sites, geocode_fn=_fake_geocode)
    assert plan == [{"site_id": "s4", "address": "13 Colombo Street",
                     "latitude": -43.5321, "longitude": 172.6362,
                     "formatted": "13 Colombo Street, Christchurch"}]


def test_plan_skips_geocode_miss():
    sites = [{"id": "s5", "address": "an address OSM has never heard of",
              "latitude": None, "longitude": None}]
    assert bf.plan_coordinate_backfill(sites, geocode_fn=_fake_geocode) == []
```

- [ ] **Step 6: Run backfill tests to verify they fail**

Run: `uv run pytest tests/unit/test_backfill_site_coords.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backfill_site_coords'`.

- [ ] **Step 7: Write `src/backfill_site_coords.py`**

Create `src/backfill_site_coords.py`:

```python
"""Non-VPC backfill: geocode sites that have an address but no coordinates.

Deploy WITHOUT VpcConfig (mirrors ExtractSessionFunction). The pure planner
below is fully unit-tested; the runner geocodes coord-less sites and writes the
coordinates back through the org-api PATCH endpoint (Task 2) — the in-VPC
org-api persists them (BUG-36: geocoding stays out here, persistence stays
in-VPC). Runs on demand / low frequency; never per request.
"""
import geocode as _geocode


def plan_coordinate_backfill(sites, geocode_fn=_geocode.geocode):
    """Pure. For each site with a non-empty address and null lat/lng that
    geocodes successfully, emit an update dict. Skips everything else."""
    updates = []
    for s in sites or []:
        if s.get("latitude") is not None or s.get("longitude") is not None:
            continue
        address = (s.get("address") or "").strip()
        if not address:
            continue
        hit = geocode_fn(address)
        if not hit:
            continue
        updates.append({
            "site_id": s.get("id") or s.get("site_id"),
            "address": address,
            "latitude": hit["lat"],
            "longitude": hit["lng"],
            "formatted": hit.get("formatted", ""),
        })
    return updates


def run_backfill(fetch_sites_fn, persist_fn, geocode_fn=_geocode.geocode):
    """Thin orchestration (I/O edges injected — verified by manual invoke, not
    unit tests). `fetch_sites_fn() -> list[site dict]`; `persist_fn(update)`
    PATCHes /api/org/sites/{site_id} with {latitude, longitude} (admin token)
    AND/OR merges the coord into config/user_mapping.json's `sites` block so the
    non-VPC ReportGeneratorFunction can read it (D-COORD option a)."""
    plan = plan_coordinate_backfill(fetch_sites_fn(), geocode_fn=geocode_fn)
    results = []
    for update in plan:
        results.append(persist_fn(update))
    return results
```

- [ ] **Step 8: Run backfill tests to verify they pass**

Run: `uv run pytest tests/unit/test_backfill_site_coords.py tests/unit/test_geocode.py -v`
Expected: PASS (all green).

- [ ] **Step 9: Commit**

```bash
git add src/geocode.py src/backfill_site_coords.py tests/unit/test_geocode.py tests/unit/test_backfill_site_coords.py
git commit -m "feat(geocode): Photon geocode helper + non-VPC coordinate backfill planner"
```

---

## Task 4: Open-Meteo weather module (normalize + prompt block + fetch)

**Files:**
- Create: `src/weather.py`
- Test: `tests/unit/test_weather.py`

**Interfaces:**
- Produces: `weather.WMO_LABELS: dict[int, str]` (WMO 4677 → label).
- Produces: `weather.normalize_weather(data: dict, date: str) -> dict | None` — reads Open-Meteo `daily` arrays (index 0), returns the block `{date, temp_max_c, temp_min_c, weathercode, condition_label, windspeed_kmh, precip_mm, source: "open-meteo"}` or `None` if no daily row.
- Produces: `weather.weather_prompt_block(weather: dict) -> str` — the factual sentence + correlation guardrail injected into the Claude prompt.
- Produces: `weather.fetch_weather(lat, lng, date, today_iso, http=None) -> dict | None` — archive API when `date < today_iso`, forecast API otherwise; returns a normalized block. Uses `urllib3.PoolManager` when `http` is None (non-VPC only).
- **Constraint reminder:** keyless Open-Meteo; one call per (site, date). Non-VPC only.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_weather.py`:

```python
import json

import pytest

weather = pytest.importorskip("weather", reason="requires urllib3 (installed in CI)")


class FakeResp:
    def __init__(self, status, payload):
        self.status = status
        self.data = json.dumps(payload).encode("utf-8")


class FakeHTTP:
    def __init__(self, resp):
        self._resp = resp
        self.last_url = None

    def request(self, method, url, **kw):
        self.last_url = url
        return self._resp


_DAILY = {
    "daily": {
        "time": ["2026-07-18"],
        "temperature_2m_max": [12.4],
        "temperature_2m_min": [3.1],
        "weathercode": [61],
        "windspeed_10m_max": [28.0],
        "precipitation_sum": [5.2],
    }
}


def test_normalize_weather_from_daily_block():
    block = weather.normalize_weather(_DAILY, "2026-07-18")
    assert block == {
        "date": "2026-07-18",
        "temp_max_c": 12.4,
        "temp_min_c": 3.1,
        "weathercode": 61,
        "condition_label": "Slight rain",
        "windspeed_kmh": 28.0,
        "precip_mm": 5.2,
        "source": "open-meteo",
    }


def test_normalize_weather_no_daily_returns_none():
    assert weather.normalize_weather({}, "2026-07-18") is None
    assert weather.normalize_weather({"daily": {"time": []}}, "2026-07-18") is None


def test_weather_prompt_block_states_conditions_and_guardrail():
    block = weather.normalize_weather(_DAILY, "2026-07-18")
    text = weather.weather_prompt_block(block)
    assert "Slight rain" in text
    assert "2026-07-18" in text
    # correlation guardrail must be present (grounded, not fabricated)
    assert "do not invent" in text.lower()


def test_fetch_weather_uses_archive_for_historical():
    http = FakeHTTP(FakeResp(200, _DAILY))
    block = weather.fetch_weather(-43.5321, 172.6362, "2026-07-18",
                                  "2026-07-19", http=http)
    assert block["temp_max_c"] == 12.4
    assert "archive-api.open-meteo.com/v1/archive" in http.last_url
    assert "start_date=2026-07-18" in http.last_url


def test_fetch_weather_uses_forecast_for_today():
    today = {
        "current_weather": {"temperature": 9.0, "weathercode": 3, "windspeed": 11.0},
        "daily": {
            "time": ["2026-07-19"],
            "temperature_2m_max": [10.0],
            "temperature_2m_min": [4.0],
            "weathercode": [3],
            "windspeed_10m_max": [15.0],
            "precipitation_sum": [0.0],
        },
    }
    http = FakeHTTP(FakeResp(200, today))
    block = weather.fetch_weather(-43.5321, 172.6362, "2026-07-19",
                                  "2026-07-19", http=http)
    assert block["weathercode"] == 3
    assert "api.open-meteo.com/v1/forecast" in http.last_url
    assert "current_weather=true" in http.last_url


def test_fetch_weather_http_error_returns_none():
    http = FakeHTTP(FakeResp(500, {}))
    assert weather.fetch_weather(-43.5, 172.6, "2026-07-18", "2026-07-19",
                                 http=http) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_weather.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'weather'`.

- [ ] **Step 3: Write `src/weather.py`**

Create `src/weather.py`:

```python
"""Open-Meteo weather (free, keyless) for the AI daily report.

NON-VPC ONLY (BUG-36). Same provider the UI weather indicator already uses:
archive API for past dates, forecast API for today/future. Normalizes to a
single block cached once per (site, date) on the report record. WMO 4677 codes
map to labels (mirrors the UI's WMO_WEATHER_CODES).
"""
import json

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
_DAILY_VARS = "temperature_2m_max,temperature_2m_min,weathercode,windspeed_10m_max,precipitation_sum"

WMO_LABELS = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    56: "Light freezing drizzle", 57: "Dense freezing drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm, slight hail", 99: "Thunderstorm, heavy hail",
}


def _first(daily, key):
    seq = daily.get(key)
    return seq[0] if isinstance(seq, list) and seq else None


def normalize_weather(data, date):
    daily = (data or {}).get("daily") or {}
    times = daily.get("time") or []
    if not times:
        return None
    code = _first(daily, "weathercode")
    code = int(code) if code is not None else None
    return {
        "date": date,
        "temp_max_c": _first(daily, "temperature_2m_max"),
        "temp_min_c": _first(daily, "temperature_2m_min"),
        "weathercode": code,
        "condition_label": WMO_LABELS.get(code, "Unknown"),
        "windspeed_kmh": _first(daily, "windspeed_10m_max"),
        "precip_mm": _first(daily, "precipitation_sum"),
        "source": "open-meteo",
    }


def weather_prompt_block(weather):
    if not weather:
        return ""
    return (
        f"Site weather for {weather['date']} was: {weather['condition_label']}, "
        f"{weather['temp_min_c']}–{weather['temp_max_c']}°C, wind up to "
        f"{weather['windspeed_kmh']} km/h, precipitation {weather['precip_mm']} mm. "
        "Where an observation plausibly relates to weather (rain → "
        "concrete/paint/earthworks delays; high wind → crane/height work; "
        "heat/cold → pours/curing), note the linkage explicitly; do not "
        "invent impacts the transcript doesn't support."
    )


def fetch_weather(lat, lng, date, today_iso, http=None):
    """One Open-Meteo call for (lat, lng, date). archive if date < today_iso,
    else forecast. Returns a normalized block or None on any failure."""
    if lat is None or lng is None:
        return None
    if http is None:
        import urllib3
        http = urllib3.PoolManager()
    historical = bool(today_iso and date < today_iso)
    if historical:
        url = (f"{ARCHIVE_URL}?latitude={lat}&longitude={lng}"
               f"&start_date={date}&end_date={date}"
               f"&daily={_DAILY_VARS}&timezone=Pacific/Auckland")
    else:
        url = (f"{FORECAST_URL}?latitude={lat}&longitude={lng}"
               f"&start_date={date}&end_date={date}"
               f"&daily={_DAILY_VARS}&current_weather=true&timezone=Pacific/Auckland")
    try:
        resp = http.request("GET", url, timeout=10.0)
        if resp.status != 200:
            return None
        return normalize_weather(json.loads(resp.data.decode("utf-8")), date)
    except Exception:
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_weather.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add src/weather.py tests/unit/test_weather.py
git commit -m "feat(weather): keyless Open-Meteo normalize + prompt block + archive/forecast fetch"
```

---

## Task 5: Report generator — fetch weather + factual block + prompt injection

**Files:**
- Modify: `src/lambda_report_generator.py:60-73` (imports), add `build_weather_block_for_site` helper, `:1272-1285` (daily flow), `:1345-1378` (report dict)
- Test: `tests/unit/test_lambda_report_generator.py`

**Interfaces:**
- Consumes: `weather.fetch_weather(lat, lng, date, today_iso, http=None)` and `weather.weather_prompt_block(weather)` from Task 4; `get_nzdt_now()` (`:233`); `sites_info` / `user_site_info` from `get_user_site_mapping` (`:332-355`), which reads `latitude`/`longitude` from `config/user_mapping.json`'s `sites` block (D-COORD option a).
- Produces: `build_weather_block_for_site(site_info: dict, target_date: str, today_iso: str, fetch=weather.fetch_weather) -> dict | None` — returns the normalized weather block for the site's coord, or `None` when the coord is missing or the fetch fails (graceful). `report['weather']` carries the block; the prompt gains a `## Site Weather (for AI correlation)` section.
- **Constraint reminder:** `ReportGeneratorFunction` is the non-VPC home (confirmed no `VpcConfig`). Weather HTTP is legal here.

- [ ] **Step 1: Write the failing unit tests**

Create `tests/unit/test_lambda_report_generator.py`:

```python
"""Unit tests for the weather seam in lambda_report_generator.

Dummy AWS/Anthropic env vars so the module's eager boto3 client + config
reads don't blow up at import (mirrors tests/unit/test_lambda_extract_session.py).
No test here makes a real AWS, Claude, or Open-Meteo call.
"""
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")

rg = pytest.importorskip("lambda_report_generator",
                         reason="requires boto3/urllib3 (installed in CI)")


def test_build_weather_block_returns_none_without_coords():
    called = []

    def fake_fetch(lat, lng, date, today_iso, http=None):
        called.append((lat, lng))
        return {"date": date, "source": "open-meteo"}

    # site_info with no latitude/longitude -> no fetch, None returned
    assert rg.build_weather_block_for_site({"name": "Depot"}, "2026-07-18",
                                           "2026-07-19", fetch=fake_fetch) is None
    assert called == []


def test_build_weather_block_calls_fetch_with_site_coords():
    seen = {}

    def fake_fetch(lat, lng, date, today_iso, http=None):
        seen.update(lat=lat, lng=lng, date=date, today=today_iso)
        return {"date": date, "condition_label": "Slight rain",
                "source": "open-meteo"}

    site_info = {"name": "Depot", "latitude": -43.5321, "longitude": 172.6362}
    block = rg.build_weather_block_for_site(site_info, "2026-07-18",
                                            "2026-07-19", fetch=fake_fetch)
    assert seen == {"lat": -43.5321, "lng": 172.6362,
                    "date": "2026-07-18", "today": "2026-07-19"}
    assert block["condition_label"] == "Slight rain"


def test_build_weather_block_swallows_fetch_error():
    def boom(*a, **k):
        raise RuntimeError("open-meteo down")

    site_info = {"latitude": -43.5, "longitude": 172.6}
    assert rg.build_weather_block_for_site(site_info, "2026-07-18",
                                           "2026-07-19", fetch=boom) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_lambda_report_generator.py -v`
Expected: FAIL — `AttributeError: module 'lambda_report_generator' has no attribute 'build_weather_block_for_site'`.

- [ ] **Step 3: Add the `weather` import (single-line Edit anchor)**

In `src/lambda_report_generator.py` replace the exact line:

```python
import urllib3
```

with:

```python
import urllib3
import weather
```

- [ ] **Step 4: Add the `build_weather_block_for_site` helper**

In `src/lambda_report_generator.py`, immediately above `def build_daily_prompt(` (line 504), insert:

```python
def build_weather_block_for_site(site_info, target_date, today_iso,
                                 fetch=weather.fetch_weather):
    """Fetch the normalized (site, date) weather block, or None when the site
    has no coordinate (un-backfilled) or the fetch fails. Non-VPC: this runs in
    ReportGeneratorFunction, which has egress. Coordinate comes from the
    config/user_mapping.json `sites` block (D-COORD option a)."""
    lat = site_info.get("latitude")
    lng = site_info.get("longitude")
    if lat is None or lng is None:
        return None
    try:
        return fetch(lat, lng, target_date, today_iso)
    except Exception as e:
        logger.warning(f"weather fetch failed for {target_date}: {e}")
        return None
```

- [ ] **Step 5: Fetch weather in the daily flow + inject into the prompt**

In `src/lambda_report_generator.py`, replace the block (lines 1276-1280):

```python
        prompt = build_daily_prompt(
            correlated, user_name, user_site_name, target_date,
            role=user_role, total_duration=user_data['total_duration'],
            num_photos=len(user_data['photos']), name_mapping=user_mapping,
        )
```

with:

```python
        weather_block = build_weather_block_for_site(
            user_site_info, target_date, get_nzdt_now().strftime('%Y-%m-%d'))

        prompt = build_daily_prompt(
            correlated, user_name, user_site_name, target_date,
            role=user_role, total_duration=user_data['total_duration'],
            num_photos=len(user_data['photos']), name_mapping=user_mapping,
        )
        if weather_block:
            prompt += ("\n\n## Site Weather (for AI correlation)\n"
                       + weather.weather_prompt_block(weather_block))
```

- [ ] **Step 6: Attach the factual weather block to the report record**

In `src/lambda_report_generator.py`, replace the exact line (currently 1361):

```python
            'executive_summary': claude_output.get('executive_summary', ''),
```

with:

```python
            'weather': weather_block,
            'executive_summary': claude_output.get('executive_summary', ''),
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_lambda_report_generator.py -v`
Expected: PASS (3 passed).
Also confirm the module still imports cleanly across the suite: `uv run pytest tests/unit -q -k "report_generator or weather or geocode or backfill or org_api"`.

- [ ] **Step 8: Commit**

```bash
git add src/lambda_report_generator.py tests/unit/test_lambda_report_generator.py
git commit -m "feat(report): fetch per-(site,date) weather, attach factual block, inject AI-correlation prompt"
```

---

## Task 6: UI weather indicator reads the active site's real coordinate

*(fieldsight-ui repo — no test runner; verification = `node --check` + grep + manual checklist.)*

**Files:**
- Modify: `fieldsight-ui/scripts/app-shell.js:177` (add coord cache), `:254-259` (coord resolution)
- Modify: `fieldsight-ui/app-shell-preview.html` (bump `?v=N` for `app-shell.js`)

**Interfaces:**
- Consumes: `window.FS.api.org.getOrgSites()` → `{ sites: [{ site_id, latitude, longitude, ... }] }` (org API now returns coords via Task 2; `_toPageSite` passes them through).
- Consumes: `window.FS.siteContext.get()` / `.onChange()` (existing).
- Produces: `WeatherIndicator` uses `siteCoord || (fixtureSite && fixtureSite.coord) || WEATHER_DEFAULT_COORD`. Existing Open-Meteo call, `weatherFetchCache`, `wmoLookup`, and `MockData.WEATHER` fallback unchanged.

- [ ] **Step 1: Pre-check the current (mock-only) coord source**

Run: `grep -n "activeSite && activeSite.coord" fieldsight-ui/scripts/app-shell.js`
Expected (red state): matches line 259 `const coord = (activeSite && activeSite.coord) || WEATHER_DEFAULT_COORD;` — proving the indicator reads the mock fixture coord, not the real site coord.

- [ ] **Step 2: Add a module-level org-site coord cache**

In `fieldsight-ui/scripts/app-shell.js`, immediately after the exact line (177):

```javascript
const WEATHER_DEFAULT_COORD = { lat: -43.5321, lng: 172.6362 };
```

insert:

```javascript
/* site_id -> { lat, lng } | null, filled once from the org API (real Aurora
   coordinates, now that the site record carries latitude/longitude). null = a
   resolved site that has no coordinate yet (un-backfilled) -> caller falls back
   to fixture coord / default. */
const orgSiteCoordCache = {};
```

- [ ] **Step 3: Resolve the real coordinate from the org API**

In `fieldsight-ui/scripts/app-shell.js`, replace the exact block (lines 254-259):

```javascript
  const sitesList = (window.FieldSight.fixtures && window.FieldSight.fixtures.sites
    && window.FieldSight.fixtures.sites.sites) || [];
  const activeSite = activeSiteId
    ? sitesList.find(function(s) { return s.site_id === activeSiteId; })
    : null;
  const coord = (activeSite && activeSite.coord) || WEATHER_DEFAULT_COORD;
```

with:

```javascript
  const sitesList = (window.FieldSight.fixtures && window.FieldSight.fixtures.sites
    && window.FieldSight.fixtures.sites.sites) || [];
  const fixtureSite = activeSiteId
    ? sitesList.find(function(s) { return s.site_id === activeSiteId; })
    : null;

  /* Real Aurora coordinate for the active site, fetched once from the org API
     (spec §3.6). Falls back to the fixture coord, then the NZ default, so the
     indicator never disappears (BUG-20-safe: getOrgSites uses the guarded
     org request; a non-JSON SPA-shell 200 resolves to _notFound, not a crash). */
  const [siteCoord, setSiteCoord] = React.useState(function() {
    return activeSiteId ? orgSiteCoordCache[activeSiteId] || null : null;
  });
  React.useEffect(function() {
    if (!activeSiteId) { setSiteCoord(null); return undefined; }
    if (orgSiteCoordCache[activeSiteId] !== undefined) {
      setSiteCoord(orgSiteCoordCache[activeSiteId]);
      return undefined;
    }
    if (!(window.FS && window.FS.api && window.FS.api.org
          && window.FS.api.org.getOrgSites)) { return undefined; }
    let cancelled = false;
    window.FS.api.org.getOrgSites().then(function(res) {
      const list = (res && res.sites) || [];
      list.forEach(function(s) {
        if (s && s.site_id && s.latitude != null && s.longitude != null) {
          orgSiteCoordCache[s.site_id] = { lat: s.latitude, lng: s.longitude };
        }
      });
      if (orgSiteCoordCache[activeSiteId] === undefined) {
        orgSiteCoordCache[activeSiteId] = null;  // resolved: this site has no coord
      }
      if (!cancelled) setSiteCoord(orgSiteCoordCache[activeSiteId]);
    }).catch(function() { if (!cancelled) setSiteCoord(null); });
    return function() { cancelled = true; };
  }, [activeSiteId]);

  const coord = siteCoord
    || (fixtureSite && fixtureSite.coord)
    || WEATHER_DEFAULT_COORD;
```

- [ ] **Step 4: Syntax-check + post-check**

Run: `node --check fieldsight-ui/scripts/app-shell.js`
Expected: no output, exit 0.
Run: `grep -n "orgSiteCoordCache\|const coord = siteCoord" fieldsight-ui/scripts/app-shell.js`
Expected (green state): both the cache declaration and `const coord = siteCoord` appear; the old `activeSite && activeSite.coord` line is gone.

- [ ] **Step 5: Bump the cache-buster**

Run: `grep -n "app-shell.js?v=" fieldsight-ui/app-shell-preview.html`
Then increment the `?v=N` on the `app-shell.js` `<script>` tag by one (e.g. `app-shell.js?v=42` → `app-shell.js?v=43`) via a single-line Edit on that exact tag. (Only that script changed; do not bump others.)

- [ ] **Step 6: Manual verification checklist** (state done vs deferred; real-browser check may be deferred to the user per `fieldsight-ui/CLAUDE.md`)

- [ ] With `?dev=1` LIVE org API + a site that has coordinates: the weather indicator temp/condition matches that site's location, and switching the header project selector re-fetches (indicator updates).
- [ ] A site with null coordinates falls back to the default Christchurch weather (no crash, indicator visible).
- [ ] MOCK mode (`?mocks=1`/no live org): fixture coord still drives weather (mock fallback unbroken).

- [ ] **Step 7: Commit**

```bash
cd fieldsight-ui
git add scripts/app-shell.js app-shell-preview.html
git commit -m "feat(weather): indicator reads active site's real Aurora coordinate via org API"
```

---

## Task 7: UI create/edit-site address Photon autocomplete

*(fieldsight-ui repo — no test runner; verification = `node --check` + grep + manual checklist.)*

**Files:**
- Modify: `fieldsight-ui/scripts/api/org.js` (add `geocodeAddress`; forward `latitude`/`longitude`)
- Modify: `fieldsight-ui/scripts/pages/sites.js:233-306` (`NewProjectModal`), `:308-345` (`EditProjectModal`) — add an `AddressAutocomplete` field
- Modify: `fieldsight-ui/app-shell-preview.html` (bump `?v=N` for `sites.js` and `org.js`)

**Interfaces:**
- Consumes: Photon (`https://photon.komoot.io/api?q=<query>&limit=5&lang=en`) directly from the browser (keyless; not routed through the in-VPC org API).
- Produces: `window.FS.api.org.geocodeAddress(query) -> Promise<[{ formatted, lat, lng }]>` (empty array on error/no result).
- Produces: `NewProjectModal`/`EditProjectModal` collect `address` + stash `latitude`/`longitude` on pick, and submit them via `createOrgSite`/`updateOrgSite` (which forward the two fields to the org API).

- [ ] **Step 1: Pre-check — no geocode helper, and NewProjectModal has no address field**

Run: `grep -n "geocodeAddress\|latitude" fieldsight-ui/scripts/api/org.js; grep -n "address" fieldsight-ui/scripts/pages/sites.js`
Expected (red state): no `geocodeAddress`/`latitude` in `org.js`; `sites.js` shows `address` only in `EditProjectModal` (create modal has none), and neither modal stashes coords.

- [ ] **Step 2: Add the `geocodeAddress` helper to `org.js`**

In `fieldsight-ui/scripts/api/org.js`, immediately above the exact line `async function createOrgSite(body) {` (line 97), insert:

```javascript
  /* Photon geocode/autocomplete — free, keyless, called DIRECTLY from the
     browser (NOT through the in-VPC org API, which cannot make outbound calls,
     BUG-36). Returns up to 5 {formatted, lat, lng}; [] on any error/no result
     so the caller degrades to a plain free-text address (coords left null ->
     backfill later). geometry.coordinates is [lng, lat]. */
  async function geocodeAddress(query) {
    if (!query || !query.trim()) return [];
    var url = 'https://photon.komoot.io/api?q=' + encodeURIComponent(query)
      + '&limit=5&lang=en';
    try {
      var resp = await fetch(url);
      if (!resp.ok) return [];
      var data = await resp.json();
      return ((data && data.features) || []).map(function (f) {
        var c = (f.geometry && f.geometry.coordinates) || [];
        var p = f.properties || {};
        var line = [];
        if (p.housenumber && p.street) line.push(p.housenumber + ' ' + p.street);
        else if (p.street) line.push(p.street);
        else if (p.name) line.push(p.name);
        ['city', 'postcode', 'state', 'country'].forEach(function (k) {
          if (p[k]) line.push(p[k]);
        });
        return { formatted: line.join(', '), lat: c[1], lng: c[0] };
      }).filter(function (x) { return x.lat != null && x.lng != null; });
    } catch (e) {
      return [];
    }
  }
```

- [ ] **Step 3: Export `geocodeAddress`**

In `fieldsight-ui/scripts/api/org.js`, replace the exact line (334):

```javascript
    getOrgSites: getOrgSites, createOrgSite: createOrgSite, updateOrgSite: updateOrgSite,
```

with:

```javascript
    getOrgSites: getOrgSites, createOrgSite: createOrgSite, updateOrgSite: updateOrgSite, geocodeAddress: geocodeAddress,
```

- [ ] **Step 4: Add a shared `AddressAutocomplete` component to `sites.js`**

In `fieldsight-ui/scripts/pages/sites.js`, immediately below the `fSelect` helper (ends line 230), insert:

```javascript
  /* Debounced Photon type-ahead. On pick: calls onPick({ address, latitude,
     longitude }) so the parent form fills the address text AND stashes coords.
     On error/no-result it stays a plain free-text input (coords left null ->
     backfilled later). Keyless; browser-direct (not via org API). */
  function AddressAutocomplete(props) {
    var refOpen = React.useState(false); var isOpen = refOpen[0], setOpen = refOpen[1];
    var refList = React.useState([]); var list = refList[0], setList = refList[1];
    var timer = React.useRef(null);
    function onType(v) {
      props.onText(v);
      if (timer.current) clearTimeout(timer.current);
      if (!v || !v.trim() || !(window.FS.api.org && window.FS.api.org.geocodeAddress)) {
        setList([]); setOpen(false); return;
      }
      timer.current = setTimeout(function () {
        window.FS.api.org.geocodeAddress(v).then(function (results) {
          setList(results); setOpen(results.length > 0);
        });
      }, 350);
    }
    function pick(item) {
      setOpen(false); setList([]);
      props.onPick({ address: item.formatted, latitude: item.lat, longitude: item.lng });
    }
    return React.createElement('div', { style: { position: 'relative' } },
      React.createElement('input', {
        type: 'text', className: 'fs-settings__input', value: props.value || '',
        placeholder: 'Start typing an address…',
        onChange: function (e) { onType(e.target.value); },
      }),
      isOpen ? React.createElement('ul', {
        className: 'fs-address-suggest',
        style: { position: 'absolute', zIndex: 20, left: 0, right: 0, margin: 0,
                 padding: '4px 0', listStyle: 'none',
                 background: 'var(--surface-panel)', border: '1px solid var(--border-subtle)',
                 borderRadius: '6px', maxHeight: '180px', overflowY: 'auto' },
      }, list.map(function (item, i) {
        return React.createElement('li', {
          key: i, style: { padding: '6px 10px', cursor: 'pointer' },
          onMouseDown: function (e) { e.preventDefault(); pick(item); },
        }, item.formatted);
      })) : null);
  }
```

- [ ] **Step 5: Wire the address+coords into `NewProjectModal`**

In `fieldsight-ui/scripts/pages/sites.js`, replace the `NewProjectModal` form initial state (exact line 240):

```javascript
    var refForm = React.useState({ name: '', location: '', region: 'south-island', client: '', project_value_nzd: '', planned_completion: '' });
```

with:

```javascript
    var refForm = React.useState({ name: '', location: '', region: 'south-island', client: '', project_value_nzd: '', planned_completion: '', address: '', latitude: null, longitude: null });
```

Replace the create-body block (exact lines 263-265):

```javascript
      var creating = live
        ? window.FS.api.org.createOrgSite({ name: form.name, location: form.location, client: form.client, icon_s3_key: form._iconKey || undefined })
        : window.FS.api.sites.createSite(form);
```

with:

```javascript
      var creating = live
        ? window.FS.api.org.createOrgSite({ name: form.name, location: form.location, client: form.client, address: form.address || undefined, latitude: form.latitude, longitude: form.longitude, icon_s3_key: form._iconKey || undefined })
        : window.FS.api.sites.createSite(form);
```

Add the address field to the create form — in `NewProjectModal`, immediately after the `fFieldRow('Location', ...)` row (exact line 295):

```javascript
        fFieldRow('Location', fText(form.location, function (v) { set('location', v); })),
```

insert:

```javascript
        fFieldRow('Address', React.createElement(AddressAutocomplete, {
          value: form.address,
          onText: function (v) { setForm(function (f) { return Object.assign({}, f, { address: v, latitude: null, longitude: null }); }); },
          onPick: function (p) { setForm(function (f) { return Object.assign({}, f, { address: p.address, latitude: p.latitude, longitude: p.longitude }); }); },
        })),
```

- [ ] **Step 6: Wire the address+coords into `EditProjectModal`**

In `fieldsight-ui/scripts/pages/sites.js`, replace the `EditProjectModal` form initial state (exact lines 312-317):

```javascript
    var refForm = React.useState({
      name:     site.name || '',
      location: site.location || '',
      client:   site.client || '',
      address:  site.address || '',
    });
```

with:

```javascript
    var refForm = React.useState({
      name:      site.name || '',
      location:  site.location || '',
      client:    site.client || '',
      address:   site.address || '',
      latitude:  site.latitude != null ? site.latitude : null,
      longitude: site.longitude != null ? site.longitude : null,
    });
```

Replace the update-body block (exact lines 324-326):

```javascript
      window.FS.api.org.updateOrgSite(props.site.site_id, {
        name: form.name, location: form.location, client: form.client, address: form.address,
      }).then(function (updated) {
```

with:

```javascript
      window.FS.api.org.updateOrgSite(props.site.site_id, {
        name: form.name, location: form.location, client: form.client, address: form.address,
        latitude: form.latitude, longitude: form.longitude,
      }).then(function (updated) {
```

Then, in `EditProjectModal`'s rendered form, replace its plain address row. First locate it:

Run: `grep -n "fFieldRow('Address'" fieldsight-ui/scripts/pages/sites.js`

The EditProjectModal currently renders `fFieldRow('Address', fText(form.address, function (v) { set('address', v); }))`. Replace that exact row with:

```javascript
        fFieldRow('Address', React.createElement(AddressAutocomplete, {
          value: form.address,
          onText: function (v) { setForm(function (f) { return Object.assign({}, f, { address: v, latitude: null, longitude: null }); }); },
          onPick: function (p) { setForm(function (f) { return Object.assign({}, f, { address: p.address, latitude: p.latitude, longitude: p.longitude }); }); },
        })),
```

*(If `EditProjectModal` has no address row today, insert this row after its `Client` field instead. Confirm via the grep above before editing.)*

- [ ] **Step 7: Syntax-check + post-check**

Run: `node --check fieldsight-ui/scripts/api/org.js && node --check fieldsight-ui/scripts/pages/sites.js`
Expected: no output, exit 0 for both.
Run: `grep -n "geocodeAddress\|AddressAutocomplete\|latitude" fieldsight-ui/scripts/api/org.js fieldsight-ui/scripts/pages/sites.js`
Expected (green state): `geocodeAddress` defined + exported in `org.js`; `AddressAutocomplete` defined once and used in both modals; both submit bodies include `latitude`/`longitude`.

- [ ] **Step 8: Bump cache-busters**

Run: `grep -n "sites.js?v=\|org.js?v=" fieldsight-ui/app-shell-preview.html`
Increment the `?v=N` on the `pages/sites.js` and `api/org.js` `<script>` tags by one each (single-line Edits on those exact tags).

- [ ] **Step 9: Manual verification checklist** (state done vs deferred)

- [ ] Create-project modal (admin, `?dev=1` LIVE): typing an address shows Photon suggestions after ~350 ms; picking one fills the field and submits `latitude`/`longitude` (verify via network tab: POST `/sites` body carries the two numbers).
- [ ] Edit-project modal: same behavior; saving persists coords; re-opening shows the address.
- [ ] Photon error / no result: field stays plain free-text, submit succeeds with `latitude/longitude: null` (no crash).
- [ ] After creating a coord-bearing site and selecting it in the header, the weather indicator (Task 6) shows that site's weather.

- [ ] **Step 10: Commit**

```bash
cd fieldsight-ui
git add scripts/api/org.js scripts/pages/sites.js app-shell-preview.html
git commit -m "feat(sites): Photon address autocomplete fills address + coordinates on create/edit"
```

---

## Task 8: PR / deploy handoff (user-gated)

**Files:** none (operational).

This task is a checklist, not code. Do NOT run any deploy/PR command without the user's explicit go-ahead (memory: commit/push only when asked; deploys are user-gated).

- [ ] **Step 1: Full pipeline test gate**

Run: `uv run pytest -q`
Expected: all unit tests pass; integration tests SKIP locally (no `TEST_DATABASE_URL`) or PASS in CI. Confirm the new tests are collected: `uv run pytest tests/unit/test_weather.py tests/unit/test_geocode.py tests/unit/test_backfill_site_coords.py tests/unit/test_lambda_report_generator.py tests/unit/test_lambda_org_api.py -q`.

- [ ] **Step 2: UI syntax gate**

Run: `node --check fieldsight-ui/scripts/app-shell.js && node --check fieldsight-ui/scripts/api/org.js && node --check fieldsight-ui/scripts/pages/sites.js`
Expected: exit 0 for all three.

- [ ] **Step 3: Confirm branch base (pipeline)**

The pipeline feature work must sit on a branch cut from `develop` (NOT the `docs/*` branch this plan lives on). Verify: `git merge-base --is-ancestor develop HEAD` (or rebase the feature branch onto `develop`). Never `git add -A`.

- [ ] **Step 4: Deploy prerequisites to note in the PR body**
  - Migration `0018` must run against the target Aurora before the org-api/report-generator code deploys (migrations apply via `MigrateFunction`).
  - Deploy `src/geocode.py`, `src/weather.py`, `src/backfill_site_coords.py`, `src/lambda_report_generator.py` (bundle per existing zip conventions), and `src/lambda_org_api.py`.
  - `backfill_site_coords` must be deployed / invoked **without** `VpcConfig` (non-VPC, mirrors `ExtractSessionFunction`). If added to `src/template.yaml`, it gets no `VpcConfig`.
  - **D-COORD follow-up:** for the AI report to include weather on existing sites, run the backfill helper (writes coords to Aurora via org-api PATCH) AND ensure `config/user_mapping.json`'s `sites` block carries `latitude`/`longitude` for each site (the report generator's read source). UI-created sites carry coords in Aurora immediately (indicator works) but appear in the report's weather only after the config carries their coord (known MVP gap).

- [ ] **Step 5: Open PRs (only when the user says so)**
  - Pipeline PR: base `develop`.
  - UI PR: base per `fieldsight-ui` current branch convention (see its CLAUDE.md "Active branches").

---

## Self-Review

**1. Spec coverage**

| Spec item | Task |
|---|---|
| §3.1 `sites.latitude/longitude` nullable; `_COLS`/`create_site`/handlers thread them | Tasks 1, 2 |
| §3.1 in-VPC org-api persist-only (no outbound) | Task 2 (`_coerce_coord`, no HTTP) + Global Constraints |
| §3.1 non-VPC backfill for coord-less sites | Task 3 (`backfill_site_coords`, non-VPC) |
| §3.2 Photon geocoder (keyless, autocomplete + geocode) | Task 3 (`geocode.py`), Task 7 (`geocodeAddress` + `AddressAutocomplete`) |
| §3.3 Open-Meteo normalize block (archive + forecast, WMO labels) | Task 4 (`weather.py`) |
| §3.4 split-VPC: weather fetched only non-VPC, coord supplied; D-COORD wiring | Task 5 + D-COORD decision |
| §3.5 factual block on report + AI-correlation prompt injection | Task 5 (`report['weather']`, `weather_prompt_block`) |
| §3.6 UI indicator reads real site coord | Task 6 |
| §3.6 UI address autocomplete fills address + coord | Task 7 |
| §4 migration `0018` (nullable double precision) | Task 1 |
| §5 D1–D6 decisions | Tasks 1-7 map 1:1; D-COORD documents §3.4's open choice |
| §6 risks (Photon NZ coverage, fair-use, coord delivery) | Graceful null-coord fallbacks (Tasks 5, 6, 7); D-COORD; caching in Global Constraints |

No spec requirement is left without a task. Non-goals (hyperlocal forecasting, migrating the UI's Open-Meteo call, paid geocoder, historical-report backfill) are respected — the UI Open-Meteo call/cache/wmoLookup/mock-fallback are explicitly kept (Task 6), and only new/regenerated reports get weather (Task 5).

**2. Placeholder scan**

No "TBD/TODO/implement later", no "add validation" (validation is spelled out in `_coerce_coord`), no "handle errors" (every `try/except` shows the code), no "similar to Task N" (the `AddressAutocomplete`/`onPick` code is repeated in full for both modals in Task 7), no "write tests for the above" (every test body is real). The one grep-then-edit in Task 7 Step 6 targets a located exact line and shows the full replacement.

**3. Type / name consistency**

- `create_site(..., latitude=None, longitude=None)` and `update_site(..., latitude=None, longitude=None)` — identical names Task 1 → consumed verbatim in Task 2's fakes and Task 3's PATCH path.
- `geocode.geocode(query, http=None, limit=5) -> {formatted, lat, lng}` — same shape consumed by `plan_coordinate_backfill` (reads `hit["lat"]`, `hit["lng"]`, `hit.get("formatted")`) and mirrored by the UI `geocodeAddress` (`{formatted, lat, lng}`).
- `weather.fetch_weather(lat, lng, date, today_iso, http=None)` — same 4 positional args used by `build_weather_block_for_site` (Task 5) and the Task 4 tests.
- `weather.normalize_weather` block keys `{date, temp_max_c, temp_min_c, weathercode, condition_label, windspeed_kmh, precip_mm, source}` — consumed by `weather_prompt_block` (reads `date`, `condition_label`, `temp_min_c`, `temp_max_c`, `windspeed_kmh`, `precip_mm`) and asserted in tests.
- `build_weather_block_for_site(site_info, target_date, today_iso, fetch=weather.fetch_weather)` — reads `site_info["latitude"]`/`["longitude"]`, matching the `sites_info`/`user_site_info` dict the report generator already builds and the `config/user_mapping.json` `sites` block (D-COORD).
- UI `orgSiteCoordCache` / `siteCoord` / `coord = siteCoord || (fixtureSite && fixtureSite.coord) || WEATHER_DEFAULT_COORD` — consistent across Task 6; `getOrgSites()` returns `{ sites: [{ site_id, latitude, longitude }] }` (from Task 2 + `_toPageSite` passthrough).

Consistent throughout.

---

**Plan complete.** 8 tasks (Tasks 1-2 pipeline Aurora/org-api; Tasks 3-5 non-VPC geocode/weather/report; Tasks 6-7 UI; Task 8 user-gated handoff).
