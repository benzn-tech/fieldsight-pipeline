"""A local, offline labelling page for a Jev shadow-eval batch (Track A, Task 10).

Reads `scripts/fixtures/jev_eval/batch/{set}.batch.jsonl` (written by
`sample_batch.py`) and writes ONE self-contained HTML file next to it,
`scripts/fixtures/jev_eval/batch/{set}.html`, opened from disk (`file://`).

The page carries customer text (unmasked, on purpose -- the owner labels the
real thing, not the masked Jev state), so it is NEVER published or committed
(the whole `batch/` directory is gitignored, same as the rest of
`scripts/fixtures/jev_eval/`). It makes NO network request of any kind: no
external script, stylesheet or font tag, and no `http`/`https` URL appears
anywhere in the file (asserted by
`tests/unit/test_jev_eval_label_batch.py::test_page_has_no_http_url`) --
everything is inlined into one `<script>`/`<style>` block.

The page shows the owner exactly the batch's `id` and `display` fields --
never `features`, `baseline`, or `stratum`. That last one is deliberate: the
brief requires the matcher's own score/stratum never reach the labeller, so
this module only ever serialises `{"id", "display"}` per item into the page's
embedded JSON (`build_items_payload`).

Controls: Yes / No / Unsure buttons plus keyboard shortcuts Y / N / U, Back
(key B), a progress bar, and a "Download labels" button that saves
`{set}.labels.json` = `{id: "yes"|"no"|"unsure"}` for every item answered so
far (a partial download mid-session is valid input to `import_labels.py`).
Progress (current index + answers so far) is persisted to `localStorage`,
keyed by `set` + a hash of the batch's id list, wrapped in try/catch so a
private-browsing tab or blocked storage never breaks the page -- only resume
convenience is lost.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# Bootstrap: run directly (`python scripts/jev_eval/label_page.py ...`), the
# repo root is not on `sys.path` (only pytest's `pythonpath = ["src", "."]`
# puts it there) -- without this, the `scripts.jev_eval.sample_batch` import
# below fails with `ModuleNotFoundError`.
_REPO_ROOT = _Path(__file__).resolve().parents[2]
for _p in (_REPO_ROOT, _REPO_ROOT / "src"):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))

import argparse
import hashlib
import json
import sys
from pathlib import Path

from scripts.jev_eval.sample_batch import BATCH_DIR

QUESTION_TEXT = {
    "threads": (
        "Is the LATER topic a follow-up or restatement of the EARLIER one "
        "(same job, same subject)?"
    ),
    "work_class": (
        "Is this conversation about something OTHER than the job (private "
        "life, family, health, unrelated business, testing the device)?\n"
        'Note: answering "Yes" here means the topic is NOT work.'
    ),
}


def load_batch(path: Path) -> list:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def build_items_payload(batch_rows: list) -> list:
    """`{"id", "display"}` per item ONLY -- never `features`, `baseline` or
    `stratum` (the labeller must not see the matcher's score, per the
    brief's ruling on this batch)."""
    return [{"id": row["id"], "display": row.get("display") or {}} for row in batch_rows]


def batch_hash(batch_rows: list) -> str:
    """Stable hash of the batch's id list, used as part of the localStorage
    key so a re-sampled batch (different ids) never resumes stale progress
    for a same-named-but-different set."""
    ids = ",".join(str(row["id"]) for row in batch_rows)
    return hashlib.sha256(ids.encode("utf-8")).hexdigest()[:16]


_LABEL_FIELD_TITLES = {
    "earlier_title": "Earlier topic",
    "earlier_summary": "Earlier summary",
    "earlier_date": "Earlier date",
    "later_title": "Later topic",
    "later_summary": "Later summary",
    "later_date": "Later date",
    "gap_days": "Gap (days)",
    "title": "Title",
    "summary": "Summary",
    "category": "Category",
}


def _safe_json(value) -> str:
    """`json.dumps` with every `</` escaped to `<\\/` (fix wave 3, minor 1) --
    customer text embedded in `display` can legitimately contain the literal
    substring `</script>` (e.g. someone reading it aloud on a recording), and
    without this a summary containing it would close the page's own
    `<script>` block early, breaking the page (and, worse, letting whatever
    text follows `</script>` render as raw, unescaped HTML)."""
    return json.dumps(value, sort_keys=True).replace("</", "<\\/")


def build_html(set_name: str, batch_rows: list) -> str:
    if set_name not in QUESTION_TEXT:
        raise ValueError(f"unknown set: {set_name!r}")

    items = build_items_payload(batch_rows)
    question = QUESTION_TEXT[set_name]
    b_hash = batch_hash(batch_rows)
    field_titles_json = _safe_json(_LABEL_FIELD_TITLES)
    items_json = _safe_json(items)

    # Everything below is inlined -- no <script src=...>, no <link rel=stylesheet
    # href=...>, no fetch()/XHR of any kind. The storage key mixes the set name
    # and the batch hash so two different batches never collide.
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Jev label: {set_name}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    font-family: -apple-system, Segoe UI, Arial, sans-serif;
    max-width: 720px; margin: 2rem auto; padding: 0 1rem;
    background: #fafafa; color: #111;
  }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #1b1b1b; color: #eee; }}
    .card {{ background: #2a2a2a !important; border-color: #444 !important; }}
    button {{ background: #333 !important; color: #eee !important; border-color: #555 !important; }}
  }}
  h1 {{ font-size: 1.1rem; }}
  #question {{ font-size: 1.3rem; margin: 1rem 0; white-space: pre-wrap; font-weight: 600; }}
  .progress-outer {{ background: #ddd; border-radius: 6px; height: 10px; overflow: hidden; margin: 1rem 0; }}
  .progress-inner {{ background: #3b82f6; height: 100%; width: 0%; transition: width .15s; }}
  .card {{
    background: #fff; border: 1px solid #ddd; border-radius: 10px;
    padding: 1.25rem; margin: 1rem 0; min-height: 8rem;
  }}
  .field {{ margin-bottom: .75rem; }}
  .field .label {{ font-size: .8rem; text-transform: uppercase; opacity: .6; letter-spacing: .04em; }}
  .field .value {{ font-size: 1.15rem; line-height: 1.4; }}
  .buttons {{ display: flex; gap: .75rem; margin: 1.25rem 0; flex-wrap: wrap; }}
  button {{
    font-size: 1.05rem; padding: .7rem 1.4rem; border-radius: 8px;
    border: 1px solid #ccc; background: #fff; cursor: pointer;
  }}
  button:hover {{ filter: brightness(0.95); }}
  #yesBtn {{ border-color: #2e7d32; color: #2e7d32; }}
  #noBtn {{ border-color: #c62828; color: #c62828; }}
  #unsureBtn {{ border-color: #ef6c00; color: #ef6c00; }}
  .meta {{ font-size: .85rem; opacity: .7; margin-top: .5rem; }}
  .footer {{ margin-top: 2rem; display: flex; gap: .75rem; align-items: center; flex-wrap: wrap; }}
  .hint {{ font-size: .8rem; opacity: .6; }}
  #doneMsg {{ display: none; }}
</style>
</head>
<body>
<h1>Jev shadow eval &mdash; label batch: {set_name}</h1>
<div id="question"></div>
<div class="progress-outer"><div class="progress-inner" id="progressBar"></div></div>
<div class="meta" id="progressText"></div>

<div class="card" id="card"></div>

<div class="buttons">
  <button id="yesBtn">Yes (Y)</button>
  <button id="noBtn">No (N)</button>
  <button id="unsureBtn">Unsure (U)</button>
</div>
<div class="footer">
  <button id="backBtn">Back (B)</button>
  <button id="downloadBtn">Download labels</button>
  <span class="hint">Progress is saved in this browser only.</span>
</div>
<p id="doneMsg"><strong>All items labelled.</strong> Click "Download labels" to save the file.</p>

<script>
(function () {{
  "use strict";
  var SET_NAME = {_safe_json(set_name)};
  var BATCH_HASH = {_safe_json(b_hash)};
  var ITEMS = {items_json};
  var QUESTION = {_safe_json(question)};
  var FIELD_TITLES = {field_titles_json};
  var STORAGE_KEY = "jev_label_progress_v1:" + SET_NAME + ":" + BATCH_HASH;

  document.getElementById("question").textContent = QUESTION;

  var state = {{ index: 0, answers: {{}} }};
  try {{
    var raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw) {{
      var loaded = JSON.parse(raw);
      if (loaded && typeof loaded === "object") {{
        if (typeof loaded.index === "number") state.index = loaded.index;
        if (loaded.answers && typeof loaded.answers === "object") state.answers = loaded.answers;
      }}
    }}
  }} catch (e) {{ /* localStorage unavailable -- resume convenience only, page still works */ }}

  function saveState() {{
    try {{
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    }} catch (e) {{ /* private browsing / blocked storage -- ignore */ }}
  }}

  function clampIndex() {{
    if (state.index < 0) state.index = 0;
    if (state.index > ITEMS.length) state.index = ITEMS.length;
  }}

  function render() {{
    clampIndex();
    var total = ITEMS.length;
    var doneCount = Object.keys(state.answers).length;
    document.getElementById("progressText").textContent =
      "Item " + Math.min(state.index + 1, total) + " of " + total +
      " (" + doneCount + " answered)";
    var pct = total ? Math.round((doneCount / total) * 100) : 0;
    document.getElementById("progressBar").style.width = pct + "%";

    var card = document.getElementById("card");
    card.innerHTML = "";

    if (state.index >= total) {{
      document.getElementById("doneMsg").style.display = "block";
      card.textContent = "Nothing left to label.";
      return;
    }}
    document.getElementById("doneMsg").style.display = "none";

    var item = ITEMS[state.index];
    var display = item.display || {{}};
    Object.keys(display).forEach(function (key) {{
      var value = display[key];
      if (value === null || value === undefined || value === "") return;
      var wrap = document.createElement("div");
      wrap.className = "field";
      var label = document.createElement("div");
      label.className = "label";
      label.textContent = FIELD_TITLES[key] || key;
      var val = document.createElement("div");
      val.className = "value";
      val.textContent = String(value);
      wrap.appendChild(label);
      wrap.appendChild(val);
      card.appendChild(wrap);
    }});

    var existing = state.answers[item.id];
    if (existing) {{
      var note = document.createElement("div");
      note.className = "meta";
      note.textContent = "Already answered: " + existing;
      card.appendChild(note);
    }}
  }}

  function answer(value) {{
    if (state.index >= ITEMS.length) return;
    var item = ITEMS[state.index];
    state.answers[item.id] = value;
    state.index += 1;
    saveState();
    render();
  }}

  function goBack() {{
    if (state.index > 0) {{
      state.index -= 1;
      saveState();
      render();
    }}
  }}

  function download() {{
    var out = {{}};
    Object.keys(state.answers).forEach(function (id) {{ out[id] = state.answers[id]; }});
    var blob = new Blob([JSON.stringify(out, null, 2)], {{ type: "application/json" }});
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = SET_NAME + ".labels.json";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () {{ URL.revokeObjectURL(url); }}, 1000);
  }}

  document.getElementById("yesBtn").addEventListener("click", function () {{ answer("yes"); }});
  document.getElementById("noBtn").addEventListener("click", function () {{ answer("no"); }});
  document.getElementById("unsureBtn").addEventListener("click", function () {{ answer("unsure"); }});
  document.getElementById("backBtn").addEventListener("click", goBack);
  document.getElementById("downloadBtn").addEventListener("click", download);

  window.addEventListener("keydown", function (ev) {{
    if (ev.target && (ev.target.tagName === "INPUT" || ev.target.tagName === "TEXTAREA")) return;
    var key = ev.key.toLowerCase();
    if (key === "y") answer("yes");
    else if (key === "n") answer("no");
    else if (key === "u") answer("unsure");
    else if (key === "b") goBack();
  }});

  render();
}})();
</script>
</body>
</html>
"""


def write_label_page(set_name: str, batch_dir: Path = BATCH_DIR) -> Path:
    batch_path = batch_dir / f"{set_name}.batch.jsonl"
    if not batch_path.exists():
        raise FileNotFoundError(
            f"missing batch file: {batch_path}; run scripts/jev_eval/sample_batch.py first")
    rows = load_batch(batch_path)
    html = build_html(set_name, rows)
    out_path = batch_dir / f"{set_name}.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", dest="set_name", required=True,
                         choices=tuple(QUESTION_TEXT))
    args = parser.parse_args(argv)
    path = write_label_page(args.set_name)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
