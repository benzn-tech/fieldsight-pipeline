"""claim_type: who said the words an item stands on, derived from its transcript link.

Spec: docs/superpowers/specs/2026-11-08-track-c-item-history-and-evidence-chain-design.md §5.4;
rule v2: docs/superpowers/specs/2026-11-10-item-quote-locator-design.md §4.4.
Derived, never self-reported. Rule v2 (one rule version for every support, lexical or
locator):

  * `linked`, the turn's CONFIRMED name is the recorder's user        -> 'observed'
  * `linked`, the turn's CONFIRMED name is a different (known) user   -> 'stated'
  * `no_locator` (locator: the model said no single line raises it)   -> 'inferred' only
    when INFERRED_FROM_NO_LOCATOR (set from the measured precision bar), else None
  * `none_close` (lexical: no link, nothing close in the topic's turns) -> 'inferred'
    (kept from v1; the locator slice never produces it)
  * everything else (no name, tentative name, a name with no user, an
    unknown recorder, ambiguous/below_floor, every locator refusal, no
    support at all)                                                   -> None (unknown)

'observed' means only "the recorder said it themselves", never "true".

`claim_basis` carries ids, statuses and the rule version only -- never a display name and
never item or transcript text. Pure: no DB, no AWS.
"""

RULE_VERSION = "claim_v2"

# Locator spec §4.4 / §6 bar 6: `no_locator` counts as `inferred` only once the measured
# precision of that mapping passes. A code constant, never an env flag.
INFERRED_FROM_NO_LOCATOR = False

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
    if status == "no_locator":
        return ("inferred" if INFERRED_FROM_NO_LOCATOR else None), basis
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
