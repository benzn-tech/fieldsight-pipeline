"""visible_scope -- the single ACL primitive every read path scopes through
(visibility spec §3.1/§3.2). Wraps the existing binary scoping (acl.resolve_
scope + sites.list_company_sites) with graded within-project authority (D1)
and the platform_admin cross-company branch (D6). Import graph is acyclic:
acl (pure) <- memberships <- scope; sites/users import no repos, so importing
them here is safe.

PERFORMANCE (plan Global Constraints #1, binding): for the graded non-ALL,
non-cross-company branch (regional_manager / pm / site_manager / worker),
site_ids AND per-site roles come from the SINGLE memberships.caller_site_roles
result -- site_ids = set(roles_map.keys()). This module deliberately does NOT
call memberships.accessible_site_ids (that would be a redundant second
round-trip over the same memberships table/index the new query already reads).
The only additional query is worker_user_ids_for_sites, and ONLY on the
SELF+WORKERS (site_manager) branch. platform_admin's list_all_sites REPLACES
(never adds to) the membership query, same for admin/gm's list_company_sites.
Net: +0 queries for pm/regional/worker vs the legacy accessible_site_ids call,
+1 for site_manager."""
from repositories import acl, memberships, sites


def visible_scope(conn, caller) -> dict:
    """Resolve the caller's visibility envelope. All rows below platform_admin
    are hard-pinned to caller.company_id (§3.0). Returns:
      site_ids     : set[str]  -- sites the caller may see at all (reach)
      user_scope   : str       -- ALL|SITE|SELF+WORKERS|SELF (within-project)
      author_ids   : set|None  -- resolved per-author allow-set; None = no filter
      self_folder  : str|None  -- caller's recording folder (own-data key)
      self_user_id : str       -- caller.id; own items are always visible
      company_id, cross_company.

    MEMOIZED (review MINOR-1): the envelope is cached on the `caller` dict
    under `_visible_scope` and reused on repeat calls within the same request
    (_allowed_site_ids + _author_filter + _can_view_folder each call this 2-3x
    per request). `caller` is resolved once per request in dispatch, so the
    cache is request-scoped -- no cross-request staleness. Single-query internal
    behavior is unchanged: the FIRST call issues the membership query(ies);
    every later call issues zero."""
    cached = caller.get("_visible_scope")
    if cached is not None:
        return cached
    global_role = caller["global_role"]
    cross_company = acl.is_cross_company(global_role)
    self_user_id = str(caller["id"])
    external_pm = set()

    if cross_company:
        # D6: the SOLE branch NOT pinned to caller.company_id. Zero membership
        # queries -- list_all_sites replaces, not adds to, caller_site_roles.
        site_ids = {str(s["id"]) for s in sites.list_all_sites(conn)}
        site_roles = {}
    elif acl.resolve_scope(global_role) == "ALL":
        # admin/gm: company-wide reach, zero membership queries.
        site_ids = {str(s["id"]) for s in sites.list_company_sites(conn, caller["company_id"])}
        site_roles = {}
        # An admin/gm added to ANOTHER company's project: the MEMBERSHIP role governs
        # reach on that one site (project-owned tenancy). A pm there sees every author
        # on that site, exactly what that site's own pm sees, and nothing on the
        # company's other sites. Home-company ALL reach is unchanged. The lower tiers
        # (site_manager/worker) are per-author restricted and cannot be expressed next
        # to an unfiltered home reach, so they add no site reach here: the person's
        # own work is read through the own-folder paths.
        external_pm = {sid for sid, role in memberships.external_site_roles(
            conn, caller["id"]).items() if role == "pm"}
        site_ids |= external_pm
    else:
        # regional_manager / pm / site_manager / worker. Final review F3: the author
        # tier is decided PER SITE. A site reached through an EXTERNAL membership
        # (a project of another company) has the tier of that membership's role and
        # nothing else; the home-company sites keep today's rule (global role +
        # home memberships) and nothing else. External roles never raise home reach
        # and home roles never raise external reach.
        all_roles = memberships.caller_site_roles(conn, caller["id"])
        ext_roles = memberships.external_site_roles(conn, caller["id"])
        home_roles = {sid: r for sid, r in all_roles.items() if sid not in ext_roles}
        site_ids = set(home_roles.keys())
        site_roles = home_roles
        home_tier = acl.visible_user_scope(global_role, home_roles.values())
        for sid, role in ext_roles.items():
            ext_tier = acl.external_user_scope(role)
            # The pair (site_ids, author_ids) every read path applies is ONE author
            # filter over ALL the sites, so an external site can join it only when
            # both tiers say exactly the same thing about authors: nobody filtered
            # (home SITE + external pm) or the caller's own rows (SELF + SELF). Any
            # other mix would hand one side the other's reach, so the external site
            # adds no reach there (fail closed: the person's own work on it is read
            # through the own-folder paths).
            if (home_tier == "SITE" and ext_tier == "SITE") or (
                    home_tier == "SELF" and ext_tier == "SELF"):
                site_ids.add(sid)

    user_scope = acl.visible_user_scope(global_role, site_roles.values())

    if user_scope in ("ALL", "SITE"):
        author_ids = None                                  # no per-author filter
    elif user_scope == "SELF+WORKERS":
        # the ONE extra query in this whole primitive, and only here.
        author_ids = {self_user_id} | memberships.worker_user_ids_for_sites(conn, site_ids)
    else:                                                  # SELF
        author_ids = {self_user_id}

    result = {
        "site_ids": site_ids,
        "user_scope": user_scope,
        "author_ids": author_ids,
        "self_folder": caller.get("folder_name"),
        "self_user_id": self_user_id,
        "company_id": caller["company_id"],
        "cross_company": cross_company,
        "external_pm_site_ids": external_pm,   # ALL-tier caller's pm sites of other companies
    }
    caller["_visible_scope"] = result
    return result
