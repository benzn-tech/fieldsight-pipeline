# Multi-unit walk: five defects from the ElevenLabs pre-roof walk (2026-11-13)

Parent spec: `2026-11-03-multi-unit-inspection-design.md` (rulings R1-R52).
Found by: the 4-speaker ElevenLabs pre-roof walk injected on TEST on 2026-11-12
(session `sid3657cdef...`, request af88a9db). The audio, transcript and results are
in `Dropbox\fs-test\multi-unit\` (not in git). The release with the walk path is on
prod (#1234, 2026-11-12). These are follow-up fixes on develop.

## Root causes, measured on the stored artefacts

| # | Symptom | Root cause |
|---|---|---|
| D1 | After "Oh, back to unit one, I forgot. ... That's a fail." the next line "Firewall to unit two, blocking and holddowns are done" (said in Unit 3) did not reach Unit 3: ROF-19 stayed not checked. | (a) The walk call carried the back-reference on to the next line, which names no unit. That line is ambiguous by itself, because Unit 1 also has a firewall to Unit 2. The shape sentence says "a finding for 'back to Unit 2' is Unit 2" but never says where the back-reference ends. (b) The extraction call put "back to unit one" in the inspection's `resumed` list. `resumed` means coming back to a CHECK after another check. Here it merged harmlessly into the open segment. On a day with two checks it would open a new segment of the walk at the wrong moment. |
| D2 | "Single story, no steel, nothing specifically engineered in this unit" gave N/A for ROF-31 only. ROF-29 (joists, if upper floor) and ROF-30 (steel, if steel beams) were dropped with the reason "evidence not in the transcript". | The model quoted two words per item ("Single story", "no steel"). `checklist.MIN_EVIDENCE_WORDS` is 3, so they failed. The 3-word quote "Single story, no steel" passes (checked against `stt.txt`). The reason given is wrong: the words ARE in the transcript; the quote was too short. |
| D3 | After the on-site answer "that was Unit 2", the checklist moved the bracing-ply No to Unit 2. The model-written defects section still said "in unit three". | The regeneration calls the main model again with the same transcript. The on-site answers are laid only over the checklist (`walk_answers_overlay.apply`), after the main call. The prose never sees them. |
| D4 | Unit 3 ROF-15: Sarah (not the wearer) said "Bottom chord restraints are all in here, trust me"; Mike said "Hmm." The final check counted it as covered and did not ask. The report left it not checked. | The final check and the report run the same shape sentence on different calls. No rule covers a claim the inspector does not confirm, so each call decided its own way. |
| D5 | The 13 site-wide items said before Unit 1 went to General. Every unit row for them stayed not checked (20 of the 59 final-check questions). Unit 2's "everything else in this one is all good" filled the silt fence Yes, while General said it was down. | `walk_checklist.build` keeps General apart from the units (R-General: "a finding that names no unit goes to General, never guessed into a unit"). Nothing links a General answer to the same item in each unit, so the unit row counts as not checked, and ALL fills it. |

Seen and left out of scope: ALL fills conditional items ("if membrane roof") Yes in
Unit 2 (observation 6). Action owners show as "Speaker" (no voiceprints). ASR wrote
"cord" for "chord".

## Baseline (measured before any change)

5 replays of the walk call on the stored transcript, in the same format the worker
sends (`transcript_window.assemble` lines, `walk_report.prompt`; script
`scratchpad/preroof/replay.py`, not in git):
- D1: Unit 3 item 42 (firewall) absent 5/5. Unit 1 keeps its own firewall line, so
  this is not "answered twice". The model leaves the row out.
- D2: Unit 1 items 39/40 quoted as "Single story" / "no steel" in 4/5 (dropped). The
  full clause appears in 1/5.
- D4: Unit 3 item 33 answered Yes from "trust me" in 4/5 and absent in 1/5. The report
  on TEST was that 1/5. The final check's answer was the 4/5.

## Decisions (Fable review 2026-11-13: D1-D4 approved with changes, D5 rework)

### D1: a back-reference covers the findings said with it

- Walk shape sentence: after "a finding for 'back to Unit 2' is Unit 2", add
  "-- that covers only the findings said with the back-reference; a later finding that
  names no unit is about the unit he walked into last." This does not contradict
  "not the unit he happens to be standing in", which is about a finding that NAMES a
  unit.
- Extraction prompt, UNITS bullet: "'Back to Unit 2' inside a walk is not `resumed`
  either: `resumed` is only for coming back to a CHECK after a different check."
- Whether the omission is carry-over or treated as a duplicate is not known. The
  replay decides: pass = Unit 3 item 42 present in at least 4/5 (baseline 0/5).

### D2: short evidence

- Both shape sentences: "Evidence is at least three words: the whole clause that
  answers the item."
- A quote under MIN_EVIDENCE_WORDS words gets the reason `"evidence too short"`, in
  `checklist.rebuild`, `walk_checklist._general` and `walk_checklist._all_good`. The
  threshold stays 3.
- Pass = items 39/40 kept in at least 4/5 (baseline 1/5).

### D3: on-site moves and overrides reach the prose on regeneration

- The answers already arrive in the request artifact (`walk.answers`, built by
  `lambda_org_api._walk_answers_for`; read by `lambda_session_report._walk_answers`).
  `walk` is read before `render_prompt`, so no new query is needed.
- Only answers that contradict the recording go in: moves (`move_to_unit` set) and
  overrides (`asked_kind == "confirm_no"` with an answer). Fills of not-checked items
  are not corrections and stay out.
- The block sits inside the transcript data region of the main prompt, fenced as
  data: "Corrections the inspector gave on site after the recording. Where they differ
  from the transcript, the correction wins." One line each:
  `<unit> -- <item text>: moved to <unit>` or `<unit> -- <item text>: <answer>`, then
  the spoken comment. At most 30 lines of at most 200 chars each.
- Known limit: the block is built before the overlay, so an answer the overlay turns
  into a conflict or leaves unmatched can still steer the prose. That is accepted:
  moves and overrides are explicit choices.
- Long-term alternative, not now: write the defects list in code from `gaps.failed`,
  as the Summary is. That needs a template-level "defects section" marker that does not
  exist yet.

### D4: a claim nobody confirms is not an answer

- Both shape sentences: "A claim answered with doubt, silence ('hmm') or a change of
  subject is not confirmed: leave the item out. A claim counts when the inspector
  agrees, repeats it or checks it."
- The final check's transcript has no speaker labels (`check_done` builds
  `[HH:MM:SS] text`). The report's has `name (spk_N)` labels, and neither names which
  speaker is the inspector. The rule is worded so it works without knowing the speaker:
  it keys on the reply, not on who made the claim. The final check stays speaker-blind.
- Pass = Unit 3 item 33 absent in at least 4/5 on BOTH the walk call and the
  final-check prompt (`check_done._prompt(walk=True)` on the same turns without speaker
  labels). Baseline: 1/5 on the walk call.

### D5: site-wide answers -> separate spec

Moved to its own spec, not tonight. The review found that it needs a new ruling, not
just code:
- a selector: which General answers are site-wide. A finding that names no unit is
  not automatically site-wide (parent spec: "never guessed into a unit"). Candidate:
  rows whose evidence falls before the first `entered` mark. The final check has no
  stretches, so it needs another way to place it;
- `_general` must keep the item number and answer (it keeps only comment and evidence
  today); duplicates and a General ALL need rules;
- five functions change: `_unit_section` (link before `_apply_all_good`), `summary`,
  overlay `apply`/`_draw`/`_recount`/move check, `check_done.walk_questions` (line
  ~370 reads back every No), `_walk_result` (`failed`), with a new row state.

## Not changing

- Unit stretches, walk keys, the migration, the API shapes, the device contract
  (`docs/contracts/2026-11-03-multi-unit-final-check.md`).
- Ordinary (single-unit) checks: only the two sentence additions in D2/D4 and the
  "evidence too short" reason. The snapshot test for the non-walk report changes
  only in the prompt text.

## Verification

1. Unit tests: prompt sentences present in both shape sentences and the extraction
   prompt; "evidence too short" in the three places; the D3 block (only moves and
   overrides, capped, fenced, absent when there are no answers, absent for ordinary
   checks).
2. Replays, 5 each, against the pass criteria above (walk call for D1/D2/D4, final-check
   prompt for D4).
3. TEST after deploy: re-inject the same EL wav as a new session, run the final check
   and the same 13 answers, regenerate. Expected: Unit 3 ROF-19 Yes; Unit 1 ROF-29/30
   N/A; the defects prose names Unit 2 for the bracing ply; the final check asks Unit 3
   ROF-15. The D5 symptoms are still expected.
