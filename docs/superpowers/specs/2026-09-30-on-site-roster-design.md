# Who is on site today — roster narrowing for speaker identity (design, 2026-09-30)

**Owner decision (2026-09-29):** "a very good feature; we need to connect upstream and
downstream — Outlook, SignOnSite, 1Breadcrumb, HammerTech". Integration style chosen:
**server-side API connectors, not MCP** (the roster must exist before a recording is
processed, deterministically; MCP is for an agent calling tools at question time).

**Line:** voiceprint. **Customer rule:** plain words, no scores.

## Why

`decide_name` compares a turn against every consented profile in the company (1:n). The
wider n is, the more a stranger can resemble somebody and the narrower every margin gets
(`effective_margin` grows with pool size). On a given day only a handful of people are on
a given site. Narrowing the candidates to them turns 1:n into 1:k with k small, which
raises recall and cuts false names at the same time — and it is the precondition for
"sign in at the gate = enrol your voice" later.

## Shape

One internal table that every source writes and every consumer reads. No consumer knows
which system a row came from.

```
site_attendance(
  id uuid PK, company_id uuid NOT NULL, site_id uuid NOT NULL,
  attend_date date NOT NULL,               -- NZ date (nz_time), never a UTC date
  display_name text NOT NULL,              -- as the source spells it
  user_id uuid NULL,                       -- resolved to our directory when possible
  voiceprint_id uuid NULL,                 -- resolved to a profile when possible
  employer_name text NULL,
  source text NOT NULL,                    -- 'graph_calendar' | 'signonsite' | 'hammertech' | '1breadcrumb' | 'manual'
  source_ref text NOT NULL,                -- the source's own id, for idempotent upserts
  first_seen_at timestamptz, last_seen_at timestamptz, created_at timestamptz,
  UNIQUE (company_id, source, source_ref, attend_date)
)
```

### Consumers (this line)

1. **Matching narrows, never blocks.** The writer's `_profiles` returns the full consented
   list as today; when attendance exists for (site, date), profiles whose `voiceprint_id`
   or `user_id` is in it come first and the margin is computed against that subset only.
   **If attendance is empty or missing, behaviour is exactly today's** — an absent
   integration must never make recognition worse.
2. **A name outside the roster is capped at tentative.** Somebody can be on site without
   having signed in; they can still be named, but never `confirmed` on a day the roster
   says they were not there.
3. **Suggestions use it.** The self-introduction dialog prefers roster names for
   the prefill (the heard name "Petrus Pang" → "Petros Pan", who signed in today).

### Sources (connectors)

Each connector is a scheduled Lambda outside the VPC (it calls the internet) that writes
`attendance_requests/{company}/{date}/{source}.json`; the existing in-VPC writer pattern
upserts rows (BUG-36: in-VPC functions make no outbound calls; S3 request artifacts cross
the boundary). Per-company credentials live in Secrets Manager, read by the out-of-VPC
connector only.

| Order | Source | What it gives | Needs from the owner / customer |
|---|---|---|---|
| 1 | Microsoft Graph (Outlook/Teams calendar) | meeting attendees by date | an Entra app registration; each customer's admin consents once |
| 2 | SignOnSite | who signed in at which site | API access (enterprise / partner) — to be confirmed |
| 3 | HammerTech | site sign-ins, inductions | API access — to be confirmed |
| 4 | 1Breadcrumb | site sign-ins | API access — to be confirmed |
| 0 | `manual` | a site manager lists today's people | nothing — ships first, proves the consumer |

**`manual` ships first.** It needs no third party, lets the consumer be tested on real
days, and stays useful for customers with no sign-in system.

## Out of scope

Enrolment at sign-in (next roadmap item; depends on this), sending anything back to the
upstream systems, and any UI beyond a minimal manual entry.

## Tests that must go red without the change

- Matching with attendance present: a turn that ties between an on-site and an off-site
  profile names the on-site one; with attendance absent the result equals today's.
- An off-roster best match is never `confirmed`.
- Attendance dates are NZ dates (a 07:00 NZDT sign-in lands on that NZ day).
- Migration and upsert run on `fieldsight_test` in a rolled-back transaction.

## Review outcome (Fable review, 2026-09-30) — supersedes the sections above where they differ

Adopted from the review (details and evidence in the plan):
- Narrowing lives in the voiceprint WRITER (`_profiles` returns `on_roster` per profile); the
  embedder applies the rule. The embedder→writer `profiles` op gains `date`.
- **Roster rule:** score the full pool first. Full-pool winner off the roster → capped at
  `tentative`. Winner on the roster → re-decide over the roster subset. A one-person roster
  narrows nothing (falls back to the full-pool result). Absent roster → exactly today's
  behaviour.
- Attendance is resolved to profiles at LOOKUP (voiceprint_id / user_id / display name), not
  stored once, so a profile enrolled later still matches.
- Connectors invoke the writer directly (non-VPC → in-VPC is allowed, BUG-43 note 4) instead
  of a new S3 prefix; no new hand-wired trigger.
- `manual` source has org-api routes guarded by `_allowed_site_ids`; roles
  `_CORRECTION_ROLES`.
- Migration is 0072 (0071 is the intro suggestions table).

Owner decisions taken on the owner's standing instruction (long-term option): the roster
rule above; direct invoke; `_CORRECTION_ROLES` for manual entry. Deferred to the owner, and
Phase 2 stays BLOCKED until answered: which calendar defines "on site" for Graph (plan
assumes a per-site shared mailbox), and the Entra app registration.
