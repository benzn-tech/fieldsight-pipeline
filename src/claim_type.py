"""claim_type: who said the words an item stands on, derived from its transcript link.

Spec: docs/superpowers/specs/2026-11-08-track-c-item-history-and-evidence-chain-design.md §5.4.
Derived, never self-reported. Rule v1:

  * `none_close` (no link, nothing close in the topic's turns)        -> 'inferred'
  * `linked`, the turn's CONFIRMED name is the recorder's user        -> 'observed'
  * `linked`, the turn's CONFIRMED name is a different (known) user   -> 'stated'
  * everything else (no name, tentative name, a name with no user, an
    unknown recorder, ambiguous/below_floor, no support at all)       -> None (unknown)

'observed' means only "the recorder said it themselves", never "true".

`claim_basis` carries ids, statuses and the rule version only -- never a display name and
never item or transcript text. Pure: no DB, no AWS.
"""

RULE_VERSION = "claim_v1"

CLAIM_TYPES = ("observed", "stated", "inferred")


def derive(support, named, recorder_user_id):
    """`(claim_type, claim_basis)` for one item.

    `support` is the item's support object (item_support) -- for a written link it may also
    carry `link_id`. `named` is the overlay hit for the linked turn
    (`{user_id, state, ...}`) or None. `recorder_user_id` is the topic's user."""
    status = support.get("status") if isinstance(support, dict) else None
    basis = {"rule_version": RULE_VERSION, "support_status": status}
    if status == "none_close":
        return "inferred", basis
    if status != "linked":
        return None, basis
    if support.get("link_id") is not None:
        basis["link_id"] = str(support["link_id"])
    if not isinstance(named, dict):
        return None, basis
    user_id = named.get("user_id")
    state = named.get("state")
    basis["name_state"] = state
    if user_id is not None:
        basis["named_user_id"] = str(user_id)
    if state != "confirmed" or user_id is None or recorder_user_id is None:
        return None, basis
    if str(user_id) == str(recorder_user_id):
        return "observed", basis
    return "stated", basis
