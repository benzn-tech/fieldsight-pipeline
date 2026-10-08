# Sub-project 3 — data follows the project; people can work on other companies' projects (design, 2026-10-09)

Status: decisions owner-approved 2026-10-08/09. Roadmap decision **D1**. Master:
`2026-10-05-tenancy-roadmap-design.md`. Sub-project 4 (company in lake keys) follows and must use
the **site's** company decided here.

## Where we are (inventory 2026-10-08, origin/develop)

- **Rows already follow the site.**
  - `topics`, `action_items`, `findings`, `report_chunks` and the `topic_*` children carry `site_id` and no `company_id`. Their tenant is `sites.company_id`, and the reads scope by `site_id = ANY(visible site ids)`.
  - `content_edits.company_id` is copied from the content row's site.
- **What is pinned to the recorder's home company** (`users.company_id`):
  - `lambda_ingest.resolve_company(folder)` decides the company **before** the site.
  - Every site rung is pinned to that company: `recordings.site_for_media/day`, `_site_from_meeting_session`, `_site_from_group_lead`.
  - The recording upload (org_api ~983) and `session_open` (~1325) refuse a site of another company and write `company_id = caller.company_id`.
  - `resolve_user(company, folder)` is company-pinned.
  - observations, decision_records, inspections and the voiceprint requests take the recorder's company.
- **Membership is blocked across companies.**
  - `_resolve_staffing` returns 404 "member not found in your company".
  - `create_member` returns 409 or 403.
  - `members_for_site`, `list_company_memberships`, `list_all_memberships` and `count_by_site` require `u.company_id = s.company_id`.
  - The group merge rejects mixed companies.
- **Reads that would leak.**
  - `_can_view_folder` and `get_timeline_compat` resolve a `?user=` folder by `get_by_folder_name(caller.company_id, folder)`. The admin/gm (ALL) tier then renders that person's day **unclipped**.
  - `topics.py ~245/1052` and `observations.py ~12` join the author by `u.company_id`.
  - The same pattern appears in `rag_search` (~149/371).
- **No cross-company membership exists yet**, so no historical row needs to move.

## Decisions (owner, 2026-10-08/09)

**P1. Who may add an external member.** A company **admin** of the site's company, or a
**platform_admin**, may add an *existing* FieldSight user of another company to one of that
company's sites:
- **How:** by the user's exact login email.
- **Access:** the person keeps their home company. The membership grants exactly that site.
- **Notice:** the person is emailed.
- **Who may not:** gm, pm and site_manager cannot add external members.
- **New users:** a person *created* inside a project is that company's employee, unchanged.

**P2. Ownership.**
- A recording, its transcript, topics, items, findings, observations, decisions, inspections and lake objects belong to the company that owns the **site** it was captured on.
- With no site, they belong to the recorder's home company.
- The recorder must hold a live membership on the site. An upload or session for a site where they have none is refused, as today.

**P3. Visibility.**
- The home company's managers do **not** see their employee's work on another company's site unless they are members of that site themselves.
- The site company's managers (admin/gm ALL tier, and members with site reach) **do** see it.
- The person sees all of their own work.
- `?user=` timeline reads are always clipped to the caller's in-scope sites.

**P4. A day that spans companies is split.** Each company sees only the part of a person's day captured on its own sites. The person sees the whole day.

**P5. Voiceprints do not cross companies.**
- Enrolment and consent stay with the person's home company.
- On another company's site, matching uses that company's enrolled voiceprints plus the **recorder's own** voiceprint. Other people from the recorder's home company are never matched there.

## Changes

1. **Membership**
   - `_resolve_staffing` and `create_member` gain an external-member path, open to company admin or platform_admin:
     - The target user is found by exact email (global lookup).
     - The site must belong to the caller's company (platform_admin: any).
     - It writes the membership with `external=true` (new column, migration).
   - It returns 409 if the person is already a member, and 404 if no such user exists.
   - The member lists and counts include external members, labelled with their home company name.
   - `archive_membership` works the same for external members.
   - A notification email is sent. Use the existing SES path.
2. **Capture**
   - `create_recording_upload_url` and `session_open` accept a site the caller holds a live membership on, whatever its company.
   - They write `recordings.company_id` / `meeting_session.company_id` as the **site's** company, or the home company when there is no site.
3. **Ingest and item-writer**
   - The site comes first. The site ladder runs **without** a company pin: media → meeting session → group lead → recording day → name/membership. Each rung requires the recorder to hold a live membership on the site.
   - `company = site.company`, or the home company when there is no site. `resolve_user` becomes a global folder lookup.
   - observations, decision_records, inspections, photos and keyframes take the resolved company.
   - Group merge: the "one company" rule becomes "one *site company*".
4. **Reads**
   - `_can_view_folder` / `get_timeline_compat` resolve the target folder globally. Every tier, including ALL, is clipped to the caller's visible site ids (P3/P4). A target with no in-scope rows → 404 for others; the person always sees their own.
   - Author joins (`topics ~245/1052`, `observations ~12`, `rag_search`) drop `u.company_id = <caller company>`. They become "author is the person; the row's site is in scope".
   - The Today and Timeline aggregates are already site-scoped. Verify they don't re-filter by author company.
5. **Voiceprints (P5)**
   - The match request for a B-site recording carries `company = site company`.
   - The matcher's candidate set is the site company's enrolled voiceprints plus the recorder's own (home company).
   - Enrolment is unchanged.
6. **Directory (sub-project 2)**: people keep `company_id` = home company. `sites[*].company_id` is the site's company. Readers that need a site's company use the site entry.
7. **UI**
   - Project members page: an admin sees "Add external member (email)". External members show a "from <Company>" badge.
   - The app and web site pickers list every site the person is a member of (org `/me site_ids` already returns them).

## Out of scope
- Billing.
- Lake key layout (sub-project 4).
- Transferring a person between companies.

## Tests that must go red without the change
- An admin adds an external member by email: 200. gm or pm: 403. Unknown email: 404. Duplicate: 409.
- An A user uploads to a B site they are a member of: `recordings.company_id = B`. Without membership: 403.
- Ingest of that recording writes topics on the B site. Item-writer company = B.
- A's admin `?user=<A user>` sees only A-site rows. B's admin sees the B-site rows. The person sees both.
- The voiceprint candidate set on a B site excludes A colleagues and includes the recorder.
- Group merge with members from two home companies on one B site is allowed.
