# retrieval-eval — a fixed question set for measuring search and Ask

Design and rules: `docs/superpowers/specs/2026-09-15-retrieval-eval-baseline.md`.

This is an **evaluation tool, not pipeline code** — nothing here is imported by a Lambda.
It exists so that a change to embeddings, chunking, reranking or prompts can be judged
against the same questions every time, instead of against whoever last tried a query.

## What is in here

| Path | What it is | Changes how |
|---|---|---|
| `gold/v1.draft.jsonl` | Questions, each anchored to the words that answer it | Append a new version; never edit a published one |
| `corpus/manifest.v1.json` | The exact transcript files the questions were written against, with sha256 | Never edited; a new corpus is `manifest.v2.json` |
| `run.py` | Scores the production search path against a gold file | Code |
| `results/` | One JSON per run, carrying the configuration it measured | Appended by `run.py` |

## Running it

```bash
export AWS_PROFILE=fieldsight-deployer AWS_DEFAULT_REGION=ap-southeast-2
export DASHSCOPE_API_KEY=...        # only the query embedding is called
python tools/retrieval-eval/run.py \
  --gold tools/retrieval-eval/gold/v1.draft.jsonl --label baseline --repeat 2
```

Read-only: it queries the database through the RDS Data API and calls DashScope for the
query embedding. It never invokes a Lambda, never calls a chat model and never writes to
S3 or the database.

**Always keep `--repeat 2` or higher.** A difference between two configurations means
nothing until you know how far one configuration moves against itself.

## Reading a result

Three numbers per question, each a 1-based rank or `null`:

- `raw_top30_rank` — did vector search return the evidence at all. The ceiling.
- `ask_context_rank` — is it among the 5 chunks Ask gives the model.
- `search_list_rank` — is it visible in the rows the search box shows.

A question whose evidence is in `raw` but not in `search_list` is lost by the
aggregation step, not by retrieval. That distinction is the point of reporting all three.

## Writing a gold item

```json
{"id":"v1-07","status":"draft","lang":"en","date":"2026-09-10",
 "question":"Why did the crane methodology have to change?","expect":"answerable",
 "evidence":[{"time":"10:07:18","quote":"So we're actually right on the, um, flight path.",
              "match_any":["flight path"]}],
 "note":""}
```

- **Anchor to words, never to ids.** `match_any` is matched against chunk text. Chunk ids,
  topic ids and report fields all change when the pipeline is re-run; the sentence does not.
- **Write patterns the ASR will actually produce.** Copy them from the transcript, not from
  memory of what was said — "gonna happy" is in the text, "going to be happy" is not.
- **Keep the controls.** `expect: "unanswerable"` items have no evidence. They exist so a
  system that retrieves *something* for every question is not scored as good.
- **`status` stays `draft` until the person who was there has confirmed it.**
- **Tag `category` and `style`.** `category` is commercial / subcontractor / quality /
  programme; `style` lists how the question is deliberately imperfect (`typo`,
  `no-punctuation`, `broken-grammar`, `mixed-language` …). Results are sliced by both.
  Type questions the way people do on site — a set of perfectly phrased questions
  overstates how well search works.
- **Cover evidence that no topic claims.** Transcript windows that do not overlap any
  extracted topic behave differently from linked ones; a set whose evidence all sits
  in linked windows cannot see changes to how unlinked ones are handled.

## The corpus

The source transcripts sit under `transcripts/`, which a bucket lifecycle rule deletes
90 days after creation. `manifest.v1.json` records each file's expiry window. The frozen
copy it describes is not made yet — where it lives, and how it honours a customer's
deletion request, is an open decision recorded in the spec.
