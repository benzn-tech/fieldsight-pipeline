"""The one place that turns an S3 lake key into an owner (company, site, user).

The rule order below is the whole contract; the first rule that yields a company wins and
nothing is ever guessed. A wrong owner here means one company's data is exported to, or listed
for, another, so every rule that cannot be certain returns NULL and says why (`reason`).

Rule order (see Resolver._one):
  0. scope: `global` keys and `lake_wide` keys are not tenant objects -> reason "global" /
     "lake_wide", no company, ever (R5b).
  1. recordings.s3_key == key             -> that row's user/site, the SITE's current company
                                             (else the row's company)                 "recording"
  2. the key names a site or a company     -> site_id: that site's company; site_slug: the one
     live site with that slug (R5c, else NULL). voiceprint_requests are NOT here: the company
     in their key is the CALLER's, so they are owned by their session (rule 3) or NULL    "key"
  3. a session id (`sid`) in the key       -> meeting_session: the site's company, else the
                                             recorder's company                       "session"
  3b. a group id                           -> the group's site company when EVERY member has a
     site and they agree (R5d); otherwise NULL "unresolved". It never falls through to the
     folder rule: a group can span recorders from two home companies.
  4. a folder (users.folder_name is globally unique) and a day:
       recordings of that (folder, day) on sites of ONE company  -> that company; the site too
                                      when there is exactly one site and no siteless recording
                                                                               "folder_day_site"
       on sites of MORE than one company (a siteless recording counts as the recorder's home
       company)                       -> company NULL, user kept            "split_day" (ruling C2)
       no recordings that day, or no day in the key -> the recorder's home company "folder_home",
       EXCEPT for a user who has ever been an external member (R5g; any memberships row with
       external = true, archived included) (a key with no day and no session, or a day with no recordings) -> company NULL,
       reason "external_member_uncertain" 
       unknown folder                 -> NULL                                 "unresolved"
  5. the manifest hint, last and only for fields still NULL (R5a). A company hint is never
     trusted over the DB and is used only when the key carries no folder, no session and no
     group; a site hint is accepted only when that site exists (its company then comes from
     the DB). Everything else about a hint (folder, session_base) is descriptive.   "hint"
  6. otherwise company NULL, reason "unresolved".

Notes: a company hint (rule 5) only ever fills keys that carry no folder/session/group (site_report,
voice, match, weather, programme with an unresolvable site); and a dry run reports the rows that
WOULD be written, not net of unchanged ones.

Keys of an unknown shape (lake_keys.parse -> None) are never resolved by 2-5: only rule 1 can
own them; otherwise NULL with scope "unknown" and kind = the first path segment (R5f).
"""
import logging
import uuid

import lake_keys

logger = logging.getLogger(__name__)

REASONS = ("recording", "session", "folder_day_site", "folder_home", "key", "hint",
           "unresolved", "split_day", "external_member_uncertain", "global", "lake_wide")
# Never produced by the resolver: written only by an operator script to pin an owner decision
# (repositories.lake_objects skips every upsert onto a row carrying it).
OPERATOR_REASON = "operator"


def _s(v):
    return None if v is None else str(v)


def _uuid(v):
    """Canonical lowercase uuid string, or None for anything that is not one."""
    if v is None or isinstance(v, bool):
        return None
    try:
        return str(uuid.UUID(str(v)))
    except (ValueError, AttributeError, TypeError):
        return None


def _clean_hint(hint):
    if not isinstance(hint, dict):
        return {}
    return {k: v for k, v in hint.items() if isinstance(k, str) and v}


def _parse(key):
    try:
        return lake_keys.parse(key)
    except Exception:  # noqa: BLE001 - one odd key must not stop the run
        logger.warning("lake_owner: parse failed for %r", key, exc_info=True)
        return None


class DbLookups:
    """The seven batched lookups the resolver needs. Tests substitute a fake with the same
    methods; each takes a collection and returns a dict (see the repository docstrings)."""

    def __init__(self, conn):
        self.conn = conn

    def recordings(self, keys):
        from repositories import recordings
        return recordings.owners_for_keys(self.conn, keys)

    def sessions(self, sids):
        from repositories import meeting_session
        return meeting_session.owners_for_sids(self.conn, sids)

    def groups(self, gids):
        from repositories import meeting_session
        return meeting_session.members_for_groups(self.conn, gids)

    def homes(self, folders):
        from repositories import users
        return users.homes_for_folders(self.conn, folders)

    def folder_days(self, pairs):
        from repositories import recordings
        return recordings.site_companies_for_folder_days(self.conn, pairs)

    def externals(self, user_ids):
        from repositories import memberships
        return memberships.ever_external_user_ids(self.conn, user_ids)

    def site_companies(self, site_ids):
        from repositories import sites
        return sites.companies_for_ids(self.conn, site_ids)

    def slugs(self, slugs):
        from repositories import sites
        return sites.live_by_slugs(self.conn, slugs)


class Resolver:
    """Resolver(conn).resolve_many([(key, hint), ...]) -> {key: Owner}.

    Owner = {"company_id", "site_id", "user_id", "kind", "folder", "day", "session_base",
             "reason", "scope"}; ids are strings or None. Lookups are batched per call and
    cached for the life of the instance (one drain run)."""

    def __init__(self, conn, lookups=None):
        self.lookups = lookups or DbLookups(conn)
        self._c = {name: {} for name in
                   ("rec", "sid", "grp", "home", "fday", "site", "slug", "ext")}

    # ---- batched, cached lookups -------------------------------------------------------
    def _fetch(self, name, wanted, fn):
        cache = self._c[name]
        missing = [w for w in wanted if w not in cache]
        if missing:
            res = fn(missing) or {}
            for w in missing:
                cache[w] = res.get(w)

    def _prefetch(self, entries):
        live = [(k, h, p) for k, h, p in entries if p is None or p["scope"] == "tenant"]
        self._fetch("rec", sorted({k for k, _h, _p in live}), self.lookups.recordings)
        self._fetch("sid", sorted({p["sid"] for _k, _h, p in live if p and p["sid"]}),
                    self.lookups.sessions)
        self._fetch("grp", sorted({p["group_id"] for _k, _h, p in live if p and p["group_id"]}),
                    self.lookups.groups)
        self._fetch("home", sorted({p["folder"] for _k, _h, p in live if p and p["folder"]}),
                    self.lookups.homes)
        self._fetch("fday", sorted({(p["folder"], p["day"]) for _k, _h, p in live
                                    if p and p["folder"] and p["day"]}), self.lookups.folder_days)
        # R5g: who has ever been an external member (batched; only users whose home company
        # could be used as a fallback matter, and those are exactly the resolved folders)
        uids = sorted({str(self._c["home"][p["folder"]]["id"]) for _k, _h, p in live
                       if p and p["folder"] and self._c["home"].get(p["folder"])})
        self._fetch("ext", uids, lambda ids: {i: True for i in self.lookups.externals(ids)})
        site_ids = set()
        for _k, h, p in live:
            if p and p["site_id"]:
                site_ids.add(_uuid(p["site_id"]))
            site_ids.add(_uuid(h.get("site_id")))
        self._fetch("site", sorted(s for s in site_ids if s), self.lookups.site_companies)
        self._fetch("slug", sorted({p["site_slug"] for _k, _h, p in live if p and p["site_slug"]}),
                    self.lookups.slugs)

    # ---- the rules ---------------------------------------------------------------------
    def resolve_many(self, items):
        entries = [(k, _clean_hint(h), _parse(k)) for k, h in items]
        self._prefetch(entries)
        return {k: self._one(k, h, p) for k, h, p in entries}

    def resolve(self, key, hint=None):
        return self.resolve_many([(key, hint)])[key]

    def _one(self, key, hint, p):
        o = {"company_id": None, "site_id": None, "user_id": None,
             "kind": (p["kind"] if p else (key.split("/", 1)[0] if isinstance(key, str) else "")),
             "folder": p["folder"] if p else None, "day": p["day"] if p else None,
             "session_base": p["session_base"] if p else None, "reason": None,
             "scope": p["scope"] if p else "unknown"}
        if p is not None and p["scope"] in ("global", "lake_wide"):
            o["reason"] = p["scope"]
            return o

        rec = self._c["rec"].get(key)  # rule 1
        if rec is not None:
            o.update(company_id=_s(rec["company_id"]), site_id=_s(rec["site_id"]),
                     user_id=_s(rec["user_id"]), reason="recording")
        elif p is None:  # R5f: an unknown shape has only rule 1
            o["reason"] = "unresolved"
            return o
        else:
            self._by_key(o, p) or self._by_session(o, p) or self._by_group(o, p) \
                or self._by_folder(o, p)
        if o["company_id"] is None and o["reason"] is None:
            o["reason"] = "unresolved"
        self._apply_hint(o, p, hint)
        return o

    def _by_key(self, o, p):  # rule 2
        company = site = None
        if p["site_id"]:
            site = _uuid(p["site_id"])
            company = self._c["site"].get(site) if site else None
        elif p["site_slug"]:
            found = self._c["slug"].get(p["site_slug"]) or []
            if len(found) == 1:  # R5c: slugs are unique per company only
                site, company = _s(found[0]["id"]), found[0]["company_id"]
        elif p["kind"] in ("company_summary", "action_pack_web") and p["company_id"]:
            company = _uuid(p["company_id"])
        if company is None:
            return False
        o.update(company_id=_s(company), site_id=site,
                 reason="key")
        return True

    def _by_session(self, o, p):  # rule 3
        row = self._c["sid"].get(p["sid"]) if p["sid"] else None
        if row is None or row["company_id"] is None:
            return False
        o.update(company_id=_s(row["company_id"]), site_id=_s(row["site_id"]),
                 user_id=_s(row["user_id"]), reason="session")
        return True

    def _by_group(self, o, p):  # rule 3b (R5d); True also when uncertain: the rule is final
        if not p["group_id"]:
            return False
        members = self._c["grp"].get(p["group_id"]) or []
        companies = {_s(m["site_company_id"]) for m in members}
        certain = bool(members) and None not in companies and len(companies) == 1
        if certain:
            sites_ = {_s(m["site_id"]) for m in members}
            o.update(company_id=next(iter(companies)),
                     site_id=next(iter(sites_)) if len(sites_) == 1 else None, reason="session")
        else:
            o["reason"] = "unresolved"
        return True

    def _by_folder(self, o, p):  # rule 4 and ruling C2
        if not p["folder"]:
            return False
        home = self._c["home"].get(p["folder"])
        if home is None:
            return False  # unknown folder: never a default company
        o["user_id"] = _s(home["id"])
        home_company = _s(home["company_id"])
        day = self._c["fday"].get((p["folder"], p["day"])) if p["day"] else None
        if not day or (not day["sites"] and not day["siteless"]):
            if home_company is None:
                return False
            if self._c["ext"].get(_s(home["id"])) and (
                    p["day"] or not (p["sid"] or p["session_base"])):
                # R5g: the home company is a certain fallback only for a user who has NEVER
                # held an external membership; this key says nothing about which company's
                # site the work was on.
                o["reason"] = "external_member_uncertain"
                return True
            o.update(company_id=home_company, reason="folder_home")
            return True
        companies = {_s(c) for c in day["sites"].values()}
        # R1: a siteless recording belongs to the recorder's home company, so a day mixing it
        # with another company's site is a split day and its whole-day documents stay NULL.
        if day["siteless"]:
            companies.add(home_company)
        if None in companies or len(companies) != 1:
            # C2: a whole-day document of a day spanning companies belongs to nobody. A site
            # whose company is unknown (None) is uncertain for the same reason.
            o["reason"] = "split_day" if len(companies - {None}) > 1 else "unresolved"
            return True
        site_ids = {_s(s) for s in day["sites"]}
        o.update(company_id=next(iter(companies)),
                 site_id=next(iter(site_ids)) if len(site_ids) == 1 and not day["siteless"] else None,
                 reason="folder_day_site" if day["sites"] else "folder_home")
        return True

    def _apply_hint(self, o, p, hint):  # rule 5 (R5a)
        if not hint:
            return
        hint_site = _uuid(hint.get("site_id"))
        hint_site_company = _s(self._c["site"].get(hint_site)) if hint_site else None
        carries = bool(p and (p["folder"] or p["sid"] or p["session_base"] or p["group_id"]))
        cands = (self._c["slug"].get(p["site_slug"]) or []) if p and p["site_slug"] else []
        if o["company_id"] is None:
            hc = _uuid(hint.get("company_id"))
            if o["reason"] == "split_day" or carries:
                pass  # a hint never decides ownership of a folder/session/group key
            elif p and p["kind"] == "voice":
                pass  # A8: the company in a voice/ key is the caller's; a hint is no better
            elif len(cands) > 1:
                # F2: an ambiguous slug may only be settled by a hint naming one of the
                # candidate sites (or their companies); anything else stays NULL.
                by_id = {_s(c["id"]): _s(c["company_id"]) for c in cands}
                if hint_site in by_id:
                    o.update(company_id=by_id[hint_site], site_id=hint_site, reason="hint")
                elif hc and hc in set(by_id.values()):
                    mine = [i for i, c in by_id.items() if c == hc]
                    o.update(company_id=hc, site_id=mine[0] if len(mine) == 1 else None,
                             reason="hint")
            elif hint_site_company:
                o.update(company_id=hint_site_company, site_id=hint_site, reason="hint")
            elif hc:
                o.update(company_id=hc, reason="hint")
        else:
            if (o["site_id"] is None and hint_site_company
                    and hint_site_company == o["company_id"]):
                o["site_id"] = hint_site
        if o["folder"] is None and isinstance(hint.get("folder"), str):
            o["folder"] = hint["folder"]
        if o["session_base"] is None and isinstance(hint.get("session_base"), str):
            o["session_base"] = hint["session_base"]
