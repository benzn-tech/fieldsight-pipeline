from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from repositories import sites
from repositories.topics import _escape_like

_COLS = ("id, company_id, user_id, site_id, kind, s3_key, client_uuid, started_at, "
         "ended_at, duration_s, resolution, codec, size_bytes, gps_track, uploaded_at, created_at")


def insert_pending(conn, company_id, user_id, site_id, kind, s3_key, client_uuid,
                   started_at, ended_at=None, duration_s=None, resolution=None,
                   codec=None, size_bytes=None) -> dict:
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO recordings (company_id, user_id, site_id, kind, s3_key, client_uuid, "
        f"started_at, ended_at, duration_s, resolution, codec, size_bytes) "
        f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_COLS}",
        (company_id, user_id, site_id, kind, s3_key, client_uuid,
         started_at, ended_at, duration_s, resolution, codec, size_bytes),
    ).fetchone()


def get_by_client_uuid(conn, user_id, client_uuid) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM recordings WHERE user_id=%s AND client_uuid=%s",
        (user_id, client_uuid),
    ).fetchone()


def get_by_id(conn, rec_id) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM recordings WHERE id=%s", (rec_id,)
    ).fetchone()


def mark_uploaded(conn, rec_id, company_id, size_bytes=None, gps_track=None) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE recordings SET uploaded_at=now(), "
        f"size_bytes=COALESCE(%s, size_bytes), "
        f"gps_track=COALESCE(%s, gps_track) "
        f"WHERE id=%s AND company_id=%s RETURNING {_COLS}",
        (size_bytes, Jsonb(gps_track) if gps_track is not None else None, rec_id, company_id),
    ).fetchone()


def duration_for_media(conn, company_id, user_folder, date, session_base) -> float | None:
    """Recorded DURATION in seconds for the media file an extraction session
    came from, or None when there is no matching recordings row. Same
    session_base LIKE match + company scoping as site_for_media below (kept as
    its own query so the two callers stay independent).

    Deliberately returns a duration, NOT an absolute end instant:
    recordings.started_at/ended_at are timestamptz (UTC), while a session's
    start is the NZ device wall clock encoded in session_base. Mixing the two
    would label a 13:05 meeting as ending at 01:22 (BUG-37's family). The
    caller adds this duration to the session_base start, so everything stays
    on ONE clock. Prefers the explicitly reported duration_s; falls back to
    the ended_at - started_at delta (a difference of two timestamptz values is
    timezone-safe). Non-positive/degenerate values are treated as absent."""
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT duration_s, started_at, ended_at FROM recordings "
        "WHERE company_id = %s AND s3_key LIKE %s ESCAPE '\\' "
        "ORDER BY created_at DESC LIMIT 1",
        (company_id, f"users/{_escape_like(user_folder)}/%/{date}/{_escape_like(session_base)}.%"),
    ).fetchone()
    if row is None:
        return None
    if row.get("duration_s") is not None and float(row["duration_s"]) > 0:
        return float(row["duration_s"])
    started, ended = row.get("started_at"), row.get("ended_at")
    if started is not None and ended is not None:
        delta = (ended - started).total_seconds()
        if delta > 0:
            return delta
    return None


def day_stats(conn, company_id, user_folder, date) -> dict:
    """Recording counts for ONE (user_folder, date), for the timeline KPI strip.

    Returns {"sessions": int, "duration_s": int}. Never None — a day with no
    recordings is an honest zero, not a missing metric.

    Two things this deliberately does NOT do:

    1. It does not count `recordings` ROWS. Under the chunk-session contract
       one recording session arrives as N ~30s chunks, each its own row
       (`..._sid{32hex}_c{NNNN}.wav`), so a single 9-minute meeting is 21 rows.
       Reporting 21 would tell the user they made 21 recordings. Rows are
       therefore folded to the session id parsed out of the key, with the key
       itself as the fold value when there is no sid — pre-chunk-session
       recordings are one row each, so the fold is the identity for them and
       the count is unchanged for legacy data.
    2. It does not filter on started_at. That column is timestamptz (UTC) while
       `date` here is the device's NZ local day, the same clock the extraction
       topics and the s3_key are on — filtering by UTC would move an evening
       recording to the next day (the BUG-37/finalize-timezone family). The
       s3_key path segment is the one date that agrees with the rest of the
       timeline, and it is the same match duration_for_media/site_for_media use.

    Only 'audio' and 'video' count: the KPI reads "Recordings" (capture
    sessions), and photos have their own surface on the Evidence page.
    company_id scopes the read — the multi-tenant invariant is that a folder
    name never reaches across tenants."""
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT COUNT(DISTINCT COALESCE("
        "  substring(s3_key from '_(sid[0-9a-f]{32})_c[0-9]+\\.'), s3_key"
        ")) AS sessions, "
        "COALESCE(SUM(duration_s), 0) AS duration_s "
        "FROM recordings "
        "WHERE company_id = %s AND kind IN ('audio','video') "
        "AND s3_key LIKE %s ESCAPE '\\'",
        (company_id, f"users/{_escape_like(user_folder)}/%/{date}/%"),
    ).fetchone()
    if row is None:
        return {"sessions": 0, "duration_s": 0}
    return {"sessions": int(row["sessions"] or 0),
            "duration_s": int(row["duration_s"] or 0)}


def range_stats(conn, company_id, date_from, date_to,
                site_ids, author_ids=None, deleted_bases=()) -> dict:
    """Sessions, seconds and photos over a date RANGE, in the ACL's own currency.

    Scoped by `site_ids` and `author_ids` -- the two sets `scope.visible_scope`
    returns -- and NOT by folder name. A folder name arriving in a request is an
    ACL bypass wearing a parameter: the caller names whose recordings to count.
    `recordings.user_id` is NOT NULL (0 of 3127 live) and is the same currency as
    `author_ids`, so there is nothing folder names buy here.

    `day_stats`'s two rules widened, plus two it does not have.

    1. SESSIONS, NOT ROWS. Folded on the sid parsed out of the key, with the key
       itself as the fold value when there is no sid -- so pre-chunk-session
       recordings are one row each and the fold is the identity for them.
       Measured live: one folder on one day is 263 rows and 5 sessions. Counting
       rows would tell a person they made fifty times the recordings they made.

    2. THE KEY SEGMENT, NOT `started_at`. That column is timestamptz (UTC) while
       this range is the caller's local calendar day, which is what
       `query_slots.time_range` produces and the clock the extraction topics and
       the s3_key are already on. Filtering by UTC moves an evening recording to
       the next day -- the BUG-37/finalize-timezone family.

    3. THE SPAN FALLBACK, which `day_stats` lacks. It sums `duration_s` only,
       which covers 97.9% of sessions; `ended_at - started_at` --
       `duration_for_media`'s fallback -- takes it to 99.7%. Without it here, 5
       sessions in 287 contribute zero and the total is quietly short.
       `unmeasured` counts the sessions that can produce neither, so a short
       total is visible rather than assumed.

    4. `unattributed` NAMES WHAT THE SITE FILTER COST. `recordings.site_id` is
       nullable and 87 of 3127 rows live have none, so a site-scoped count drops
       2.8% of the corpus. Dropping them is right -- a row that belongs to no
       site cannot be shown to someone whose reach IS a set of sites, and
       widening the filter when the set is empty is the "empty list means no
       filter" bug this repo has already shipped once. Dropping them SILENTLY is
       not: `unattributed` is how many sessions the caller's author scope would
       have allowed but the site filter excluded, so a number that is short
       arrives with the reason attached.

    Photos are counted separately and never join the fold: they are rows in this
    table with `kind='photo'` -- 304 of the 3127 -- and mixing them into a
    session count is how the fold ratio was first miscomputed.

    `deleted_bases` EXCLUDES SID-KEYED AUDIO AND VIDEO SESSIONS ONLY. A row with
    no session id in its key -- a pre-chunk-session recording, or any photo --
    has the whole key as its fold, which no `sid{hex}` base can equal, so it
    cannot be excluded here and is not claimed to be. The tombstones name
    `extractions/{folder}/{date}/sid{hex}` and there is nothing in a legacy key
    or a photo key to match it against; closing that belongs where the tombstone
    is written, not here.

    Otherwise: session bases the CALLER has already resolved, in either
    spelling. They cannot be resolved here: the
    tombstones live in the `extractions/` key space, `recordings` has no
    `source_s3_key` column at all, and the predicate every other reader uses
    therefore matches nothing against this table. The translation happens in the
    caller and the exclusion happens here -- and a count that includes deleted
    recordings is a way to observe what was deleted.

    An empty `site_ids` yields `= ANY('{}')`, which matches no rows. That is the
    correct deny-by-default: skipping the filter would count the whole company.
    `author_ids=None` means no author filter, which is what `visible_scope`
    returns for an ALL- or SITE-scoped caller; an empty SET is still a filter
    that matches nobody.
    """
    bases = {b for b in (deleted_bases or ()) if b}
    bases |= {b[3:] for b in list(bases) if b.startswith("sid")}
    bases |= {"sid" + b for b in list(bases) if not b.startswith("sid")}

    row = conn.cursor(row_factory=dict_row).execute(
        "WITH windowed AS ("
        "  SELECT kind, duration_s, started_at, ended_at, site_id,"
        "    COALESCE(substring(s3_key from '_(sid[0-9a-f]{32})_c[0-9]+\\.'), s3_key) AS fold"
        "  FROM recordings"
        # `company_id=None` MEANS NO COMPANY RESTRICTION, and only the ACL
        # primitive may ask for it -- `visible_scope` sets `cross_company` for
        # platform_admin, and `_metric` passes None only then.
        #
        # Everyone else keeps the pin, which is belt-and-braces over a site set
        # that is already theirs: for an ordinary role `site_ids` comes from
        # memberships or list_company_sites and cannot span companies. Removing
        # it outright would make this function trust whatever site list it is
        # handed, and a test written for exactly that reason went red when I
        # tried.
        #
        # But the pin cannot apply to a cross-company caller, because the sites
        # they reach belong to OTHER companies: `company_id = <own> AND site_id =
        # ANY(<their sites>)` matched nothing, and a platform_admin reaching 5
        # sites that recorded all day was told "no recording data was registered
        # for it". `has_topics_in_range` scopes by site alone, which is why the
        # topic half of that sentence was right while the count was zero.
        "  WHERE (site_id = ANY(%(sites)s::uuid[])"
        "         OR (site_id IS NULL"
        "             AND (%(company)s::uuid IS NULL"
        "                  OR company_id = %(company)s)))"
        "    AND (%(company)s::uuid IS NULL OR company_id = %(company)s)"
        "    AND substring(s3_key from '/([0-9]{4}-[0-9]{2}-[0-9]{2})/')"
        "        BETWEEN %(from)s AND %(to)s"
        "    AND (%(authors)s::uuid[] IS NULL OR user_id = ANY(%(authors)s::uuid[]))"
        "), sess AS ("
        "  SELECT fold,"
        # ONE session, and the site test is per ROW, not per session. `in_scope`
        # asks whether any of this session's rows are on a site the caller can
        # reach; the sums below then count ONLY those rows. Summing the whole
        # fold once one row qualified would report seconds recorded on a site the
        # ACL hides everywhere else in the product -- no session spans two sites
        # in either live database today, but multi-device merge groups by session
        # id, which is exactly how one would arrive.
        "    bool_or(site_id = ANY(%(sites)s::uuid[])) AS in_scope,"
        "    bool_or(site_id IS NULL) AS no_site,"
        "    COALESCE(SUM(duration_s) FILTER"
        "      (WHERE site_id = ANY(%(sites)s::uuid[])), 0) AS dur,"
        # THE SPAN OF THE SESSION, not the longest chunk in it. `MAX(ended_at -
        # started_at)` is the span of one ~30s chunk, so a nine-minute session
        # whose rows carry no `duration_s` reported 30 seconds and `unmeasured`
        # stayed 0 -- a total short by 94% with nothing flagging it. Only the
        # one-row legacy case was covered by a test, where the two are equal.
        "    EXTRACT(EPOCH FROM ("
        "      MAX(ended_at) FILTER (WHERE site_id = ANY(%(sites)s::uuid[]))"
        "      - MIN(started_at) FILTER (WHERE site_id = ANY(%(sites)s::uuid[]))"
        "    )) AS span"
        "  FROM windowed"
        "  WHERE kind IN ('audio','video') AND NOT (fold = ANY(%(deleted)s))"
        "  GROUP BY fold"
        ")"
        "SELECT"
        "  (SELECT count(*) FROM sess WHERE in_scope) AS sessions,"
        "  (SELECT COALESCE(SUM(CASE WHEN dur > 0 THEN dur"
        "                            WHEN span > 0 THEN span"
        "                            ELSE 0 END), 0) FROM sess WHERE in_scope) AS duration_s,"
        "  (SELECT count(*) FROM sess WHERE in_scope"
        "     AND COALESCE(dur, 0) <= 0 AND COALESCE(span, 0) <= 0) AS unmeasured,"
        "  (SELECT count(*) FROM sess WHERE NOT COALESCE(in_scope, false)"
        "     AND COALESCE(no_site, false)) AS unattributed,"
        # NO DELETION FILTER HERE, AND IT IS NOT AN OVERSIGHT. A photo key
        # carries no session id, so its `fold` is the whole key and can never
        # equal a `sid{hex}` base -- the filter that used to sit here read as
        # protection and excluded nothing, ever. Linking a photo to a deleted
        # session is not possible from these keys at all: the tombstone names
        # `extractions/{folder}/{date}/sid{hex}` and the photo is
        # `users/{folder}/pictures/{date}/IMG_x.jpg`, sharing only a folder and
        # a day. A documented gap beats a no-op that looks like a guard.
        "  (SELECT count(*) FROM windowed"
        "    WHERE kind = 'photo' AND site_id = ANY(%(sites)s::uuid[]))"
        "    AS photos",
        {"company": company_id, "from": date_from, "to": date_to,
         "sites": [str(s) for s in site_ids],
         "authors": [str(a) for a in author_ids] if author_ids is not None else None,
         "deleted": list(bases)},
    ).fetchone()
    if row is None:
        return {"sessions": 0, "duration_s": 0, "unmeasured": 0,
                "unattributed": 0, "photos": 0}
    # SUM() over bigint is numeric, which psycopg hands back as Decimal, and
    # json.dumps has no encoder for Decimal. These go into an HTTP body.
    return {k: int(row[k] or 0) for k in ("sessions", "duration_s", "unmeasured",
                                          "unattributed", "photos")}


def site_for_media(conn, company_id, user_folder, date, session_base) -> dict | None:
    """The app-tagged site (recordings.site_id) for the recording whose media
    file this extraction session came from, or None. Matches recordings.s3_key
    by session_base within users/{folder}/.../{date}/ (LIKE, wildcard-escaped),
    scoped to company_id, and only returns a site that is itself in-company
    (multi-tenant invariant — never attribute across tenants). Newest matching
    recording wins. Returns a sites.get_site()-shaped row so it drops in where
    resolve_site's return is used (lambda_item_writer)."""
    pattern = f"users/{_escape_like(user_folder)}/%/{date}/{_escape_like(session_base)}.%"
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT r.site_id FROM recordings r JOIN sites s ON s.id = r.site_id "
        "WHERE r.company_id = %s AND s.company_id = %s AND r.site_id IS NOT NULL "
        "AND r.s3_key LIKE %s ESCAPE '\\' "
        "ORDER BY r.created_at DESC LIMIT 1",
        (company_id, company_id, pattern),
    ).fetchone()
    if row is None:
        return None
    return sites.get_site(conn, row["site_id"])


def site_for_day(conn, company_id, user_folder, date) -> dict | None:
    """The app-tagged site (recordings.site_id) for a user's WHOLE day, or
    None. Report-level sibling of site_for_media above: same LIKE match on
    users/{folder}/.../{date}/ (wildcard-escaped), same company double-scope
    via the sites join (multi-tenant invariant -- never attribute across
    tenants), same r.site_id IS NOT NULL filter, same sites.get_site()-shaped
    return so it drops into the same slot as resolve_site.

    Unlike site_for_media there is no session_base to pin a single
    recording, and a day's recordings can in principle span more than one
    site. Ambiguity rule: a daily report is inherently attributed to one
    site, so pick the site with the MOST recordings that day (majority
    signal), tie-breaking by the most recent created_at. This is strictly
    better than the caller's env-var default (SITE_NAME) fallback."""
    pattern = f"users/{_escape_like(user_folder)}/%/{date}/%"
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT r.site_id, COUNT(*) AS cnt, MAX(r.created_at) AS latest "
        "FROM recordings r JOIN sites s ON s.id = r.site_id "
        "WHERE r.company_id = %s AND s.company_id = %s AND r.site_id IS NOT NULL "
        "AND r.s3_key LIKE %s ESCAPE '\\' "
        "GROUP BY r.site_id "
        "ORDER BY cnt DESC, latest DESC LIMIT 1",
        (company_id, company_id, pattern),
    ).fetchone()
    if row is None:
        return None
    return sites.get_site(conn, row["site_id"])
