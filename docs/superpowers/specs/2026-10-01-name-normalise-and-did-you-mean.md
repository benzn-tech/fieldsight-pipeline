# Same name, different spelling: normalise case/spacing, suggest near spellings (2026-10-01)

Follow-up to `2026-10-01-naming-asks-which-same-name-person.md`. Owner approved all three.

## 1. Case and spacing never make a new person

`find_existing_profile` compares names exactly, so "ben  lin" missed "Ben Lin" and the
chooser asked a question that has only one sensible answer. Compare on a normalised key:
`lower(btrim(regexp_replace(name, '[[:space:]]+', ' ', 'g')))` in every branch that matches by
name (the empty-unlinked branch and the (name, anchor) branch). Python side uses the same
normalisation (one helper). Case/spacing variants are never two people.

## 2. One spelling per person

When the correction lands on an existing profile (lookup hit, chosen `voiceprint_id`), the
name that travels to the embedder (`correction.display_name` in the artifact) and back in the
response is the PROFILE's display name, not the typed string — so the transcript shows
"Ben Lin", never a mix of "ben lin" / "Ben Lin". `new_person` keeps the typed name (trimmed,
internal whitespace collapsed).

## 3. Near spellings are suggested, never merged

"Benn Lin" / "Ben Linn" may be another person ("Sam Yu" vs "Sam Wu"), so nothing is joined
automatically. `GET /voiceprints/same-name` adds `similar`: live profiles in the company whose
normalised name is NOT equal but within edit distance ≤ 1 (normalised length ≤ 6) or ≤ 2
(longer), each with the same identity fields as `profiles`. Computed in Python over the
company's live named profiles (small set; no DB extension needed).

UI: if `ask` is false and `similar` is non-empty, ask "Did you mean Ben Lin?" — one option per
similar profile (sends `voiceprint_id`, so the name becomes that profile's) and
"No — save as 'Benn Lin'" (sends as today). Cancel sends nothing. `ask` true takes precedence
(the which-one chooser), and its option list may show `similar` under a "Similar names"
heading. Plain words, no numbers.
