# Procore developer sandbox — getting to a first authenticated call

**Date:** 2026-09-24 · **Status:** runbook, written from public documentation and the
2026-09-09 spike; the portal itself was not reachable from the session that wrote this, so
**every UI label below is to be checked against the screen**. Corrections go into this file.
**Follows:** `specs/2026-09-09-procore-integration-feasibility.md`.

## The message you saw, and what it is not

> *Promote Your App to View OAuth Credentials — To generate your production OAuth credentials,
> your app must include a data connector component and be promoted to the production environment.*

That panel is the **Production** credentials section. It stays empty until (a) the app has a
data connector component and (b) a version has been promoted. **None of that is needed for the
sandbox.** Sandbox credentials live in a different section and appear as soon as the app has a
data connector component and a saved version. Production promotion is a later, separate act
and is also where the verification tier (Private App Developer vs Marketplace Partner, spike
§5.2) starts to matter. Not now.

## Steps (portal)

1. **Open the app → Configuration Builder.**
2. **Data Connector Components → Add Components.** Choose the **service-account / DMSA**
   option (client credentials), *not* "User Level Authentication". The authorization-code
   flow is user-scoped, its refresh token is single-use, and a lost refresh response is a
   full re-authorisation — the spike ruled it out (§3.1).
3. **Declare permissions** in the component. For the probe and Blocks B/C, read on:
   Company Directory (users), Projects, Schedule. Add read on Daily Log, Photos, RFIs,
   Observations so the probe can tell us whether those tools are reachable under a DMSA
   manifest (spike §7). Writes come later and are a product decision (RFI cannot be
   recalled).
4. **Create Version** (semantic number, e.g. `0.1.0`). Saving a version is what makes the
   credentials appear.
5. **Manage App → OAuth Credentials**: copy the **Sandbox** Client ID and Client Secret.
   These do not work against production hosts and production ones do not work here
   (§3.2).
6. **Install the version into the developer sandbox company.** A Developer Sandbox is
   created automatically with the app (minutes; cannot be reset; seeded project
   *1234 – Sandbox Test Project*). In that company: Company Admin → App Management →
   Install custom app → paste the App Version ID → Install. The install is where the
   company admin approves the permissions and picks projects — the same step a customer
   will do, and the one that has to be redone on every version (§5.4).
7. Note the sandbox **company id** (visible in the company URL or via `/companies`).

## Steps (here)

```bash
export PROCORE_CLIENT_ID=...          # sandbox pair, never the production pair
export PROCORE_CLIENT_SECRET=...
export PROCORE_COMPANY_ID=...
python scripts/procore_probe.py --json /tmp/procore_probe.json
```

The probe is read-only and stops at the first failure with the status and body. Expected
first-run shapes:

| step | pass | means |
|---|---|---|
| token | 200, `expires_in=5400`, no `refresh_token` | DMSA works; §3.1 confirmed |
| companies | 200 with the sandbox company | the `Procore-Company-Id` header is right (§6: needed even here) |
| companies | 401 | app not installed in the sandbox company, or production key against sandbox host |
| projects | ≥1 project | seeded project present |
| users / schedule | counts | Block B / C inputs exist |
| write targets | reachable / 403 / 404 per tool | closes §7's "reachable under DMSA" question |
| rate headers | numbers | closes §7's "actual limit values" question |

Paste the `findings` block into `specs/2026-09-09-procore-integration-feasibility.md` §7 and
mark each item settled.

## What is deliberately not in the probe

- No POST. Writing into the sandbox is fine in principle, but the probe exists to establish
  facts, and the first write belongs to Block D with the tenant mapping in front of it
  (§4.1: one credential set serves every company; the mapping table is the only source of
  `company_id`, no default).
- No webhook creation. Which resources emit webhooks is answered from the portal's
  downloadable resource list, not by subscribing.
- No token caching. 90-minute tokens; the connector will cache, the probe does not need to.

## What unblocks after the probe

Track C's Block A (`procore_connections` table, non-VPC connector Lambda, S3 request files
into the in-VPC writer — BUG-36) and Block B (directory → `sites` / `users` / `memberships`).
Block D waits for Track B's stable ids (`origin_id`).
