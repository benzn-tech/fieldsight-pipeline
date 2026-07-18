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
      company_id, cross_company."""
    global_role = caller["global_role"]
    cross_company = acl.is_cross_company(global_role)
    self_user_id = str(caller["id"])

    if cross_company:
        # D6: the SOLE branch NOT pinned to caller.company_id. Zero membership
        # queries -- list_all_sites replaces, not adds to, caller_site_roles.
        site_ids = {str(s["id"]) for s in sites.list_all_sites(conn)}
        site_roles = {}
    elif acl.resolve_scope(global_role) == "ALL":
        # admin/gm: company-wide reach, zero membership queries.
        site_ids = {str(s["id"]) for s in sites.list_company_sites(conn, caller["company_id"])}
        site_roles = {}
    else:
        # regional_manager / pm / site_manager / worker: ONE membership query
        # gives both site_ids (the map's keys) and the per-site roles that
        # feed visible_user_scope -- no second accessible_site_ids round-trip.
        site_roles = memberships.caller_site_roles(conn, caller["id"])
        site_ids = set(site_roles.keys())

    user_scope = acl.visible_user_scope(global_role, site_roles.values())

    if user_scope in ("ALL", "SITE"):
        author_ids = None                                  # no per-author filter
    elif user_scope == "SELF+WORKERS":
        # the ONE extra query in this whole primitive, and only here.
        author_ids = {self_user_id} | memberships.worker_user_ids_for_sites(conn, site_ids)
    else:                                                  # SELF
        author_ids = {self_user_id}

    return {
        "site_ids": site_ids,
        "user_scope": user_scope,
        "author_ids": author_ids,
        "self_folder": caller.get("folder_name"),
        "self_user_id": self_user_id,
        "company_id": caller["company_id"],
        "cross_company": cross_company,
    }
