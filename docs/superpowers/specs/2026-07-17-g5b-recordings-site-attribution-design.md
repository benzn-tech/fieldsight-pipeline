# G5b — pipeline consumes `recordings.site_id` for site attribution

Date: 2026-07-17
Status: approved (user), pending implementation plan
Repo: fieldsight-pipeline (Python / psycopg3 / SAM, Aurora PG16)

## Problem

Extraction-sourced topics are attributed to a site by the RECORDER's Aurora membership
(`lambda_ingest.resolve_site` → `memberships.accessible_site_ids`, and only for non-`ALL`
scope users). This has two failures:

1. **Admin/test recordings never attribute.** `resolve_site` deliberately returns `None`
   for `ALL`-scope users (admin/gm have no single "home" site). So a recording made under
   an admin account (e.g. `Ben_Lin`) is skipped with "identity bridge miss", even though the
   app tagged it with an explicit site.
2. **Cross-site work mis-attributes.** A worker who is a member of site A but records at site B
   (app project pick = B) would attribute to A (their membership), not B.

Meanwhile the mobile app now uploads via the presigned recordings flow and stamps the
in-app project pick onto `recordings.site_id` (verified live 2026-07-16: recent app rows carry
`site_id = 2f6b0776…` = SB1108 Ellesmere). The pipeline ignores that column. G5b makes the
pipeline consume it.

This is the durable close of the recording↔site attribution gap
([[fieldsight-recording-site-attribution-gap]]) and the last pipeline task from the mobile
app→prod integration (GrandTime `docs/superpowers/specs/2026-07-16-app-prod-integration-design.md`
"Out of scope → G5b").

## Goal

At extraction-write time, attribute a topic's site from the matching `recordings.site_id`
(the explicit app-selected tag) when present and company-valid; fall back to the existing
membership logic otherwise. No schema change, no migration, no touch to the report path.

## Decisions (locked with user 2026-07-17)

- **D1 — Explicit tag ALWAYS wins.** When a matching recording row with a non-null,
  company-valid `site_id` exists, use it — overriding the membership fallback, even when they
  disagree. Rationale: the multi-tenant model attributes by explicit company+site tag, never by
  the recorder's role/membership. Behavior-change note: existing non-admin app uploads
  re-attribute to their tagged site (not their membership site) on re-extraction — intended.
- **D2 — Scope: `item-writer` (extraction path) ONLY.** The report path (`lambda_ingest`,
  `daily_report.json`) aggregates a whole day across recordings — no single recording/`site_id` —
  and already has the `report['site']` name-match. Leave it unchanged.
- **D3 — Multi-tenant safety is an invariant.** The looked-up site must belong to the caller's
  resolved company. Query is scoped by `recordings.company_id = company_id` AND the site's
  `company_id` is re-checked. A `site_id` whose site is not in-company is ignored (never attribute
  across tenants) → fall through to the existing fallback.
- **D4 — Match by media key, format-agnostic (approach B).** Match `recordings.s3_key` by the
  extraction's `session_base` within `users/{user_folder}/…/{date}/`, LIKE-escaped. One
  `session_base` = one recording session; no exact format→kind/ext reconstruction needed.
- **D5 — Fallback unchanged for everything else.** No matching recording / `site_id IS NULL` /
  RealPTT-pulled media (no `recordings` row at all) → the current `resolve_site` (membership)
  path runs exactly as today. RealPTT recordings never get a `recordings` row, so their behavior
  is byte-identical.

## Design

### New repo function — `repositories/recordings.py::site_for_media`

```python
def site_for_media(conn, company_id, user_folder, date, session_base):
    """The explicit app-tagged site for the recording whose media file this
    extraction session came from, or None. Matches recordings.s3_key by
    session_base within users/{folder}/…/{date}/ (LIKE, escaped), scoped to the
    caller's company, and only returns a site that is in-company (D3). Returns a
    site row (sites.get_site shape) so it drops in where resolve_site's return is
    used; None on no match / null site_id / cross-company / unparseable."""
```

Query shape (parameterised; `_escape_like` applied to `user_folder` and `session_base` because
folder names and session bases contain `_`, a LIKE wildcard):

```sql
SELECT r.site_id
FROM recordings r
JOIN sites s ON s.id = r.site_id
WHERE r.company_id = %s
  AND s.company_id = %s
  AND r.site_id IS NOT NULL
  AND r.s3_key LIKE %s ESCAPE '\'
ORDER BY r.created_at DESC
LIMIT 1
```
- LIKE pattern: `users/{esc(user_folder)}/%/{date}/{esc(session_base)}.%` — the `%` between
  folder and `/{date}/` covers the kind subfolder (audio/video/pictures); the trailing `.%`
  covers the extension. `date` is a fixed `YYYY-MM-DD` (no wildcard chars).
- `ORDER BY created_at DESC LIMIT 1`: deterministic when a session_base has multiple media rows
  (e.g. audio + video from the same session) — in practice they share the same site pick, so the
  choice is immaterial; the ordering just makes it stable.
- On a hit, return `sites.get_site(conn, site_id)` (or `s.*`) so the caller gets the same row
  shape as `resolve_site`.
- Reuse `_escape_like` (currently in `repositories/topics.py`, factored in migration-0011 work).
  If the cross-repo import reads awkwardly, move `_escape_like` to a shared `db`/repo util in the
  plan — one definition, no re-derivation.

### Modified — `lambda_item_writer.write_extraction_items`

At the site-resolution point (currently `src/lambda_item_writer.py:235`):

```python
# G5b: the app stamps the in-app project pick onto recordings.site_id; that
# explicit tag is authoritative over the recorder's membership (and is the only
# way an admin-account recording attributes). Fall through to the legacy
# membership resolver only when there is no matching, company-valid tag.
site = recordings.site_for_media(conn, company["id"], user_folder, date, session_base) \
       or lambda_ingest.resolve_site(conn, company["id"], {}, user_folder)
if site is None:
    reason = (f"identity bridge miss: user_folder={user_folder!r} -- "
              f"skipping extraction, zero writes")
    logger.warning("%s: %s", extraction_key, reason)
    return {"skipped": True, "reason": reason}
```

- `session_base` is available: `_parse_extraction_key(extraction_key)` already yields it (parse it
  in `write_extraction_items` or thread it in from the handler).
- `recordings` is already an import target of item-writer's repo layer (or add the import).
- Everything after (user_id via `resolve_user`, topic/child inserts, photo attach) is unchanged —
  they consume `site` the same way.

### Precedence / fallback table

| Case | Result |
|---|---|
| App recording, `site_id` set, in-company | **use `recordings.site_id`** (overrides membership) |
| App recording, `site_id` set, cross-company | ignore (D3) → fall to membership |
| App recording, `site_id` NULL | fall to membership |
| No matching recording (RealPTT-pulled, or none) | membership (unchanged) |
| Neither tag nor membership resolves | skip, zero writes (unchanged) |
| Admin account + app recording (tagged) | **attributes** (was: skipped) — the headline fix |
| Admin account + RealPTT recording (no row) | skip (unchanged) |

## Testing

Unit (existing FakeConn / monkeypatch patterns):
- `repositories/recordings.py`:
  - `test_site_for_media_matches_by_session_base_returns_site`
  - `test_site_for_media_escapes_like_wildcards_in_folder_and_session` (folder `Ben_Lin`,
    session `Ben_Lin_2026-07-16_09-50-00` — the `_` must be escaped, not matched as wildcard)
  - `test_site_for_media_null_site_id_returns_none`
  - `test_site_for_media_cross_company_site_returns_none` (row in company A but site in company B)
  - `test_site_for_media_no_row_returns_none`
  - `test_site_for_media_orders_by_created_at_desc` (two media rows same session_base → newest)
- `tests/unit/test_lambda_item_writer.py`:
  - `test_recording_site_overrides_membership` (recording tag wins over a resolvable membership)
  - `test_falls_back_to_membership_when_no_recording`
  - `test_admin_recording_attributes_via_tag_not_skipped` (the admin fix; membership would skip)
  - `test_no_tag_no_membership_still_skips`

Live smoke (after merge→deploy; before prod, per repo rules): re-trigger an extraction for an
app-uploaded (folder,date) whose `recordings.site_id` = Ellesmere → Data-API confirms the topic's
`site_id` = Ellesmere (not skipped). An admin-account app recording is the acceptance case.

## Out of scope

- Report path (`lambda_ingest`) — unchanged (D2).
- `extract-session` — unchanged (`declared_site` stays record-only).
- No migration, no template change (read-only lookup on an existing table/column).
- Mobile app G1–G5a — shipped separately (GrandTime, prod-accepted).
