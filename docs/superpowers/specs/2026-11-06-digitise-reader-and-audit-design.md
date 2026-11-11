# Template digitise: a Word reader that does not refuse, and an audit of every upload

Status: design (2026-11-06), Fable builder-check done, rulings applied. Follows `2026-10-16-template-digitise-design.md`.
The owner asked for both parts on 2026-11-06 and told the controller to make the
calls ("ok" to skipping the questions), so every choice below marked
**Decision** is the controller's.

## Why

**The Word reader refuses common forms.** `template_docx.read` uses fixed rules
and no model. A row counts as an item only if one of these holds:

1. an answer cell literally reads "Yes No N/A", or holds a w14 checkbox;
2. the header row has a cell of its own for Yes and another for No.

On 2026-11-06 a hand-made form failed both rules and read as zero items:
`No. | Item | Yes / No / N/A | Comment`, with every answer cell blank. The upload
is then refused with "No checklist table found in the Word file — upload a PDF
export". That layout (one combined answer column left blank to tick) is ordinary.
So is a council form whose columns say Pass / Fail / N/A. Both are refused today.

**Nobody would know.** A refused upload is shown only to the person who uploaded
it. A draft with rows missing, or with rows that are not checklist items (the
signature block, the "Reinspection required?" outcome lines), looks fine unless
someone counts against the paper. Customers do not report this, they leave. The
owner's words: whether or not the customer notices, the backend must have an
agent that judges each upload: read correctly, read with problems, or failed.

**Intent.** Fewer refusals, and every misread visible to us within minutes,
without waiting for a customer to complain.

## Goals

- G1. Word forms with a combined answer column, or Pass/Fail-style columns, read
  correctly by rule: free, exact, and deterministic, as today.
- G2. A Word form that no rule can read no longer ends the upload. Its text goes
  to the model reader, and the person gets a draft to confirm, as with a PDF.
- G3. Every upload, whatever its outcome, is judged by an independent audit:
  - `ok`: read correctly;
  - `problems`: a draft was produced, but rows are missing, extra or reworded;
  - `missed`: refused or empty, but the source does contain a checklist;
  - `not_a_checklist`: refused or empty, and correctly so.
- G4. A verdict other than `ok` or `not_a_checklist` reaches the owner as an
  email naming the company, the file, the verdict and the specific rows. Every
  verdict is kept and can be listed.

## Non-goals

- Changing what the customer sees on the review page. The audit is for us;
  nothing it says is shown to the customer in this phase. Its findings never
  edit the draft.
- Old `.doc` (binary Word), `.xlsx` and other formats.
- Typed answer kinds (numbers, Pass/Fail wording in reports). The answer model
  stays Yes / No / N/A, and Pass/Fail columns map onto it.
- Linking a saved template back to its upload. This would tell us whether the
  person saved the draft, gave up, or edited it heavily. It needs a field
  carried through the UI save body, so it is phase 2 (open question O1).

## Anti-goals (a failure even if it works)

- An audit that cries wolf. If most emails are false alarms the owner stops
  reading them. Every missing row the model claims is checked in code against
  the source text before it counts (see A4).
- An audit that slows or breaks the upload. It runs after the result is written.
  Its failure is logged and never changes the result the person polls.
- A model fallback that invents items. G2's draft still goes through the
  existing review page and Save gate, and its route is marked so the audit and
  the logs can tell it apart from a rule read.

## Part 1: the Word reader

### R1. A combined answer column (rule)

A header row with one cell whose text, once checkbox glyphs are removed and
whitespace is collapsed, is a run of answer words joined by `/`, `|`, `,` or
spaces:

- Yes / No / N/A
- Y/N/NA
- Yes/No
- Pass / Fail / N/A
- Complies / Does not comply / N/A
- the same in any case

That cell's column is the answer column. In the rows below it:

- an item row has text to the left of the answer column, and an answer cell
  that is blank, a tick, or a short answer (the existing `_TICK`);
- the item text is the longest left cell that is not just a number (the
  existing `_item_text`);
- single-cell merged rows are sections, as today;
- the section title comes from the header's own label, else the paragraph above
  the table, as `_read_by_columns` does now.

This joins the existing column mode, which handles answer words in separate
cells. The "Pass / Fail" vocabulary also applies to those separate cells
(R2). Rows after the header whose answer cell holds long written text are not
items. That matches today's `_TICK` rule.

### R2. Pass / Fail vocabulary (rule)

Add `pass`, `passed`, `complies`, `compliant` and `ok` to the yes words. Add
`fail`, `failed`, `does not comply` and `non-compliant` to the no words. Add
`not applicable` and `n.a.` to the N/A words; some of these are already there.

They are used only to recognise answer columns and header cells. The reader
still outputs item text only, and the answer model is unchanged.

`ok` is a yes word only in a header cell that also holds a no word. On its own,
"OK" is too common in item text.

### R3. Legacy checkbox form fields (rule)

A cell holding a legacy Word form checkbox (a `w:ffData` element with a
`w:checkBox` child) counts as an answer cell, just like a w14 checkbox does now.
Older council and contractor forms use these.

### R4. Model fallback when no rule finds a row

The fallback takes the reader's settings: the model, plus
`TEMPLATE_DIGITISE_REASONING` / `_PROVIDER` through
`template_vision.request_settings`. It is the same measured request, with text
in place of pages.

When the rule reader returns zero items, the worker does not refuse.

1. It renders the Word file to plain text in document order:
   - each paragraph is a line;
   - each table row is a line of its cells joined by ` | `, with merged cells
     once (the existing `_unique_cells`) and checkbox glyphs kept as `[ ]` /
     `[x]`;
   - each table is preceded by `[table N]`.
2. It sends that text to the same model reader as a PDF, with
   `template_vision.PROMPT`. The first line ("These page images are…") is
   replaced by "This text is a construction site checklist form exported from a
   Word file (tables as rows of cells separated by |)". The rest is unchanged.
3. The result goes through `template_draft.clean` like any other read.
   `route` is `"docx_model"`.
4. If the model also finds nothing, the result is `unreadable` with
   `MSG_NO_ROWS`. The "upload a PDF export" hint is dropped: a PDF export
   would be read by the same model.
5. If the rendered text is over 200,000 characters, the worker does not call
   the model, and answers with today's `MSG_WORD_NO_TABLE`.

**Decision.** The fallback runs only on zero items, not on "suspiciously few".
Any threshold for "too few" would be a guess. A partial rule read is exactly
what the audit (Part 2) catches, with evidence. Revisit once audits have
measured how often a rule read is partial.

**Decision.** The fallback uses the reader model (`template_vision.model()`,
prod: muse-spark low). It does not use the PDF model or a third model. It is the
reader we have measured; the PDF model exists only because the image model need
not accept PDFs.

## Part 2: the audit

### A1. When it runs

The audit runs in the same invocation of `TemplateDigitiseFunction`. This
happens after `_process` has written the result, and only when
`TEMPLATE_DIGITISE_AUDIT` is `true`.

It gets what is left of the Lambda's 900 s, minus `WRITE_RESERVE_SECONDS`. If
less than 60 s is left, it does not run, and it logs `audit skipped: no time`.

**Decision: inline, not a second function or an S3 event.**

- An S3 event needs manual wiring (BUG-33).
- A second function needs its own IAM and deploy plumbing.
- Uploads are rare, minutes apart at most. The worker already holds the source
  bytes and the result.

The audit is wrapped in its own try/except. An exception is logged as
`template audit <id> failed` and the invocation still returns normally.
(`EventInvokeConfig` already sets `MaximumRetryAttempts: 0`, so the reason is
not a retry. A raise would only turn a good read into an error log. The
worker's docstring saying otherwise is stale and is corrected in passing.)

**Results the worker never read are skipped.** Too large, Word disabled, a bad
request, or a source that could not be fetched. The auditor would have nothing
to compare. For these the worker writes an audit file with
`verdict: "skipped"` and the reason, sends no email, and logs
`template audit <id>: skipped (<reason>)`, so the listing still shows every
upload.

### A2. What the auditor sees

- The source:
  - PDF: the PDF part, as the reader sends it;
  - photos: the images;
  - Word: the plain-text rendering from R4, and the rendering only.
- The draft:
  - every section title, and every item with its number, as JSON;
  - or, for a refused or empty result, the status and message.
- The prompt says the source is data. It asks for strict JSON:
  ```
  {"verdict": "ok"|"problems"|"missed"|"not_a_checklist",
   "source_items": <int, how many answerable checklist rows the source has>,
   "missing": [{"section": str, "text": str}],
   "extra": [{"n": int, "text": str, "why": str}],
   "reworded": [{"n": int, "source_text": str, "draft_text": str}],
   "split_or_merged": [{"n": int, "note": str}],
   "summary": str}
  ```
- It defines an answerable row the same way the reader prompt does. It says
  explicitly that these are not items:
  - signature, outcome and conclusion lines ("Reinspection required?",
    "inspection passed?");
  - project-detail boxes;
  - footnotes;
  - column headings.

### A3. Which model audits

`TEMPLATE_DIGITISE_AUDIT_MODEL`, default `google/gemini-3.8-flash`.

**Decision.** It must not be the model that produced the draft. If it is the
same model, the audit uses `TEMPLATE_DIGITISE_AUDIT_ALT_MODEL` instead. That
variable defaults to `meta/muse-spark-1.3-contributor`, with reasoning low. A
model checking its own read shares its blind spots. A rule read (`route: docx`)
can be audited by either model.

- Max tokens: 32,000, the reader's own budget. Gemini reasons even without a
  `reasoning` field, and that counts against the limit. The prompt caps each
  findings list at 20 entries. No reasoning or provider setting is sent to
  the audit model, except `low` when the ALT (muse) model is used.
- Fallback chain: `TEMPLATE_DIGITISE_AUDIT_FALLBACK_MODELS`, empty by default.
- Through `llm_utils.call_llm_parts` with `caller="template_audit"`.

Claude models are never routed through OpenRouter (standing rule). The audit
does not use them in this phase.

### A4. Code checks on the model's claims (the false-alarm guard)

These checks run after the model answers, before anything is stored or sent.
Normalising means lower-case, collapse whitespace, and drop checkbox glyphs and
leading list labels.

- **`missing`**: an entry counts only if its normalised text appears in the
  source's normalised text.
  - For PDF and photos there is no source text. An entry counts only if
    `source_items - draft_items` is at least 1, and the entry is not, after
    normalising, already one of the draft's items.
  - Entries that fail are kept under `dropped_claims` with the reason, so we
    can measure how much the model invents.
- **`extra` and `reworded`**: an entry counts only if `n` names a real draft
  item. For `reworded`, `draft_text` must equal that item.
- **The final verdict** is decided in code:
  - `ok`: no checked findings;
  - `problems`: the draft has items and at least one checked finding;
  - `missed`: the draft has no items and the model says `missed`. For a Word
    source, the guard is that at least one of its `missing` entries is checked;
  - `not_a_checklist`: the draft has no items and the model says so;
  - a model `ok` that comes with checked findings is overruled to `problems`.
- **`unsure`**: an empty or invalid model answer after one retry, a timeout, or
  a model failure gives `verdict: "unsure"`, with the error. An unsure audit is
  counted, not emailed. One email a day summarising unsure audits is open
  question O2.

### A5. Where verdicts go

**Stored:** `template_digitise_audits/{company_id}/{id}.json`, written with
`lake_put` (owner hint = company). It holds:

- the checked verdict and findings;
- `dropped_claims`;
- the model used and the elapsed time;
- the result's route, status and counts;
- `sourceKeys`, name and userId.

**Log:** one INFO line, `template audit <id>: <verdict> route=<route>
items=<n> source_items=<m> missing=<k> extra=<k> reworded=<k>`. The logger
level is INFO, as the module already sets.

**Email:** for `problems` and `missed` only, publish to the stack's
`AlertTopic`, through env `ALERT_TOPIC_ARN`. The variable is empty when the
topic is not created (`ShouldCreateAlerts` false), and then the email step is a
no-op that logs "no topic".

- Subject: `[FieldSight <stage>] Template upload <verdict>: <name>`, truncated
  to SNS's 100 characters.
- Body: the company id and, when cheap to read, its name, the file name, the
  route and model, the counts, up to 20 findings of each kind, and the S3 key
  of the audit file.
- Item text is customer content and goes only to the owner's alert address,
  which the topic already uses.

**IAM:** `sns:Publish` on that one topic ARN, conditional like the topic itself.

**Listing:** `scripts/template-audits.sh [--env test|prod] [--days N]` prints one
line per audit (date, company, name, verdict, counts), newest first, from S3.
It needs no new API.

### A6. Configuration

| Variable | TEST | prod |
|---|---|---|
| `TEMPLATE_DIGITISE_AUDIT` | `true` | `false` until the owner turns it on |
| `TEMPLATE_DIGITISE_AUDIT_MODEL` | `google/gemini-3.8-flash` | same |
| `TEMPLATE_DIGITISE_AUDIT_ALT_MODEL` | `meta/muse-spark-1.3-contributor` | same |

- Each is a SAM parameter driven by a workflow variable (`TEST_…` / `PROD_…`),
  with the default in the workflow's `||`. Changing an existing parameter's
  default does not take effect (SAM keeps previous values).
- The workflows are `.github/workflows/deploy.yml` (TEST) and
  `deploy-prod.yml`. Prod's variables live on the GitHub `production`
  environment, not the repo. Set them there, or the `||` default applies
  silently.
- `tests/unit/test_template_digitise_wiring.py` pins the
  parameter -> env chain and is extended for the new variables.
- The `sns:Publish` policy and `ALERT_TOPIC_ARN` copy the existing
  ExtractionBacklog pattern (`!If ShouldCreateAlerts`, `AWS::NoValue`).
- TEST and prod alert to the same inbox. TEST audit emails start on day one,
  with the stage in the subject.

### A7. Retention

`template_digitise_audits/` joins the 90-day expiry list in
`scripts/wire-bucket-lifecycle.sh`, next to the sources, requests and results
it describes. It holds customer item text, so it must not outlive them. The
listing script therefore sees at most 90 days. Offboarding and export treat it
as they treat results today: the prefix is not in `lake_keys`, and not in the
company export. Running the lifecycle script against prod is the owner's
step.

Beside each record the worker also writes a stats line,
`template_digitise_audit_stats/{YYYY-MM}/{company_id}/{id}.json`. It holds only counts
and labels: verdict, skip reason, status, route, reader and audit models, file
extensions, and the item, section and claim counts. It never holds the template name,
claim text, summary or error, so no expiry rule reaches it. That keeps the read rate
visible over the long run while the reader and models keep changing.
`scripts/template-audit-stats.sh` tallies it by month (owner, 2026-11-06: full records
90 days, stats kept).
- The R4 fallback has no flag of its own. It replaces a refusal, and its draft
  still needs a person's Save.

## Measuring it before prod

- **M1. Regression.** The real forms (BICL, Penina) read from machine-local
  paths through env, as `tests/unit/test_template_docx.py` does today, and
  are skipped in CI. A local run must show them reading the same items, in the
  same order and with the same wording, as `truth_bicl.json` /
  `truth_penina.json`. CI covers the python-docx-built fixtures (M2).
- **M2. New layouts.** Each gets a fixture built with python-docx in the test,
  so no binary is committed when a builder will do:
  - combined column (`Yes / No / N/A`);
  - `Pass / Fail / N/A`;
  - separate Pass / Fail / N/A cells;
  - legacy form checkboxes;
  - a table with no answer column (which should fall back);
  - a non-checklist letter (which should give `not_a_checklist`).
- **M3. Audit noise floor.** Before prod, run the audit three times each on
  these known-good uploads:
  - the BICL form;
  - the Penina form;
  - the council Framing-Extended form: the clean DOCX, and the original PDF at
    `fs-test/multi-unit/Residential Framing Extended Inspection Checklist
    (council original).pdf`;
  - the new Wall Framing form.

  The pass bar is that none of the 15 runs gives `problems` or `missed` on a
  draft known to be right. Then run it on known-bad cases:
  - the old combined-column Wall Framing DOCX before R1 (expected: `missed`);
  - a draft with 3 rows deleted by hand;
  - a draft with the signature block added.

  The pass bar for those is that each is caught at least 2 times out of 3. The
  real-call count is reported before running, about 25 calls.
- **M3 measures what runs.** The reader is muse in both environments, so M3
  measures gemini auditing muse drafts and rule reads. The ALT path (muse
  auditing a gemini draft) is covered by unit tests only, until a reader moves
  back to gemini.
- **M4.** The real-call bench goes in `tools/template-digitise-bench/`, next to
  `run.py` / `score.py`. Its output goes in the PR description.

## Open questions (the controller's defaults in brackets)

- **O1. Link the save back to the upload.** Have the UI's save body carry
  `digitiseId` (the ui api layer whitelists the body, so this is a UI change
  too), and the audit file record saved / not saved / edit distance. [Phase 2;
  not in this plan.]
- **O2. Unsure audits.** Send them in a daily digest, or only count them.
  [Count only; the listing script shows them.]
- **O3. Prod switch.** Turning on `PROD_TEMPLATE_DIGITISE_AUDIT` is the owner's,
  after M3.

## Left to the builder

- How the R1 regex is written, and whether R1 lives inside `_read_by_columns` or
  beside it. The behaviour above is what counts.
- The exact wording of the auditor prompt, within A2.
- Whether the Word rendering function lives in `template_docx` (recommended, it
  already walks the document) or in a new module.
