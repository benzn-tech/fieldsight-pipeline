# One folder key — a person's recording folder is minted once, by one rule (design, 2026-10-05)

Status: owner-approved 2026-10-05 ("同意，开始"). Fits roadmap decision D4 (identity is the
login, never a name); sub-project 4 later replaces folder-keyed lake paths entirely.

## The incident

2026-10-05: Deandre recorded ~45 min. Audio, transcripts and the extraction reached S3; the
web showed nothing. item-writer raised `folder 'Deandre__Alberts' has no directory row` 21
times. His directory first name is `Deandre'` (the apostrophe is wanted on screen), so
`_free_folder_name` stored `Deandre'_Alberts`; the upload route builds the key with
`_safe_seg(folder_name)`, which turns `'` into `_` → `users/Deandre__Alberts/...`. Two
cleansers in one Lambda disagree. Any name with `'`, `é`, or non-Latin script hits it.

The item-writer error alarm fired twice (15:38, 16:21) and published to
`fieldsight-prod-pipeline-alerts` — which has **zero subscriptions**. Every prod alarm
notifies nobody.

## Decisions

1. **One function mints every folder key**: `folder_key(display_name) -> str | None`
   (`src/folder_key.py`). Output alphabet is exactly what the upload route keeps:
   `[A-Za-z0-9._-]`. Steps: strip; NFKD + drop combining marks (`é`→`e`); drop `'` and `’`
   (`O'Brien`→`OBrien`); every other char outside the alphabet → `_` (whitespace → `_`, so
   `Ben Lin`→`Ben_Lin`, unchanged from today). If the result contains no `[A-Za-z0-9]`
   (e.g. a CJK name), return None and the caller uses the id fallback `u_<user_id[:8]>`.
   Used by `_free_folder_name`, `patch_member_folder`, `_enrol_folder_on_upload`, and
   `users.upsert_field_only_user`'s callers. Display names are never altered.
2. **The upload route never re-cleanses.** It uses `users.folder_name` verbatim; if
   `_safe_seg(folder) != folder` it refuses (422 -- not 409, which the same route uses for a duplicate key -- `"recording folder is invalid; ask an admin"`)
   and logs ERROR — a loud refusal on a retryable route beats a recording written under a
   folder no one owns.
3. **The database refuses a bad key**: migration `0081` adds
   `CHECK (folder_name IS NULL OR folder_name ~ '^[A-Za-z0-9._-]+$')`. Audited 2026-10-05:
   prod 32/32 and TEST 17/17 members comply (after Deandre's fix).
4. **A stranded recording is noticed**: a log metric filter on `has no directory row` over
   the item-writer and ingest log groups, its own alarm ("recording stranded: folder not in
   directory"), and the alert topic's email subscription becomes a standalone
   `AWS::SNS::Subscription` resource. CloudFormation creates it once and does not repair it
   on later deploys; what changes is that it is a first-class resource in the stack
   (visible, and recreated if the resource is replaced or removed and re-added). The owner
   must confirm the AWS email on prod and on TEST (TEST's inline subscription is replaced
   by a new, pending one).
5. **One-command recovery**: `scripts/replay_extractions.py --env prod --folder F --date D
   [--apply]` lists `extractions/F/D/*.json` and, only with `--apply`, re-invokes the
   item-writer with the S3 event the bucket would have sent; prints each result.

## Out of scope
`lambda_orchestrator.safe_name` (idle, frozen-mapping path — sub-project 2 retires it);
renaming existing folders (all comply).
