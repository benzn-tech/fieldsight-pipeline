# One person, one company — the constraint, and what it would cost to change

**Status:** decision record. No code change. Written because the constraint is invisible
and the obvious "fix" is worse than the limitation.

**Question that prompted it:** a subcontractor works for several main contractors. How is
that handled?

**Answer today: it is not.** One physical person = one folder = one company.

---

## 1. Three things lock it, and they lock each other

| what | where | why it is there |
|---|---|---|
| `users.company_id` is a single column | `0002_core_relational.sql:11` | a user belongs to exactly one company |
| `folder_name` is **globally** unique | `0012` | was per-company in 0007; widened deliberately |
| the shared lake routes **by folder_name alone** | `0012`'s own comment | `users/{folder}/…`, `reports/{date}/{folder}/…` |

0012 states the reason in the migration itself: *"two companies claiming one folder would
silently cross-attribute data. Fail loudly at onboarding instead."*

So the uniqueness is not bookkeeping. **It is the tenant routing key.** Data arriving in S3
carries no company — the folder in the path is what decides whose it is.

`create_member` refuses to re-parent an existing user for the same reason, with
`409 user already belongs to another company`, and its comment says so.

## 2. What a person CAN have

Any number of sites, inside one company. `memberships (user_id, site_id, role)` with
`UNIQUE (user_id, site_id)` and no count limit. **Company is the only hard boundary**; a
project is not.

## 3. What actually happens today for a cross-contractor subcontractor

Two accounts, two emails — Cognito keys identity on email, so two emails are two subjects,
two users, two folders. Their recordings land under whichever account is signed in.

**The failure this invites is bounded, and that was checked rather than assumed.**
`lambda_item_writer._site_from_meeting_session` re-verifies the resolved site's company
against the company the folder resolved to and returns None on mismatch; `session_open`
rejects a cross-tenant site upstream of that. So recording under the wrong account does
**not** put one main contractor's words in another's tenant. It produces a recording in the
signed-in account with no site attribution — visible, inside one tenant, fixable.

That is the difference between an inconvenience and a privacy incident, and it is the
reason the two-account workaround is acceptable rather than dangerous.

## 4. Do not "fix" this by adding a join table

`user_companies (user_id, company_id)` looks like the obvious change and is the wrong one:
it makes a person multi-tenant in Aurora while the **S3 lake still routes on one folder**.
The pipeline would keep attributing every recording to whichever company that folder maps
to, and now nothing would fail loudly, because the join table would say the mapping is
legitimate. 0012 chose a loud failure over exactly this silence.

A real change moves the routing key from *person* to *person × context*:

```
users/{folder}/…                →  users/{company}/{folder}/…
reports/{date}/{folder}/…       →  reports/{date}/{company}/{folder}/…
```

which reaches: the device upload path, VAD/transcribe/extract key parsing,
`session_scope.EXTRACTION_KEY_RE`, the deletion tombstone prefixes, every S3 trigger prefix
filter, and a migration of everything already written. **Weeks, and touching the paths that
decide who sees what.**

## 5. The commercial question comes first, because it decides the design

Pricing is per capture seat.

* **Each main contractor pays for their own seat** → two accounts is not a workaround, it is
  the correct model. Nothing to build.
* **One seat shared across contractors** → then multi-company is needed, and it immediately
  raises the question the technical design hangs on: can main contractor A's admin see what
  that person recorded at B? If the answer is no, what is needed is not multi-company
  accounts but per-site visibility isolation — a different design again.

**Answer that before building anything.**

## 6. What would change the recommendation

A real customer with a subcontractor spanning two main contractors, on one device, inside
one week. Not "later, if". At that point this outranks span deletion, because it decides the
correctness of data ownership rather than the convenience of a feature.

## 7. One thing that looks like an improvement and is not

`409 user already belongs to another company` does not name the company, and `error()`
already supports an `extra` payload, so naming it looks like a free win.

**It is a tenant information leak.** Telling an admin of company A the name of company B —
which they are not in — is exactly the disclosure the company boundary exists to prevent.
The vague message is correct. The fix belongs in the UI: show the message, and add the
action ("this email already belongs to another organisation — invite a different address, or
contact us").
