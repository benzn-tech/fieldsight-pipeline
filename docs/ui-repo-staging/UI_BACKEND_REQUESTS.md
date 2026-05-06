# UI_BACKEND_REQUESTS.md — Append-only log

> Owner: UI repo (`fieldsight-ui`)
> Mirror reviewed weekly by: pipeline repo (`fieldsight-pipeline`)
> Format: one entry per request. Append at the bottom; do not edit older entries.

---

## How to file a request

When the UI repo needs something the backend does not yet expose, append an entry below with:

```
### YYYY-MM-DD — <short title>
**Sprint:** <e.g. 9.2>
**Endpoint or change requested:** <method + path or behaviour>
**Why:** <UI surface that needs it>
**Proposed schema (if applicable):**
```json
{ ... }
```
**Status:** ⬜ Filed | 🟡 In progress | ✅ Done | ❌ Won't do
**Backend response:** <left empty by UI; pipeline maintainer fills in>
```

Pipeline maintainer reviews this list weekly, opens issues / PRs in `fieldsight-pipeline`, and updates **Status + Backend response** in place.

---

## Open requests

(none yet — first entries land in Sprint 9)

---

## Closed requests

(none yet)
