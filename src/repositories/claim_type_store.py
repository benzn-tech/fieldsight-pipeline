"""claim_type storage: write the derived claim_type, link a pass's items, recompute a session.

Spec: docs/superpowers/specs/2026-11-08-track-c-item-history-and-evidence-chain-design.md §5.4,
§9.1. One rule (`claim_type.derive`) and three callers:

  * `write_pass` -- lambda_item_writer, in its own SAVEPOINT after carry-forward: writes
    each linked child's supported_by row (carrying the row's post-carry stable_id) and every
    supported child's claim_type/claim_basis. A child with no `support` key is not touched.
  * `recompute_session` -- after the voiceprint writer lands a session's names: re-derives
    every live item of the session that has a supported_by link, or whose last basis said
    `none_close`.
  * a backfill script, through `recompute_session`.

Speaker identity: a turn's name comes from `speaker_turn_names` (voiceprints.live_turn_names,
keyed on the normalised `sid{32hex}`), joined to the linked turn by
turn_name_overlay.resolve (tolerance, one row per turn -- never equality), and the name's
voiceprint maps to a user through speaker_voiceprints.user_id (withdrawn profiles map to
nobody; the profile may live in the recorder's home company). Only `confirmed` names
count; derive enforces that.

claim_basis holds ids and statuses only -- never a display name, never text.
"""

from psycopg.errors import QueryCanceled
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import claim_type
import turn_name_overlay
from deleted_predicates import visible_topics_predicate
from repositories import event_links, voiceprints

# dst_turn keys (migration 0098's column comment). Locators and labels only.
DST_TURN_KEYS = ("source_stem", "start_sec", "end_sec", "speaker_label", "quote_status")

SUPPORT_STATUSES = ("linked", "none_close", "ambiguous", "below_floor")



def _kind(kind):
    if kind not in event_links.KIND_TABLE:
        raise ValueError(f"unknown kind {kind!r}")
    return kind


def write(conn, kind, row_id, claim_type_value, claim_basis):
    """Set one item row's claim_type and claim_basis."""
    if claim_type_value is not None and claim_type_value not in claim_type.CLAIM_TYPES:
        raise ValueError(f"unknown claim_type {claim_type_value!r}")
    table = event_links.KIND_TABLE[_kind(kind)]
    conn.execute(f"UPDATE {table} SET claim_type = %s, claim_basis = %s WHERE id = %s",
                 (claim_type_value, Jsonb(claim_basis) if claim_basis is not None else None,
                  row_id))


def _names_key(session_base):
    """The key speaker_turn_names is stored under (speaker_match_request normalises it)."""
    return turn_name_overlay.session_base(session_base) or session_base


def _named_turns(conn, company_id, session_base, turns):
    """{(source_stem, start_sec): named} for the given linked turns; `named` is the overlay
    hit plus `user_id`. Empty when the session has no names (the common case at write time).
    """
    turns = sorted({t for t in turns if t[0] is not None and t[1] is not None},
                   key=lambda t: (str(t[0]), float(t[1])))
    if not turns or not company_id or not session_base:
        return {}
    rows = voiceprints.live_turn_names(conn, company_id, _names_key(session_base))
    if not rows:
        return {}
    index = turn_name_overlay.build(rows)
    hits = turn_name_overlay.resolve(
        index, [{"source_filename": stem, "chunk_start": float(start)} for stem, start in turns])
    if not hits:
        return {}
    vp_ids = list({str(h["voiceprint_id"]) for h in hits.values() if h.get("voiceprint_id")})
    users = {}
    if vp_ids:
        users = {str(vid): uid for vid, uid in conn.execute(
            # No company filter: the turn-name rows are already company-scoped, and on
            # another company's site the recorder's OWN profile is matched from their home
            # company (voiceprints.own_profiles_for_matching, tenancy P5). Filtering on the
            # site's company would drop exactly the recorder and turn `observed` into NULL.
            "SELECT id, user_id FROM speaker_voiceprints "
            "WHERE id = ANY(%s::uuid[]) AND status <> 'withdrawn'",
            (vp_ids,)).fetchall()}
    out = {}
    for si, hit in hits.items():
        named = dict(hit)
        named["user_id"] = users.get(str(hit.get("voiceprint_id")))
        out[turns[si]] = named
    return out


def _turn_key(support):
    start = support.get("start_sec")
    return (support.get("source_stem"), float(start) if start is not None else None)


def _empty_counts():
    return {"support": {s: 0 for s in SUPPORT_STATUSES},
            "claim": {c: 0 for c in claim_type.CLAIM_TYPES + ("null",)},
            "links": 0}


def write_pass(conn, *, company_id, site_id, session_base, source_s3_key,
               recorder_user_id, items):
    """Link and classify one pass's children. `items` is `[(kind, row_id, support)]` for the
    children that carry a support object, collected at insert time. Returns counts
    `{support: {status: n}, claim: {type|null: n}, links: n}`."""
    counts = _empty_counts()
    items = [(kind, rid, sup) for kind, rid, sup in items or () if isinstance(sup, dict)]
    linked = [_turn_key(sup) for _, _, sup in items if sup.get("status") == "linked"]
    names = _named_turns(conn, company_id, session_base, linked)
    for kind, row_id, support in items:
        status = support.get("status")
        if status in counts["support"]:
            counts["support"][status] += 1
        basis_in = dict(support)
        named = None
        if status == "linked":
            basis_in["link_id"] = event_links.insert_supported_by(
                conn, site_id=site_id, src_kind=kind, src_row_id=row_id, src_stable_id=None,
                source_s3_key=source_s3_key,
                dst_turn={k: support.get(k) for k in DST_TURN_KEYS},
                score=support.get("score"), rule_version=support.get("rule_version"))
            counts["links"] += 1
            named = names.get(_turn_key(support))
        ct, basis = claim_type.derive(basis_in, named, recorder_user_id)
        write(conn, kind, row_id, ct, basis)
        counts["claim"][ct or "null"] += 1
    return counts


def _session_key_suffix(session_base):
    return f"/{session_base}.json"


def _session_sites(conn, company_id, suffix):
    """The sites of `company_id` that hold a LIVE, visible topic of this session (by
    extraction-key suffix). Scoped through sites.company_id: a same-named session in another
    company is never returned; a site holding only superseded/deleted topics costs nothing."""
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT t.site_id::text FROM topics t JOIN sites s ON s.id = t.site_id "
        "WHERE s.company_id = %s AND right(t.source_s3_key, %s) = %s AND "
        + visible_topics_predicate("t") + " ORDER BY 1",
        (company_id, len(suffix), suffix)).fetchall()]


def recompute_session(conn, *, company_id, site_id=None, session_base,
                      statement_timeout=None) -> dict:
    """Re-derive claim_type for every live item of one session on `site_id`.

    The session's items are the live (visible_topics_predicate) rows that either have a
    supported_by link whose source_s3_key ends `/{session_base}.json` (the best link per
    row, event_links.BEST_LINK_ORDER), or whose topic carries that key and whose last
    claim_basis said `none_close`. The recorder is each row's topic user. Returns
    `{rows, changed, claim: {type|null: n}}`.

    `site_id=None` (the voiceprint writer, which knows the company and session but not
    the site): every site OF `company_id` holding a live topic of the session, each
    recomputed as above in its OWN savepoint, counts summed, plus `sites: n` and
    `failed: n` / `errors: [exception type names]`. One site's failure rolls back only
    that site. A QueryCanceled (the caller's statement_timeout) stops the loop -- the
    budget is spent -- and the sites already done stand (`timed_out: True`).

    `statement_timeout` (e.g. "15s"), when given, is set LOCAL to the caller's transaction
    first, so a synchronous caller has a bound on every statement below."""
    if not company_id or not session_base:
        raise ValueError("company_id and session_base are required")
    if statement_timeout is not None:
        conn.execute("SELECT set_config('statement_timeout', %s, true)",
                     (str(statement_timeout),))
    if site_id is None:
        total = {"rows": 0, "changed": 0, "sites": 0, "failed": 0, "errors": [],
                 "timed_out": False,
                 "claim": {c: 0 for c in claim_type.CLAIM_TYPES + ("null",)}}
        for sid in _session_sites(conn, company_id, _session_key_suffix(session_base)):
            try:
                with conn.transaction():
                    one = _recompute_site(conn, company_id, sid, session_base)
            except QueryCanceled as exc:
                total["failed"] += 1
                total["errors"].append(type(exc).__name__)
                total["timed_out"] = True
                break
            except Exception as exc:
                total["failed"] += 1
                total["errors"].append(type(exc).__name__)
                continue
            total["sites"] += 1
            total["rows"] += one["rows"]
            total["changed"] += one["changed"]
            for k, v in one["claim"].items():
                total["claim"][k] += v
        return total
    return _recompute_site(conn, company_id, site_id, session_base)


def _recompute_site(conn, company_id, site_id, session_base) -> dict:
    if not site_id:
        raise ValueError("company_id, site_id and session_base are required")
    owner = conn.execute("SELECT company_id::text FROM sites WHERE id = %s",
                         (site_id,)).fetchone()
    if owner is None or owner[0] != str(company_id):
        raise ValueError("site_id is not a site of company_id")
    suffix = _session_key_suffix(session_base)
    cur = conn.cursor(row_factory=dict_row)
    rows = []
    for kind, table in event_links.KIND_TABLE.items():
        col = event_links.KIND_SRC_COLUMN[kind]
        live = visible_topics_predicate("t")
        for r in cur.execute(
                f"SELECT c.id, c.claim_type, c.claim_basis, t.user_id, "
                f"o.id AS link_id, o.dst_turn "
                f"FROM {table} c JOIN topics t ON t.id = c.topic_id "
                f"JOIN LATERAL (SELECT e.id, e.dst_turn FROM event_links e "
                f"WHERE e.{col} = c.id AND e.link_type = 'supported_by' "
                f"AND e.dst_kind = 'turn' AND right(e.source_s3_key, %(n)s) = %(sfx)s "
                f"ORDER BY {event_links.BEST_LINK_ORDER} LIMIT 1) o ON true "
                f"WHERE c.site_id = %(site)s AND {live}",
                {"n": len(suffix), "sfx": suffix, "site": site_id}).fetchall():
            support = {"status": "linked", **(r["dst_turn"] or {}), "link_id": r["link_id"]}
            rows.append((kind, r, support))
        for r in cur.execute(
                f"SELECT c.id, c.claim_type, c.claim_basis, t.user_id "
                f"FROM {table} c JOIN topics t ON t.id = c.topic_id "
                f"WHERE c.site_id = %(site)s AND right(t.source_s3_key, %(n)s) = %(sfx)s "
                f"AND c.claim_basis->>'support_status' = 'none_close' AND {live} "
                f"AND NOT EXISTS (SELECT 1 FROM event_links e WHERE e.{col} = c.id "
                f"AND e.link_type = 'supported_by')",
                {"n": len(suffix), "sfx": suffix, "site": site_id}).fetchall():
            rows.append((kind, r, {"status": "none_close"}))
    names = _named_turns(conn, company_id, session_base,
                         [_turn_key(s) for _, _, s in rows if s["status"] == "linked"])
    counts = {"rows": 0, "changed": 0, "claim": {c: 0 for c in claim_type.CLAIM_TYPES + ("null",)}}
    for kind, r, support in rows:
        named = names.get(_turn_key(support)) if support["status"] == "linked" else None
        ct, basis = claim_type.derive(support, named, r["user_id"])
        counts["rows"] += 1
        counts["claim"][ct or "null"] += 1
        if ct != r["claim_type"] or basis != r["claim_basis"]:
            write(conn, kind, r["id"], ct, basis)
            counts["changed"] += 1
    return counts
