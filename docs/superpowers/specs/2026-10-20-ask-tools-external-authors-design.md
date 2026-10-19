# Ask tools: the external flag in the people footprint (and two parked gaps that are not real)

Date: 2026-10-20. Line: search & vectors (owner of `ask_tools` / `rag_tools_dispatch` /
repo SQL). Source: the tool layer's parked follow-ups (1)-(3) from PR #1128. Each was
checked against `origin/develop` @ `1e3b6eec` before this design. One is real, two are not.

## 1. Problem

### 1.1 (Real) The memberships arm of the footprint ignores `memberships.external`

`recordings.author_footprint_in_sites` (`src/repositories/recordings.py:722-736`) and its
set-returning sibling `recordings.author_candidates_in_sites` (`:847-861`) decide who is
"in reach" for Ask's `find_people` (`users._candidates`, `users.py:384`), for
`users.person_in_reach` (`users.py:407-423`, re-run on every tool call that carries a
`person`), and for two org-api folder gates (`lambda_org_api.py:10790` `_can_view_folder`,
`:10857` `_all_tier_may_open`). Each has three arms: a topic on the sites, a recording on
the sites, or a live membership on the sites. The membership arm is

    SELECT user_id FROM memberships WHERE site_id = ANY(...) AND archived_at IS NULL

What "the external flag" means. Migration `0085_memberships_external.sql` added
`memberships.external boolean NOT NULL DEFAULT false`: true marks a person of ANOTHER
company put on one site of this company (project-owned tenancy P1). Every roster read
enforces the invariant "a live membership counts only if the person is an employee of the
site's company, or the row is flagged external":

    (u.company_id = s.company_id OR m.external)

`members_for_site` (`memberships.py:222`), `count_by_site` (`:181`, whose docstring calls
an unflagged cross-company row "bad data, still dropped"), `list_company_memberships`
(`:161`), `list_all_memberships` (`:200`). The footprint arm does not apply it.

What the arm admits that the roster does not: a live, **unflagged** membership whose user
is not an employee of the site's company. The writers never create one --
`add_external_membership` always sets `external=true` (`memberships.py:90-100`);
`create_member` and `put_member_membership` require the target's company to equal the
site's, or an existing external row (`lambda_org_api.py:6337-6372`, `:6625-6627`);
`ensure_membership`'s upsert never touches `external`. The one path that produces it
today is **a site move**: `sites.move_site_company` (`sites.py:106-118`, called at
`lambda_org_api.py:5193`, platform_admin only) "touches the sites row ONLY: memberships
... are left alone". After the move every old-company employee row on that site is an
unflagged cross-company row. (A user with `company_id IS NULL` holding a membership is the
other, legacy, case; `u.company_id = s.company_id` is NULL there, so the roster drops it
too.)

Failure scenario. Site S moves from company A to company B. Alice, an A employee, holds a
live worker membership on S but never recorded there. B's roster for S does not list her.
But:

- B's pm or admin asks Ask about "Alice": `find_people` returns her `person_ref`, display
  name and `folder_name` -- a person of another company with no work on B's site.
- `person_in_reach` is true for her ref, so `day_overview` / `search_records` narrowed to
  her run (they return nothing: there are no rows of hers on S -- the leak is the
  identity, not content).
- B's admin can open her folder on `/timeline` (`_can_view_folder`, `_all_tier_may_open`);
  the per-site row filtering then shows nothing of hers, but the folder "exists" for them.

What it does NOT affect: anyone with a topic or recording on S (the other two arms admit
them, which is project-owned tenancy working as designed), and flagged external members
(admitted either way). Adding the predicate only removes; it never hides a legitimate
person. Severity: low (a name/folder disclosure, no record content).

### 1.2 (Not real) "An external author whose folder collides with a home-company folder is unsearchable"

The `KNOWN GAP` in `_search_author_folder` (`rag_tools_dispatch.py:150-176`) assumes two
users can share a `folder_name`. They cannot: `0012_folder_name_global_unique.sql` creates
`idx_users_folder_global ON users (folder_name) WHERE folder_name IS NOT NULL`, and no
later migration drops it. `_search_author_folder` takes the user's own folder from
`users.get_by_id`, then mirrors rag-search's resolution
(`lambda_rag_search.py:463-477`: company-pinned `get_by_folder_name`, then
`get_by_folder_name_global`; global only for cross-company). Both lookups are exact
(`users.py:93-106`), so with a globally unique folder they can only return that same user;
the id check at `:172` always passes. The only remaining miss is a user with
`folder_name IS NULL`, who cannot have searchable chunks (recordings and extractions are
keyed by folder at ingest) and whose folder cannot be cleared once set (the only writer,
`users.set_folder_name`, is reached only with a validated non-empty `folder_key`,
`lambda_org_api.py:6313-6333`).

So the suggested author-by-user-id field on the rag-search event fixes nothing observable.
It stays a valid simplification (it would drop two lookups per narrowed search) but it is
a contract change with no defect behind it; it is **not** in this design. The `KNOWN GAP`
docstring is wrong and should be corrected to say why the check cannot fail except for a
NULL folder (a comment-only change, folded into the implementation PR).

### 1.3 (Not real today) "An archived external site cannot be found by name"

`_resolve_site` (`rag_tools_dispatch.py:104-121`) resolves only inside `sc["site_ids"]`:
a UUID by set membership, a name through `sites.list_sites_by_ids` (`sites.py:39-45`),
which adds `archived_at IS NULL`. The premise is a site that is archived AND in the
caller's reach. No such site exists:

- admin/gm: `list_company_sites` (`sites.py:22-26`) excludes archived by default;
  platform_admin: `list_all_sites` (`:29-36`) the same. No `src/` caller passes
  `include_archived=True` into scope.
- every other tier, and the external branches: reach comes from live memberships
  (`caller_site_roles` `memberships.py:229`, `external_site_roles` `:293`), and
  `archive_site` (`sites.py:62-77`) archives every membership on the site, external
  included. `unarchive_site` does not restore them; `add_external_member`,
  `create_member` and `put_member_membership` all refuse an archived site (409)
  (`lambda_org_api.py:6426`, `:6628`, `:6358`). Covered by
  `tests/integration/test_archive.py::test_archive_site_cascades_memberships`.

So the name miss is the same miss the UUID branch produces, and the same nothing every Ask
read returns for that site: no disclosure, no omission relative to the ACL. The two
branches would only diverge if `visible_scope` ever admitted an archived id. Not in this
design. Whether Ask should read archived-site history at all is a visibility decision
(§5), not a tool-layer fix.

## 2. Goal

The footprint -- both forms -- admits a membership-only person exactly when the site's own
roster lists them: an employee of the site's company, or a flagged external member.

Unchanged: the topics and recordings arms; archived USERS stay findable
(`test_archived_user_with_footprint_is_still_found`); the `find_people` result shape and
`meta`; `search_records`' author path and the rag-search event contract; the plain Ask
and search-box paths (no file of theirs changes); `visible_scope` and the roster reads.

## 3. Design

One predicate, applied to the membership arm of both functions in
`src/repositories/recordings.py`, so they keep their documented promise of never
disagreeing.

`author_footprint_in_sites`:

    ... OR EXISTS (SELECT 1 FROM memberships m
                   JOIN users u ON u.id = m.user_id
                   JOIN sites s ON s.id = m.site_id
                   WHERE m.user_id = %s AND m.site_id = ANY(%s::uuid[])
                     AND m.archived_at IS NULL
                     AND (u.company_id = s.company_id OR m.external))

`author_candidates_in_sites`:

    UNION SELECT m.user_id FROM memberships m
          JOIN users u ON u.id = m.user_id
          JOIN sites s ON s.id = m.site_id
          WHERE m.site_id = ANY(%s::uuid[]) AND m.archived_at IS NULL
            AND (u.company_id = s.company_id OR m.external)

The predicate text is copied from the roster reads, not reinvented. No
`u.archived_at` filter (the footprint deliberately keeps departed people findable).

Deploy tolerance: none. `memberships.py`'s pre-0085 fallback exists for the minute between
`sam deploy` and the migration step; 0085 is applied on TEST and prod, so a new reader of
the column needs no fallback. (In Ask a failure would be one call's `meta.error =
"upstream"`, never a wider result.)

Callers outside the tool layer. `_can_view_folder` and `_all_tier_may_open`
(`lambda_org_api.py:10790`, `:10857`) use the boolean form, so the `/timeline` folder
gate tightens identically: an ALL-tier admin of company B no longer opens the folder of
an A employee whose only tie to B is a pre-move membership. That is the intended rule
there too (the gate's docstring already says "a footprint (a membership, topic or
recording)", meaning a roster membership). Named in the PR so the org-api reviewer sees
it.

## 4. Tests

The proof is integration (fake conns never execute SQL). Local Postgres via
`C:\Users\camil\fswork\run-tests.sh`, one pytest at a time, Python
`C:\Users\camil\fspy\Scripts\python.exe`. New tests in
`tests/integration/test_ask_tools_people_sql.py`, on its `w` fixture (companies A, B;
sites SA, SB):

1. **Flagged external member, membership only** -> in `author_candidates_in_sites(SA)`,
   `author_footprint_in_sites` true, `find_visible_people` finds them, `person_in_reach`
   true. (`memberships.add_external_membership(db, b_user, SA, "worker")`.)
2. **Unflagged cross-company row** (direct `INSERT INTO memberships (user_id, site_id,
   role)` of a B user on SA) -> not a candidate, footprint false, `find_visible_people`
   `[]`, `person_in_reach` false.
3. **Site move**: A employee with only a membership on SA, plus A employee with a topic on
   SA; `sites.move_site_company(SA, A -> B)`. The membership-only one is no longer a
   candidate / in reach for SA; the one with a topic still is (other arm).
4. **Same-company member** (existing `test_candidates_include_live_membership_only_people`)
   still passes: the predicate keeps employees.
5. **NULL-company user** with a live membership on SA -> excluded (matches the roster).
6. **The two forms agree**: for each user in tests 1-5, `uid in candidates ==
   footprint(uid)`.
7. **Red check**: revert the predicate in either function and tests 2, 3 and 5 fail
   (run once, record in the PR).

Org-api regression: run `tests/integration/test_project_owned_reads.py` and
`test_project_owned_final_review.py` unchanged (Eve, the flagged external worker, must
still be openable by B's admin). Add one integration case there: after a site move, B's
admin `_can_view_folder` on a membership-only A employee is false.

Unit (fake conn): none added. A fake conn would only pin SQL text, which the integration
tests above prove by execution; the existing `tests/unit/test_rag_tools_dispatch.py`
must still pass unchanged (dispatch code does not change).

## 5. Out of scope, and decisions for the owner

- **Should a site move clean up memberships at the writer?** `move_site_company` leaves
  the old company's employees as unflagged rows. This design stops the people footprint
  from honouring them; it does not decide whether the move should archive them or flag
  them external. Org-api line.
- **Same gap in the scope inputs.** `caller_site_roles`, `user_ids_for_sites` and
  `worker_user_ids_for_sites` (`memberships.py:229-271`) also ignore the flag, and feed
  `visible_scope`. After a move, an A pm with an old membership on S plausibly keeps reach
  on S (now B's), and a stale A worker row lands in a B site_manager's `author_ids`. Not
  verified end to end here; visibility line's call.
- **Archived-site history in Ask.** Nobody's reach contains an archived site, so Ask
  cannot answer about a closed project even for its own company's admin, while
  `/timeline` still shows those rows per person. Opening that is a `visible_scope` /
  visibility-spec change; a name lookup must then resolve exactly the archived sites that
  enter reach, and no others.

## 6. Rollout

develop -> TEST only. No template change, no migration, no flag. Before merging, run one
read-only count on TEST (and hand the same SQL to the owner for prod) to learn whether the
case has ever occurred:

    SELECT count(*) FROM memberships m
      JOIN users u ON u.id = m.user_id JOIN sites s ON s.id = m.site_id
     WHERE m.archived_at IS NULL AND NOT m.external
       AND u.company_id IS DISTINCT FROM s.company_id;

Zero means the change is preventive only. Non-zero lists the people whose `find_people` /
folder-gate reach shrinks on deploy; check each is a move orphan, not a writer bug.
