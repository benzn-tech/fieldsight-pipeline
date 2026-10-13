# Naming asks "which Ben Lin?" instead of silently making another (design, 2026-10-01)

Item 3 of the Voices same-name work (items 1, 4, 2: `2026-10-01-voices-identity-and-merge-design.md`).

## Problem

`POST /sessions/{id}/speaker-corrections` picks the profile a name attaches to with
`upsert_profile`'s lookup: linked user first, then an EMPTY unlinked same-name profile, else
(name, whoever vouched). When none matches it INSERTs — silently. That is how TEST got two
live "Ben Lin" (one named by Ben_UCPK2 on 08-28, one linked on 09-30). The lookup is right to
refuse to guess between two same-named people; what is wrong is that nobody is asked.

## Rule

Ask only when the answer is not already certain:
- 0 live profiles with that name → no question (a new one is made, as today).
- exactly 1, and the lookup would use it → no question.
- otherwise (≥ 2, or 1 that the lookup would NOT use, i.e. a duplicate is about to be
  made) → ask: "Is this one of these, or someone else with the same name?"

## API

`GET /api/org/voiceprints/same-name?name=Ben%20Lin` — anyone allowed to name a speaker in
this company (`_CORRECTION_ROLES`, or the folder owner: so `user` query param is accepted
and checked the same way speaker-corrections checks it). 404 when identity is off.
Returns `{"profiles": [...], "wouldUse": id | null, "ask": bool}`; each profile:
`{id, displayName, linkedAccount, heardOn, firstNamed, employer, lastHeard}` (the
listing's identity fields + `lastHeard` = latest `speaker_turn_names.created_at` for it).
Match is case- and whitespace-insensitive on `display_name`, live (`status <> 'withdrawn'`)
only, company-scoped. `wouldUse` is computed by the SAME lookup `upsert_profile` runs
(extract it into one function both call — never two copies), with the caller as asserter
and the name's directory resolution as user. No writes.

`POST …/speaker-corrections` gains two optional, mutually exclusive fields:
- `voiceprint_id`: "it is this one". Must be a live, consented profile of the caller's
  company (else 400). Used instead of the lookup. Employer still updates when sent. Does
  NOT link an account (two profiles linked to one user is worse than none).
- `new_person: true`: "someone else with this name". Always INSERTs (lookup skipped).
  Links the resolved directory user only if that user has no live profile already.
Both absent → today's behaviour exactly.

Also: accepting a name proposal and retrying a refused voice both already concern ONE
known profile — they now pass its `voiceprint_id`, so they can never land on a different
same-named profile.

## UI

In the naming panel, after the name is typed and before the correction is sent: call
same-name; if `ask`, show a chooser — one option per profile with its identity line
("Linked to … · Heard on … · last heard 30 Sep") plus "Someone else called Ben Lin", which
offers to add something that tells them apart (prefilled "Ben Lin ()"); a changed name
re-runs the check. Cancel sends nothing. If the check fails, say so and do not send.
Plain words, no scores.
