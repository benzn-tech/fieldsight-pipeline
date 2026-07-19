# Site Location & Weather — Design (2026-07-19)

**Status:** Design / for review.
**Goal:** Drive weather from each site's **real address** (not a mock/default coordinate) and surface it (a) on the app's weather indicator and (b) inside the AI-generated daily report, where the AI correlates weather with on-site observations. Adds address→coordinate resolution (geocoding) plus an address-autocomplete affordance on site create/edit.

**Repos:** fieldsight-pipeline (Aurora `sites` columns, a non-VPC geocode backfill helper, weather fetch + report-prompt injection) + fieldsight-ui (address autocomplete, weather indicator pointed at real coords).

---

## 1. Problem

Two gaps behind the user's ask ("connect a weather API by project address, and put weather into the agent report"):

### 1.1 Weather exists in the UI but is NOT address-driven
`fieldsight-ui/scripts/app-shell.js` already integrates **Open-Meteo** (free, keyless): `WeatherIndicator()` calls the forecast API (`current_weather`) for today and the archive API for past dates, maps the WMO 4677 weather code, caches per coord+date, and falls back to `MockData` on error. But the coordinate is
```js
const WEATHER_DEFAULT_COORD = { lat: -43.5321, lng: 172.6362 };           // Christchurch
const coord = (activeSite && activeSite.coord) || WEATHER_DEFAULT_COORD;   // activeSite from MOCK fixtures
```
— i.e. it reads `coord` off the **mock `fixtures.sites`** entry, else the hardcoded Christchurch default. Real sites carry no coordinate, so the weather shown is never the site's actual weather.

### 1.2 The generated report has NO weather
`lambda_report_generator.py` only uses `"weather"` as one label in a programme-event `type` enum (`deadline | inspection | delivery | weather | meeting | other`). No actual conditions are fetched or written; the AI daily report never mentions weather.

### 1.3 Sites store a text address only — no coordinates
Migration `0013_site_address.sql` added `sites.address text` (freeform). There is no `latitude`/`longitude`, so nothing can turn an address into a weather lookup. `sites._COLS` (`src/repositories/sites.py:3`) = `id, company_id, name, location, client, industry, icon_s3_key, created_at, archived_at, slug, address`.

---

## 2. Goals / non-goals

**Goals**
1. Each site resolves to a **coordinate** derived from its real address, stored in Aurora.
2. Site create/edit gets **address autocomplete** (type-ahead) that fills the address **and** the coordinate in one pick.
3. The **weather indicator** uses the active site's real coordinate.
4. The **AI daily report** carries a **factual weather block** for the report date AND feeds that weather into the report prompt so the AI **correlates** it with observations (e.g. "rain delayed the pour", "high wind affected crane ops").
5. Zero paid keys, minimal cost: geocode **once per site**, weather cached **once per (site, date)**.

**Non-goals**
- Hyperlocal / hourly forecasting, weather alerts/push, or a weather history page — daily granularity for the report date only.
- Migrating the UI's existing Open-Meteo call — it stays; only its coordinate source changes.
- A paid geocoder — Photon (free, keyless) is chosen (§3.2); revisit only if NZ address coverage proves inadequate.
- Backfilling weather into historical reports — new/regenerated reports only.

---

## 3. Design

### 3.1 Site coordinates (`sites.latitude` / `sites.longitude`, nullable)
Migration adds two nullable numeric columns; `sites._COLS`, `create_site(...)`, and the create/patch site handlers (`create_org_site`/`patch_org_site` in `lambda_org_api.py`) thread them through. Nullable because existing rows predate coordinates and get backfilled (§3.3).

Populated by **two paths**, so the **in-VPC org-api never makes an outbound call itself** (BUG-36 — an in-VPC Lambda with no NAT/egress black-holes on any external HTTP):
- **On create/edit (primary):** the UI address field uses Photon autocomplete (§3.5); picking a suggestion gives the browser `{formatted address, lat, lng}` already, so the UI sends `latitude`/`longitude` in the create/patch-site body → the handler just persists them. **No backend geocoding on this path.**
- **Backfill (secondary):** a **non-VPC** geocode helper (mirrors the repo's existing non-VPC pattern — e.g. `ExtractSessionFunction`/`ReportGeneratorFunction` shape, no `VpcConfig`) geocodes any site with an `address` but null coordinates via Photon, once, and writes the coordinates back through the org-api (or a one-shot admin invoke). Runs on demand / low-frequency; never per-request.

### 3.2 Geocoding provider — Photon (OSM/Komoot), free, keyless (D2)
Open-Meteo's own geocoder is **place-name** only (cities/towns), unusable for a street address, so a real address geocoder is required. **Photon** (`https://photon.komoot.io/api?q=<query>&limit=5`, optional `&lat=&lon=` bias, `&lang=en`) is open-source, keyless, **designed for type-ahead autocomplete**, and returns GeoJSON features with `geometry.coordinates [lng, lat]` + structured `properties` (housenumber/street/city/postcode/countrycode) — one call serves BOTH the autocomplete list and the chosen coordinate. NZ coverage is OSM-level (good in towns, patchier rural); public API is fair-use (self-host is the escape hatch if volume grows). No key, no billing.

### 3.3 Weather provider — Open-Meteo (unchanged, free, keyless)
Reuse the exact call the UI already proved:
- **Past date (archive):** `https://archive-api.open-meteo.com/v1/archive?latitude=&longitude=&start_date=&end_date=&daily=temperature_2m_max,temperature_2m_min,weathercode,windspeed_10m_max&timezone=Pacific/Auckland`.
- **Today (forecast):** `https://api.open-meteo.com/v1/forecast?latitude=&longitude=&current_weather=true&daily=...&timezone=Pacific/Auckland`.
Normalize to a **weather block**: `{ date, temp_max_c, temp_min_c, weathercode, condition_label, windspeed_kmh, precip_mm, source: 'open-meteo' }` (WMO 4677 → label via the same `wmoLookup` table the UI has).

### 3.4 Weather resolver + the split-VPC constraint (D3)
Weather needs **two things that live on opposite sides of the VPC boundary**: the site's coordinate (Aurora, in-VPC) and an external HTTP call (non-VPC egress). This is the same split the repo already solves for SP-Ask (non-VPC synth ↔ in-VPC retrieval) and voice-fanout (in-VPC resolve ↔ non-VPC `@connections`). So:

- Weather is a **per-(site, date) fact**, cached once in the item store (keyed `(site_id, date)`), fetched on demand at report-generation and read by the dashboard/indicator thereafter.
- The fetch runs **only in a non-VPC context** (which has egress); the coordinate is **supplied to it** rather than read from Aurora inside the outbound call.

**Recommended concrete wiring (plan confirms):** `ReportGeneratorFunction` is **already non-VPC** and already makes the daily-report Claude call, so it is the natural single home for both the Open-Meteo fetch and the AI correlation — no new multi-hop Lambda. It needs the site's coordinate; deliver it by the cheapest available of: (a) extending the S3 site config the generator already reads (`config/user_mapping.json` v2 `sites`) with `lat/lng`, published from Aurora when a site is created/edited; or (b) passing `lat/lng` in the generator's invocation payload from the in-VPC step that triggers it. The plan picks one after reading the current trigger path. (If the dashboard-first prose generator has moved off `ReportGeneratorFunction`, the same "non-VPC fetch + coord-supplied" rule applies to whichever function now emits the per-(site,date) prose.)

### 3.5 Report integration — factual block + AI correlation (D4)
- **Factual block:** the normalized weather block is written onto the daily report / dashboard record for `(site, date)` and rendered at the top of the report (temp, condition, wind, precip).
- **AI correlation:** the same block is injected into the report-generation **prompt**, with an instruction: *"Site weather for this day was {…}. Where an observation plausibly relates to weather (rain → concrete/paint/earthworks delays; high wind → crane/height work; heat/cold → pours/curing), note the linkage explicitly; do not invent impacts the transcript doesn't support."* — so correlation is grounded in the day's actual observations, not fabricated.

### 3.6 UI (D5)
- **Weather indicator:** replace `activeSite.coord`-from-fixtures with the active site's real `{latitude, longitude}` fetched from Aurora via the org API (the site record now carries them). Keep the existing Open-Meteo call, cache, WMO lookup, and mock fallback unchanged. On a null coordinate (un-backfilled site) fall back to the current default rather than breaking.
- **Address autocomplete:** the create/edit-site address field becomes a Photon type-ahead — debounced query → suggestion list → on pick, fill the address text **and** stash `latitude`/`longitude` into the form, submitted with the site. Graceful: on Photon error/no-result the field stays a plain free-text input (coordinates left null → backfill later).

---

## 4. Data model change

```sql
-- 0018 (next free number; 0017 is action_item audit): site coordinates for
-- weather + map features. Nullable — existing rows backfill via Photon.
ALTER TABLE sites ADD COLUMN latitude  double precision;
ALTER TABLE sites ADD COLUMN longitude double precision;
```

---

## 5. Decisions

- **D1 — Coordinate storage:** two nullable columns on `sites`; populated by UI-autocomplete pick (primary) + non-VPC Photon backfill (existing sites). In-VPC org-api never calls out. (§3.1)
- **D2 — Geocoder:** Photon (OSM/Komoot), free/keyless, autocomplete-capable, returns coordinate — one provider for both autocomplete and geocode. (§3.2)
- **D3 — Weather provider + fetch locus:** Open-Meteo (unchanged); fetched only in a non-VPC function with the coordinate supplied to it (split-VPC), cached per (site, date). (§3.3–3.4)
- **D4 — Report weather:** factual block on the report + injected into the AI prompt for grounded correlation. (§3.5)
- **D5 — UI:** indicator reads real site coordinate; create/edit address field gets Photon autocomplete filling address + coordinate. (§3.6)
- **D6 — Cost/caching:** geocode once per site; weather cached once per (site, date); no keys, no billing.

---

## 6. Risks / open questions

- **Photon NZ coverage** — OSM addresses are good in towns, patchier rural; a miss leaves coordinates null → weather falls back to default and the site is a backfill candidate. Acceptable; escalate to a paid geocoder only if real sites miss often.
- **Photon/Open-Meteo fair-use** — public endpoints are rate-limited; the once-per-site / once-per-(site,date) caching keeps volume tiny. Self-hosting Photon is the escape hatch.
- **Coordinate delivery to the non-VPC generator** (§3.4) is the one genuinely open wiring choice — resolved in the plan after reading the current report-prose trigger path. It does not change the user-facing design.
- **`ReportGeneratorFunction` post authority-flip role** — if per-(site,date) prose has moved to a different function, the injection point moves with it; the "non-VPC fetch + coordinate supplied" rule is unchanged.

---

## 7. 中文摘要

需求:天气要**按项目真实地址**驱动(现在 UI 的 Open-Meteo 用的是 mock/写死的 Christchurch 坐标),并把天气放进 **AI 生成的日报**,让 AI 把天气和现场观察关联。

设计:`sites` 表加 `latitude`/`longitude`(可空)。坐标两条来源——建/改站时 UI 用 **Photon**(免密钥、支持自动补全)选中地址即拿到坐标一起提交(in-VPC org-api 不外呼,避 BUG-36);存量站点由**非-VPC 辅助**用 Photon 一次性回填。天气仍用 **Open-Meteo**;因为天气同时需要 Aurora 坐标(in-VPC)和外网调用(非-VPC),走仓库已有的 **split-VPC** 模式:只在非-VPC 处抓取、坐标作为入参传入,按 `(站点,日期)` 缓存。推荐落在**本就非-VPC 的 `ReportGeneratorFunction`**(它已做每日 Claude 调用),抓天气 + 注入 prompt 让 AI 关联影响(降雨→浇筑推迟、大风→塔吊),同时在报告顶部显示事实天气块。UI:天气指示器改读该站真实坐标;建/改站地址框加 Photon type-ahead(选中同时填地址 + 坐标)。全部免费额度,坐标每站编码一次、天气每(站,日)缓存一次。
