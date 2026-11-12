"""GET /api/org/status-pick-cards and POST /api/org/status-pick-cards/{id}
(spec 2026-11-12 status pick card surface, §4.1-§4.2).

Reach is the card's site in _allowed_site_ids. Only LIVE candidates are shown (the shared
drop rule), in stored order; a card with none is hidden; the 50-card cap applies after
filtering. choose = the normal PATCH (audited, actor = the person) + the CAS resolve, in one
transaction; none = needs site authority or edit rights on every live candidate.
"""
import logging
import uuid

import lambda_org_api as org
from tests.unit.test_lambda_org_api import (  # noqa: F401  (fixture)
    CALLER, SITE_ID, body_of, make_event, wired)

OTHER_SITE = "b2b2b2b2-b2b2-b2b2-b2b2-b2b2b2b2b2b2"
S = [f"{n}{n}{n}{n}{n}{n}{n}{n}-0000-4000-8000-00000000000{n}" for n in range(1, 7)]
CO = CALLER["company_id"]
WORKER_NAME = "Ada L"                    # CALLER's display name -> the assignee tier


def _cid():
    return str(uuid.uuid4())


def _item(n, **kw):
    row = {"id": f"i-{n}", "stable_id": S[n], "site_id": SITE_ID, "company_id": CO,
           "text": f"task {n}", "responsible": "Someone Else", "status": "open",
           "priority": "low", "topic_user_id": None}
    row.update(kw)
    return row


def _card(cands, site=SITE_ID, status="done", state="pending", cid=None, quote="that one"):
    return {"id": cid or _cid(), "company_id": CO, "site_id": site, "suggested_status": status,
            "quote": quote, "segment_key": "k", "offset_sec": 1.0, "candidates": list(cands),
            "run": "r", "state": state, "chosen_stable_id": None, "resolved_by": None,
            "resolved_at": None, "created_at": "2026-11-12T10:00:00+00:00"}


def _wire(wired, cards, items, *, allowed=None, roles=None, role="admin", resolved=True,
          locked=None, caller_extra=None):
    """`items` maps stable_id -> live row (absent = gone); `locked` overrides the
    FOR UPDATE re-read. `seen` records every write and the reads that matter."""
    caller = {**CALLER, "global_role": role, **(caller_extra or {})}
    wired.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(caller))
    wired.setattr(org, "_allowed_site_ids",
                  lambda conn, c: set(allowed if allowed is not None else {SITE_ID}))
    wired.setattr(org.memberships, "caller_site_roles", lambda conn, uid: dict(roles or {}))
    seen = {"audits": [], "resolved": [], "pending_sites": [], "locked_reads": []}
    by_id = {c["id"]: c for c in cards}

    def _pending(conn, site_ids):
        seen["pending_sites"].append(sorted(site_ids))
        return [dict(c) for c in cards if c["site_id"] in site_ids and c["state"] == "pending"]

    def _get(conn, i):
        c = by_id.get(org.canon_uuid(i))
        return dict(c) if c else None

    def _resolve(conn, i, state, sid, uid):
        seen["resolved"].append((str(i), state, sid, uid))
        return {**by_id[str(i)], "state": state, "chosen_stable_id": sid} if resolved else None

    wired.setattr(org.status_pick_cards_repo, "pending_for_sites", _pending)
    wired.setattr(org.status_pick_cards_repo, "get_pick_card", _get)
    wired.setattr(org.status_pick_cards_repo, "resolve_pick_card", _resolve)

    def _live(conn, sid, for_update=False):
        if for_update:
            seen["locked_reads"].append(sid)
            if locked is not None and sid in locked:
                return dict(locked[sid]) if locked[sid] else None
        r = items.get(sid)
        return dict(r) if r else None

    wired.setattr(org.action_items, "get_live_by_stable_id", _live)
    current = {**{r["id"]: r for r in items.values() if r},
               **{r["id"]: r for r in (locked or {}).values() if r}}
    wired.setattr(org.action_items, "get_action_item",
                  lambda conn, i: dict(current[i]) if i in current else None)
    wired.setattr(org.action_items, "update_action_item_fields",
                  lambda conn, i, fields, by: (seen.update(fields=fields, patched=i, by=by)
                                               or {**current[i], **fields}))
    wired.setattr(org.content_edits, "append_content_edit",
                  lambda conn, *a: seen["audits"].append(a) or {"id": "e-1"})
    wired.setattr(org.action_items, "task_nos_for_item_ids",
                  lambda conn, ids: (seen.setdefault("task_no_calls", []).append(list(ids))
                                     or {i: 7 for i in ids}))
    wired.setattr(org.sites, "list_sites_by_ids",
                  lambda conn, ids: (seen.setdefault("site_reads", []).append(sorted(ids))
                                     or [{"id": i, "name": f"Site {i[:4]}"} for i in ids]))
    return seen


def _get_cards(params=None):
    res = org.lambda_handler(make_event("GET", "/api/org/status-pick-cards", params=params), None)
    return res


def _post(card_id, body):
    return org.lambda_handler(make_event(
        "POST", f"/api/org/status-pick-cards/{card_id}", body=body), None)


# ------------------------------------------------------------------ GET


def test_get_hides_unreachable_sites_and_other_tenants(wired):
    mine = _card([S[0], S[1]])
    theirs = _card([S[2], S[3]], site=OTHER_SITE)
    items = {S[0]: _item(0), S[1]: _item(1),
             S[2]: _item(2, site_id=OTHER_SITE, company_id="c-other"),
             S[3]: _item(3, site_id=OTHER_SITE, company_id="c-other")}
    seen = _wire(wired, [mine, theirs], items)
    res = _get_cards()
    assert res["statusCode"] == 200, body_of(res)
    assert [c["id"] for c in body_of(res)["cards"]] == [mine["id"]]
    assert seen["pending_sites"] == [[SITE_ID]]


def test_get_site_param_out_of_reach_403(wired):
    seen = _wire(wired, [_card([S[0], S[1]], site=OTHER_SITE)], {})
    res = _get_cards({"site": OTHER_SITE})
    assert res["statusCode"] == 403
    assert seen["pending_sites"] == []


def test_get_site_param_narrows_to_that_site(wired):
    other_reach = "c3c3c3c3-c3c3-c3c3-c3c3-c3c3c3c3c3c3"
    seen = _wire(wired, [], {}, allowed={SITE_ID, other_reach})
    res = _get_cards({"site": SITE_ID})
    assert res["statusCode"] == 200 and body_of(res) == {"cards": []}
    assert seen["pending_sites"] == [[SITE_ID]]


def test_moved_site_candidate_dropped(wired):
    # live, moved_site, gone, terminal, same -- only the live one survives.
    card = _card([S[0], S[1], S[2], S[3], S[4]], status="blocked")
    items = {S[0]: _item(0), S[1]: _item(1, site_id=OTHER_SITE),
             S[3]: _item(3, status="done"), S[4]: _item(4, status="blocked")}
    _wire(wired, [card], items)
    cards = body_of(_get_cards())["cards"]
    assert len(cards) == 1
    assert [c["stable_id"] for c in cards[0]["candidates"]] == [S[0]]
    # A choose on the moved candidate is refused as no longer open, with no write.
    seen = _wire(wired, [card], items)
    res = _post(card["id"], {"decision": "choose", "stable_id": S[1]})
    assert res["statusCode"] == 409 and body_of(res)["error"] == "item no longer open"
    assert "fields" not in seen and seen["resolved"] == []


def test_card_with_no_live_candidate_omitted(wired):
    dead = _card([S[0], S[1]])
    _wire(wired, [dead], {S[0]: _item(0, status="done")})
    assert body_of(_get_cards()) == {"cards": []}


def test_one_live_candidate_still_a_card(wired):
    card = _card([S[0], S[1]])
    _wire(wired, [card], {S[0]: _item(0, status="done"), S[1]: _item(1)})
    cards = body_of(_get_cards())["cards"]
    assert len(cards) == 1
    c = cards[0]
    assert c["status"] == "done" and c["quote"] == "that one" and c["site_id"] == SITE_ID
    assert c["site_name"] == f"Site {SITE_ID[:4]}"
    assert c["candidates"] == [{"stable_id": S[1], "action_item_id": "i-1", "text": "task 1",
                                "status": "open", "responsible": "Someone Else",
                                "task_no": 7, "can_choose": True}]


def test_can_choose_and_can_dismiss_flags(wired):
    card = _card([S[0], S[1]])
    items = {S[0]: _item(0, responsible=WORKER_NAME), S[1]: _item(1)}
    # A worker who is the assignee of one candidate only.
    _wire(wired, [card], items, role="worker")
    c = body_of(_get_cards())["cards"][0]
    assert [x["can_choose"] for x in c["candidates"]] == [True, False]
    assert c["can_dismiss"] is False
    # The same worker as the site's pm: both choosable, may dismiss.
    _wire(wired, [card], items, role="worker", roles={SITE_ID: "pm"})
    c = body_of(_get_cards())["cards"][0]
    assert [x["can_choose"] for x in c["candidates"]] == [True, True]
    assert c["can_dismiss"] is True
    # The assignee of every live candidate may dismiss without a site role.
    both = {S[0]: _item(0, responsible=WORKER_NAME), S[1]: _item(1, responsible=WORKER_NAME)}
    _wire(wired, [card], both, role="worker")
    assert body_of(_get_cards())["cards"][0]["can_dismiss"] is True
    # Company admin.
    _wire(wired, [card], items)
    assert body_of(_get_cards())["cards"][0]["can_dismiss"] is True


def test_get_reads_task_numbers_and_sites_once(wired):
    cards = [_card([S[0], S[1]]), _card([S[2], S[3]])]
    items = {s: _item(n) for n, s in enumerate(S[:4])}
    seen = _wire(wired, cards, items)
    assert len(body_of(_get_cards())["cards"]) == 2
    assert len(seen["task_no_calls"]) == 1 and sorted(seen["task_no_calls"][0]) == [
        "i-0", "i-1", "i-2", "i-3"]
    assert seen["site_reads"] == [[SITE_ID]]


def test_cap_applies_after_filtering(wired):
    stale = [_card([S[0], S[1]]) for _ in range(60)]        # newest first: all dead
    live = _card([S[2], S[3]])                              # oldest, still live
    items = {S[0]: _item(0, status="done"), S[1]: _item(1, status="done"),
             S[2]: _item(2), S[3]: _item(3)}
    _wire(wired, stale + [live], items)
    assert [c["id"] for c in body_of(_get_cards())["cards"]] == [live["id"]]
    many = [_card([S[2], S[3]]) for _ in range(55)]
    _wire(wired, many, items)
    assert len(body_of(_get_cards())["cards"]) == 50


def test_get_never_writes(wired):
    seen = _wire(wired, [_card([S[0], S[1]])], {S[0]: _item(0), S[1]: _item(1)})
    _get_cards()
    assert seen["resolved"] == [] and "fields" not in seen and seen["audits"] == []


# ------------------------------------------------------------------ POST choose


def test_choose_happy_path_patches_and_resolves(wired):
    card = _card([S[0], S[1]])
    seen = _wire(wired, [card], {S[0]: _item(0), S[1]: _item(1)})
    res = _post(card["id"], {"decision": "choose", "stable_id": S[1].upper()})
    assert res["statusCode"] == 200, body_of(res)
    assert seen["fields"] == {"status": card["suggested_status"]} and seen["patched"] == "i-1"
    assert [a[3:6] for a in seen["audits"]] == [("status", "open", "done")]
    assert seen["locked_reads"] == [S[1]]
    assert seen["resolved"] == [(card["id"], "chosen", S[1], CALLER["id"])]
    assert body_of(res) == {"id": card["id"], "state": "chosen", "status": "done",
                            "action_item_id": "i-1"}


def test_choose_not_a_candidate_400(wired):
    card = _card([S[0], S[1]])
    seen = _wire(wired, [card], {S[0]: _item(0), S[1]: _item(1), S[2]: _item(2)})
    res = _post(card["id"], {"decision": "choose", "stable_id": S[2]})
    assert res["statusCode"] == 400 and body_of(res)["error"] == "not a candidate"
    assert "fields" not in seen and seen["resolved"] == []


def test_choose_rechecks_under_lock(wired):
    card = _card([S[0], S[1]])
    seen = _wire(wired, [card], {S[0]: _item(0), S[1]: _item(1)},
                 locked={S[1]: _item(1, status="done")})
    res = _post(card["id"], {"decision": "choose", "stable_id": S[1]})
    assert res["statusCode"] == 409 and body_of(res)["error"] == "item no longer open"
    assert "fields" not in seen and seen["audits"] == [] and seen["resolved"] == []


def test_choose_cas_loss_rolls_back_409(wired):
    card = _card([S[0], S[1]])
    seen = _wire(wired, [card], {S[0]: _item(0), S[1]: _item(1)}, resolved=False)
    res = _post(card["id"], {"decision": "choose", "stable_id": S[0]})
    assert res["statusCode"] == 409 and body_of(res)["error"] == "card already decided"
    assert len(seen["resolved"]) == 1           # the CAS ran after the PATCH, and lost


def test_choose_refused_patch_is_returned_and_not_resolved(wired):
    card = _card([S[0], S[1]])
    seen = _wire(wired, [card], {S[0]: _item(0), S[1]: _item(1)}, role="worker")
    res = _post(card["id"], {"decision": "choose", "stable_id": S[0]})
    assert res["statusCode"] == 403
    assert "fields" not in seen and seen["resolved"] == []


# ------------------------------------------------------------------ POST none


def test_none_by_site_role(wired):
    card = _card([S[0], S[1]])
    seen = _wire(wired, [card], {S[0]: _item(0), S[1]: _item(1)},
                 role="worker", roles={SITE_ID: "site_manager"})
    res = _post(card["id"], {"decision": "none"})
    assert res["statusCode"] == 200, body_of(res)
    assert body_of(res) == {"id": card["id"], "state": "none", "status": "done",
                            "action_item_id": None}
    assert seen["resolved"] == [(card["id"], "none", None, CALLER["id"])]
    assert "fields" not in seen


def test_none_by_editing_every_candidate(wired):
    card = _card([S[0], S[1], S[2]])
    # S[2] is gone, so "every live candidate" is S[0] and S[1].
    seen = _wire(wired, [card], {S[0]: _item(0, responsible=WORKER_NAME),
                                 S[1]: _item(1, responsible=WORKER_NAME)}, role="worker")
    assert _post(card["id"], {"decision": "none"})["statusCode"] == 200
    assert seen["resolved"] == [(card["id"], "none", None, CALLER["id"])]


def test_none_needs_authority_over_every_candidate(wired):
    card = _card([S[0], S[1]])
    seen = _wire(wired, [card], {S[0]: _item(0, responsible=WORKER_NAME), S[1]: _item(1)},
                 role="worker")
    assert _post(card["id"], {"decision": "none"})["statusCode"] == 403
    assert seen["resolved"] == []


def test_none_cas_loss_409(wired):
    card = _card([S[0], S[1]])
    _wire(wired, [card], {S[0]: _item(0), S[1]: _item(1)}, resolved=False)
    res = _post(card["id"], {"decision": "none"})
    assert res["statusCode"] == 409 and body_of(res)["error"] == "card already decided"


# ------------------------------------------------------------------ POST errors


def test_decide_unknown_or_malformed_card_404(wired):
    _wire(wired, [_card([S[0], S[1]])], {S[0]: _item(0), S[1]: _item(1)})
    assert _post("nope", {"decision": "none"})["statusCode"] == 404
    assert _post(_cid(), {"decision": "none"})["statusCode"] == 404


def test_decide_out_of_reach_404(wired):
    card = _card([S[0], S[1]], site=OTHER_SITE)
    seen = _wire(wired, [card], {S[0]: _item(0, site_id=OTHER_SITE),
                                 S[1]: _item(1, site_id=OTHER_SITE)})
    assert _post(card["id"], {"decision": "none"})["statusCode"] == 404
    assert _post(card["id"], {"decision": "choose", "stable_id": S[0]})["statusCode"] == 404
    assert seen["resolved"] == [] and "fields" not in seen


def test_decide_card_with_no_live_candidate_404(wired):
    card = _card([S[0], S[1]])
    seen = _wire(wired, [card], {S[0]: _item(0, status="done")})
    assert _post(card["id"], {"decision": "none"})["statusCode"] == 404
    assert seen["resolved"] == []


def test_decided_card_409(wired):
    card = _card([S[0], S[1]], state="chosen")
    seen = _wire(wired, [card], {S[0]: _item(0), S[1]: _item(1)})
    for body in ({"decision": "none"}, {"decision": "choose", "stable_id": S[0]}):
        res = _post(card["id"], body)
        assert res["statusCode"] == 409 and body_of(res)["error"] == "card already decided"
    assert seen["resolved"] == [] and "fields" not in seen


def test_bad_body_400(wired):
    card = _card([S[0], S[1]])
    seen = _wire(wired, [card], {S[0]: _item(0), S[1]: _item(1)})
    for body in ({"decision": "maybe"}, {}, {"decision": "choose"},
                 {"decision": "choose", "stable_id": "not-a-uuid"},
                 {"decision": "choose", "stable_id": 12}):
        assert _post(card["id"], body)["statusCode"] == 400, body
    assert seen["resolved"] == [] and "fields" not in seen


def test_logs_never_carry_quote_or_text(wired, caplog):
    secret_q, secret_t, secret_r = "QUOTE-SENTINEL", "TEXT-SENTINEL", "RESP-SENTINEL"
    card = _card([S[0], S[1]], quote=secret_q)
    items = {S[0]: _item(0, text=secret_t, responsible=secret_r),
             S[1]: _item(1, text=secret_t, responsible=secret_r)}
    caplog.set_level(logging.DEBUG)
    _wire(wired, [card], items)
    assert _get_cards()["statusCode"] == 200
    assert _post(card["id"], {"decision": "choose", "stable_id": S[0]})["statusCode"] == 200
    _wire(wired, [card], items, resolved=False)
    assert _post(card["id"], {"decision": "none"})["statusCode"] == 409
    _wire(wired, [card], items, locked={S[0]: None})
    assert _post(card["id"], {"decision": "choose", "stable_id": S[0]})["statusCode"] == 409
    assert caplog.text                              # the handlers do log...
    for secret in (secret_q, secret_t, secret_r):
        assert secret not in caplog.text            # ...but never the words
