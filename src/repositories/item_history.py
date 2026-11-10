"""find_items: a site's items across sessions -- the shared, site-scoped item-history read.

Spec: docs/superpowers/specs/2026-11-08-track-c-item-history-and-evidence-chain-design.md
§5.2. Three consumers read this exact shape (status suggestions' reconcile step,
cross-day continuity, the nightly agent), so the returned keys and the order are a
contract, not a convenience.

What "an item" is here: one stable_id. Its TEXT, status, responsible, source key for
`exclude_source_keys` and `origin` come from the LIVE row (the one whose topic is neither
superseded nor deleted); its `first_seen` and `source` come from the EARLIEST row carrying
that stable_id on this site, superseded passes included -- re-extraction and carry-forward
move a stable_id onto a new row, and the item was first seen when the first of those rows
was written, not when the latest pass ran. Deleted rows (a topic tombstone or a recording
tombstone) never supply first_seen: a deleted pass must not be named as anyone's source.

Visibility comes from deleted_predicates and is never inlined: the live row carries
`visible_topics_predicate` (both deletion arms plus the live arm; CHILD_OF_VISIBLE_TOPIC
lacks the source-prefix arm, and a deletion read with one arm leaks the next night), the
first-seen row carries the two deletion arms without the live one.

`first_seen_recorded_at` is when the first-seen row's session STARTED recording, as an
aware UTC datetime; `first_seen` is only when that row was written. Python picks the rule
from lake_keys.parse(first_key), and SQL only fetches the candidate: a `sid{32hex}` base
reads meeting_session.opened_at (one LEFT JOIN on the session id the key names, no extra
round trip); a legacy base is the device's transcript stem, `{device}_{YYYY-MM-DD}_
{HH-MM-SS}` in NZ wall-clock time, converted through nz_time; a group extraction, any
other key, or a sid with no session row / NULL opened_at is None. A sid base carries no
wall clock, so a missing session row has nothing to fall back to.

Ranking is Python over the SQL candidates, with item_text_match.overlap -- the same
tokeniser the evidence chain uses, ASCII words plus CJK bigrams, so a Chinese item is
never scored zero for being Chinese.
"""
import datetime as _dt

from psycopg.rows import dict_row

import lake_keys
import nz_time
import transcript_utils
from deleted_predicates import (
    DELETED_SOURCE_PREDICATE,
    DELETED_TOPIC_PREDICATE,
    visible_topics_predicate,
)
from item_text_match import overlap
from repositories.event_links import BEST_LINK_ORDER, KIND_SRC_COLUMN, KIND_TABLE

KINDS = tuple(KIND_TABLE)

# kind -> (text column, responsible column, status column, deadline column). None = the kind
# has no such field. The table and the event_links FK column come from event_links
# (KIND_TABLE / KIND_SRC_COLUMN), the one mapping every Track C module reads.
_SHAPE = {
    "action_item": ("text", "responsible", "status", "deadline"),
    "finding": ("observation", "entity_name", "status", None),
    "decision": ("decision", "decided_by", None, None),
    "question": ("question", None, "status", None),
}

# A status filter is accepted only where status is a closed vocabulary a caller can name.
# Decisions have no status column; a finding's status is free text no reader agrees on.
_STATUS_FILTERABLE = ("action_item", "question")

_ORIGIN_KEYS = ("source_stem", "start_sec", "end_sec", "speaker_label", "quote_status")


def _as_date(value, name):
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        raise ValueError(f"{name} must be a date, not a datetime")
    if isinstance(value, _dt.date):
        return value
    try:
        return _dt.date.fromisoformat(str(value))
    except ValueError:
        raise ValueError(f"{name} must be an ISO date (YYYY-MM-DD), got {value!r}") from None


def _validate(company_id, site_id, kinds, statuses, since, until, limit):
    if not company_id:
        raise ValueError("company_id is required")
    if not site_id:
        raise ValueError("site_id is required")
    if isinstance(kinds, str) or not kinds:
        raise ValueError("kinds must be a non-empty collection of kind names")
    kinds = tuple(dict.fromkeys(kinds))
    unknown = [k for k in kinds if k not in KINDS]
    if unknown:
        raise ValueError(f"unknown kind(s) {unknown}; expected a subset of {KINDS}")
    statuses = dict(statuses or {})
    for kind, values in statuses.items():
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r} in statuses")
        if kind not in _STATUS_FILTERABLE:
            raise ValueError(f"a status filter on {kind!r} is not supported "
                             f"(only {_STATUS_FILTERABLE})")
        if isinstance(values, str) or not values:
            raise ValueError(f"statuses[{kind!r}] is empty; omit the kind for no filter")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError(f"limit must be a positive int, got {limit!r}")
    return kinds, statuses, _as_date(since, "since"), _as_date(until, "until")


def _kind_sql(kind, live_predicate, status_filtered):
    """One kind's candidates: live rows on the site (company-checked through sites), each
    with its earliest undeleted same-stable_id row and its best supported_by link."""
    table, link_col = KIND_TABLE[kind], KIND_SRC_COLUMN[kind]
    text_col, resp_col, status_col, deadline_col = _SHAPE[kind]
    resp = f"c.{resp_col}" if resp_col else "NULL::text"
    status = f"c.{status_col}" if status_col else "NULL::text"
    deadline = f"c.{deadline_col}" if deadline_col else "NULL::date"
    status_clause = f" AND c.{status_col} = ANY(%(statuses)s)" if status_filtered else ""
    return (
        f"SELECT c.id, c.stable_id, c.item_id, c.{text_col} AS text, {resp} AS responsible, "
        f"{deadline} AS deadline, {status} AS status, c.carried_from, c.created_at, "
        f"t.source_s3_key AS live_key, fs.first_seen, fs.first_key, fs.first_date, "
        f"o.dst_turn AS origin, ms.opened_at AS first_opened_at "
        f"FROM {table} c "
        f"JOIN topics t ON t.id = c.topic_id "
        f"JOIN sites s ON s.id = c.site_id "
        f"JOIN LATERAL (SELECT x.created_at AS first_seen, tx.source_s3_key AS first_key, "
        f"tx.report_date AS first_date FROM {table} x JOIN topics tx ON tx.id = x.topic_id "
        f"WHERE x.stable_id = c.stable_id AND x.site_id = c.site_id "
        f"AND {DELETED_TOPIC_PREDICATE.format(alias='tx')} "
        f"AND {DELETED_SOURCE_PREDICATE.format(alias='tx')} "
        f"ORDER BY x.created_at ASC, x.id ASC LIMIT 1) fs ON true "
        # The session the first-seen key's FILE name carries (lake_keys._sid_from_name):
        # session_id is the table's primary key and the key names this item's own
        # recording, so the join reads one timestamp and widens nothing.
        f"LEFT JOIN meeting_session ms ON ms.session_id = "
        f"substring(split_part(fs.first_key, '/', 4) from 'sid([0-9a-f]{{32}})') "
        f"LEFT JOIN LATERAL (SELECT e.dst_turn FROM event_links e "
        f"WHERE e.{link_col} = c.id AND e.link_type = 'supported_by' AND e.dst_kind = 'turn' "
        f"ORDER BY {BEST_LINK_ORDER} LIMIT 1) o ON true "
        f"WHERE c.site_id = %(site_id)s AND s.company_id = %(company_id)s "
        f"AND {live_predicate}{status_clause} "
        f"AND (%(since)s::date IS NULL OR fs.first_date >= %(since)s::date) "
        f"AND (%(until)s::date IS NULL OR fs.first_date <= %(until)s::date)"
    )


def _source(first_key, first_date):
    parsed = lake_keys.parse(first_key)
    folder = base = None
    if parsed and parsed["kind"] == "extraction":
        folder = parsed["folder"]
        base = parsed["session_base"] or (
            f"grp{parsed['group_id']}" if parsed["group_id"] else None)
    date = first_date.isoformat() if first_date else None
    return {"user_folder": folder, "date": date, "session_base": base,
            "source_s3_key": first_key}


def recorded_at_from_stem(session_base):
    """The recording start a legacy session_base names, as aware UTC, or None.

    The base is the device's transcript stem (`{device}_{YYYY-MM-DD}_{HH-MM-SS}` or the
    hyphen-only variant), named with the device's NZ wall clock; transcript_utils reads
    the timestamp exactly as the extractor does and nz_time converts it (never +13)."""
    if not session_base:
        return None
    local = transcript_utils.extract_base_time_from_filename(session_base)
    if local is None:
        return None
    return nz_time.from_nz(local).replace(tzinfo=_dt.timezone.utc)


def _recorded_at(first_key, opened_at):
    parsed = lake_keys.parse(first_key)
    if not parsed or parsed["kind"] != "extraction" or parsed["group_id"]:
        return None
    if parsed["sid"]:
        return opened_at.astimezone(_dt.timezone.utc) if opened_at is not None else None
    return recorded_at_from_stem(parsed["session_base"])


def _origin(turn):
    if not isinstance(turn, dict):
        return None
    return {k: turn.get(k) for k in _ORIGIN_KEYS}


def _norm(name):
    return " ".join(str(name).split()).casefold()


def find_items(conn, *, company_id, site_id, kinds, statuses=None, since=None, until=None,
               recorded_by=None, responsible=None, exclude_source_keys=None, query=None,
               limit=10) -> list[dict]:
    """A site's live items of `kinds`, one per stable_id, ranked by `query`.

    Returns dicts with exactly: kind, stable_id, item_id (the extractor's lineage id, not
    the row id), text, responsible, deadline, status, first_seen, source {user_folder,
    date, session_base, source_s3_key}, recorded_by, carried_from, score, matched_terms,
    place (always None in this slice), origin, first_seen_recorded_at (aware UTC
    recording start of the first-seen row's session, or None -- module docstring).

    * `statuses`: {kind: [status, ...]}; a kind absent from it is unfiltered. An empty
      list, or a filter on 'decision' or 'finding', raises ValueError.
    * `since`/`until`: inclusive, on the first-seen row's topic report_date.
    * `recorded_by`: the folder of the first-seen row's extraction key; a key that does
      not parse has recorded_by None and never matches.
    * `responsible`: equality after whitespace/case folding; questions never match.
    * `exclude_source_keys`: drops items whose LIVE row's topic key is listed.
    * `query`: ranks by overlap(query, text); never filters. Without it every score is 0.
    * Order: score DESC, first_seen DESC, stable_id ASC; then the first `limit`.

    [] when the site does not belong to `company_id` (sites.company_id, the current owner).
    """
    kinds, statuses, since, until = _validate(company_id, site_id, kinds, statuses,
                                              since, until, limit)
    if isinstance(exclude_source_keys, str):
        # A bare key would iterate to characters and exclude nothing.
        raise ValueError("exclude_source_keys must be a collection of source keys")
    live = visible_topics_predicate("t")
    excluded = set(exclude_source_keys or ())
    want_resp = _norm(responsible) if responsible is not None else None

    cur = conn.cursor(row_factory=dict_row)
    best = {}
    for kind in kinds:
        params = {"site_id": site_id, "company_id": company_id,
                  "since": since, "until": until, "statuses": statuses.get(kind)}
        sql = _kind_sql(kind, live, kind in statuses)
        for row in cur.execute(sql, params).fetchall():
            key = (kind, row["stable_id"])
            # Two live rows can share a stable_id for one commit's width in a concurrent
            # pass; the newest is the item (as action_items.get_live_by_stable_id does).
            prior = best.get(key)
            if prior is None or (row["created_at"], str(row["id"])) > (
                    prior[1]["created_at"], str(prior[1]["id"])):
                best[key] = (kind, row)

    out = []
    for kind, row in best.values():
        if row["live_key"] is not None and row["live_key"] in excluded:
            continue
        source = _source(row["first_key"], row["first_date"])
        if recorded_by is not None and source["user_folder"] != recorded_by:
            continue
        if want_resp is not None and (row["responsible"] is None
                                      or _norm(row["responsible"]) != want_resp):
            continue
        if query:
            score, matched = overlap(query, row["text"] or "")
        else:
            score, matched = 0.0, []
        out.append({
            "kind": kind,
            "stable_id": row["stable_id"],
            "item_id": row["item_id"],
            "text": row["text"],
            "responsible": row["responsible"],
            "deadline": row["deadline"],
            "status": row["status"],
            "first_seen": row["first_seen"],
            "source": source,
            "recorded_by": source["user_folder"],
            "carried_from": row["carried_from"],
            "score": float(score),
            "matched_terms": list(matched),
            "place": None,
            "origin": _origin(row["origin"]),
            "first_seen_recorded_at": _recorded_at(row["first_key"], row["first_opened_at"]),
        })

    # Three stable sorts, least significant key first: stable_id ASC, first_seen DESC,
    # score DESC.
    out.sort(key=lambda r: str(r["stable_id"]))
    out.sort(key=lambda r: r["first_seen"], reverse=True)
    out.sort(key=lambda r: r["score"], reverse=True)
    return out[:limit]
