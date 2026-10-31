# FieldSight Product Roadmap

> Last updated: 2026-11-01. Only open work is listed; finished items are removed, not
> ticked. History is in git and in `docs/superpowers/specs/`.

## Owner decisions in force (2026-10-26/27)

- **SES production access:** paused until the sending domain is switched.
- **Database least-privilege role:** later; the owner runs each step.
- **Company exit / offboarding** (was "per-company delete"): not deletion. Logins,
  devices and billing stop; the data is kept and can be exported; the company can come
  back. Design in progress.
- **External project managers:** access to another company's site is granted by the
  company that owns the site, per company, not as a global role.
- **Task owner ≠ speaker:** see Speaker identity below.
- **Background agent:** after Track B item continuity is switched on in prod
  (`PROD_DECLARE_CONTINUITY`), build v1: one instruction, run in the background, return
  an action pack (draft changes to existing to-dos plus new ones, each applied by one
  click, every claim cited, web search as a separate tool lane).

## P0 — now

| Item | Difficulty | Why it is hard |
|---|---|---|
| One create modal for all record types; fix the Safety photo upload | medium | a migration (TEST first), a new upload route needing IAM + authorisation, and a pipeline/ui release order |
| Company exit / offboarding | medium-high | covers Cognito, device auth, nightly jobs and external memberships without touching any other company's data |

## P1 — next

| Item | Difficulty | Why it is hard |
|---|---|---|
| Track B: continuity on in prod | low (switch) | owner is finishing the ~50-item review; then `PROD_DECLARE_CONTINUITY` |
| Background agent v1 (see above) | high | verifier pass and citations on every claim; DeepSeek invented 2 of 3 decisions in an earlier test |
| Ask: per-user request cap | low-medium | the cap needs cost data; where to count; what the user sees |
| Record who closed a manually entered issue | low | one migration and one write |
| Stalled-upload recovery on a real device | low (work) | needs a device and forced offline / full-disk cases |
| Custom vocabulary: proper A/B | medium | needs human reference transcripts and a measured noise floor first |
| ElevenLabs voice agent to prod | medium | token-issuing client; cost and first-token limits; owner listens to the TTS before main |

## P2 — valuable, needs a rule or a measurement first

| Item | Difficulty | Why it is hard |
|---|---|---|
| Speaker / owner / company as separate fields | high | prompt and schema change; must be shown not to worsen owner assignment |
| Condition-aware voiceprints | high | too few samples per person per condition; <3 s samples are unreliable; prod still shadow |
| Enrol at site sign-in | medium-high | sign-in flow design; consent in the same act; natural speech, not a read-out |
| Speaker-based opt-out | high | depends on voiceprint accuracy; touches transcripts, reports, search index and vectors |

## P3 — waiting on something else

| Item | Waiting on |
|---|---|
| Site sign-in system integrations (SignOnSite, 1Breadcrumb, calendars) | vendor APIs |
| Voice sign-in on shared devices | only as a backup to QR login |
| Searchable photo content | measure on the retrieval ruler first |
| Per-industry prompts | a second industry customer |
| Face blurring | its own compute environment |
| HTML one-pager report | not scheduled |
| Small cleanups: the spare user pool, `deploy-prod-code.yml`, the old `ui/` copy, bringing S3 notifications under CloudFormation | low, except the S3 one (a CFN-managed notification can overwrite the existing config) |

## Owner follow-ups still open

- Delete `s3://fieldsight-data-509194952652/config/user_mapping.json` and
  `scripts/fargate_downloader.py`, and the GitHub secrets `REALPTT_ACCOUNT`,
  `REALPTT_PASSWORD`, `FARGATE_SUBNET_IDS`, `FARGATE_VPC_ID` (safe since prod deployed
  #1199 cleanly on 2026-10-30).
- Delete IAM roles `fieldsight-lambda-role` and `fieldsight-transcribe-callback-role`
  after a quiet week (their lambdas were deleted 2026-10-31).
- About a month after 2026-10-29: purge the old S3 versions of the deleted folders
  `MPI1`, `Benl1`, `Ben_UCPK_`.

---

## Speaker identity — roadmap (owner decisions, 2026-09-29)

Where the voiceprint work goes next. Ordered by value, not by ease. Items marked
**parked** are decided-for but not scheduled; they stay here so they are not lost.

### Know who is speaking without a rename
- **Candidates narrowed to who is on site today.** Compare a voice against the few
  people signed in, not the whole company. Needs upstream integrations: SignOnSite,
  1Breadcrumb, Outlook/Teams calendar attendees.
- **Enrol at site sign-in.** 15–20 s of natural speech (name, company, trade, today's
  task), not a read-out script; the embedder keeps the cleanest 10 s. Consent is taken
  in the same act.
- **Voice sign-in on shared devices.** Replaces or backs up QR login; devices rotate
  monthly.

### A better library
- **Condition-aware profiles.** Samples clustered by condition (wind, indoor, vehicle,
  phone, language); a turn is scored against each person's nearest condition centroid.

### Using the identity
- **Who said it ≠ who owns it.** Owner ruling 2026-10-26: record the speaker and the
  task owner as separate fields. The owner is whoever the instruction is addressed to
  ("Simon, follow this up" → Simon); only when nobody is named does it fall back to the
  speaker. Also record the company/trade it binds. Keep #1069's prompt contract and
  measure owner assignment before and after. A subcontractor who is not in the library
  is never made the task owner.
- **Variation and safety evidence.** Who instructed extra work; who gave and who
  acknowledged a toolbox talk.

### Privacy and trust
- **Speaker-based opt-out.** A person who opts out once is redacted from every later
  recording automatically, not by a one-off deletion.
- **Self-serve consent page — parked.** The person named gets a link to see, hear and
  withdraw their own voiceprint.
