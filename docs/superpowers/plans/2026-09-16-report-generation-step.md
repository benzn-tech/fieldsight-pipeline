# Report Generation Step Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A report request that names a template comes back as a written document — prose sections shaped by the template, an action list taken from extraction rather than invented — instead of the topic list pasted into Word.

**Architecture:** The in-VPC org-api cannot reach a model, so generation belongs to the existing S3-triggered `session-report` worker, which is non-VPC and already owns the async contract (202 → poll → presigned docx). org-api adds `generate: {templateId, templateVersion}` and the excluded spans to the request artifact; the worker selects the window's transcript objects, cuts the excluded spans out, renders the template into one prompt, makes one model call, and renders the returned prose into a document. Everything on this path is additive: an artifact without `generate` behaves exactly as it does today.

**Tech Stack:** Python 3.12 Lambda (SAM), `llm_utils.call_llm`, `transcript_utils`, `python-docx` via the existing Docx layer, Aurora (read-only here), S3.

**Spec:** `docs/superpowers/specs/2026-09-15-reports-over-any-stretch-of-the-day-design.md` — §5.3 (generation and clipping), §6.1 (the built-in template, adopted 2026-09-16), §7 (risks).

## Global Constraints

- Backend repo `fieldsight-pipeline`, base branch `develop`. Work in a fresh worktree off `origin/develop`; on this machine `git -C` needs `C:/` paths, not `/c/...`.
- Windows + `core.autocrlf=true`: stage explicit paths only. **Never `git add -A`.**
- Tests: `export UV_LINK_MODE=copy AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=ap-southeast-2` then `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy --with tzdata pytest tests/unit -q` from the worktree root.
- Code comments, commit messages and PR bodies in English.
- **The worker is non-VPC and must stay that way** (it needs the model over HTTPS). It must not import `psycopg` or anything that transitively does.
- **`SessionReportFunction` has `Timeout: 300`.** `llm_utils` retries up to 4 times with `HTTP_TIMEOUT` (default 150 s), which can outlive the function. Every call from this path passes `deadline=` and sets `LLM_HTTP_TIMEOUT`.
- **A generated document never invents an owner or a date.** The action list is rendered from the artifact's action items (`owner`, `deadline`), not from model prose.
- **Clipping fails closed** (§5.3): if an excluded topic's `time_range` cannot be parsed, the request fails with an error result; it does not generate from an unclipped transcript.
- An artifact without a `generate` key produces exactly today's document. No existing test may change its expectations.
- Times are the device's wall clock. Nothing here converts time zones or reads `topics.occurred_at`.
- End every commit message with:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01N2ZDWwHWVd7mH4NNKX1kjt
  ```

## File Structure

| File | Responsibility |
|---|---|
| `src/report_templates/personal-meeting.v3.json` (create) | The built-in template, byte-identical to the one committed beside the spec. |
| `src/report_template.py` (create) | Load a built-in template by id + version; render template + scope + action items + transcript into one prompt string. No I/O beyond reading its own package data. |
| `src/transcript_window.py` (create) | Select the transcript objects overlapping a clock window, assemble their turns, and cut excluded spans out. |
| `src/lambda_meeting_minutes.py` (modify) | Add `generate_prose_document` beside `generate_word_document`. The fixed minutes layout stays untouched. |
| `src/lambda_session_report.py` (modify) | When the artifact carries `generate`, build the prompt, call the model, render prose; otherwise today's path. |
| `src/lambda_org_api.py` (modify) | Accept and validate `templateId`/`templateVersion`; write `generate`, `window` and `excludedSpans` onto the artifact. |
| `src/template.yaml` (modify) | Model env vars, `LLM_HTTP_TIMEOUT`, and `s3:GetObject` on `transcripts/*` for the worker. |

---

### Task 1: The built-in template and the prompt it renders

**Files:**
- Create: `src/report_templates/personal-meeting.v3.json`
- Create: `src/report_template.py`
- Test: `tests/unit/test_report_template.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `report_template.load_template(template_id: str, version: int) -> dict` — raises `report_template.TemplateNotFound`.
  - `report_template.render_prompt(template: dict, scope: dict, action_items: list[dict], transcript: str) -> str`
    - `scope` keys: `folder`, `date`, `from`, `to`, `recordings` (int).
    - each action item: `{"action": str, "owner": str|None, "deadline": str|None}`.

- [ ] **Step 1: Write the failing test**

```python
"""The template is data: a section plan plus house style. This test pins what the
renderer turns that data into, because the prompt is the product here -- a silently
dropped section reads as a model failure, not a rendering one."""
import json
import pathlib

import pytest

import report_template


TEMPLATE = {
    "template_id": "personal-meeting",
    "version": 3,
    "name": "Personal Meeting Notes",
    "sections": [
        {"key": "what", "title": "What this was", "purpose": "Who was in it."},
        {"key": "actions", "title": "Actions", "purpose": "One line per action."},
    ],
    "catch_all": {"key": "other", "title": "Anything else", "purpose": "What still matters."},
    "excluded_subjects": [],
    "style": ["One sentence per item."],
}
SCOPE = {"folder": "Ben_UCPK2", "date": "2026-09-10", "from": "09:00", "to": "11:30",
         "recordings": 70}


def test_the_builtin_template_is_the_one_the_owner_adopted():
    tpl = report_template.load_template("personal-meeting", 3)
    assert tpl["version"] == 3
    assert [s["key"] for s in tpl["sections"]] == ["what", "decided", "open", "actions"]
    assert tpl["catch_all"]["key"] == "other"
    assert tpl["style"], "v3 carries house style; without it the record runs long"


def test_an_unknown_template_is_refused_not_defaulted():
    with pytest.raises(report_template.TemplateNotFound):
        report_template.load_template("personal-meeting", 99)
    with pytest.raises(report_template.TemplateNotFound):
        report_template.load_template("../secrets", 3)


def test_every_section_reaches_the_prompt_in_order_with_its_purpose():
    p = report_template.render_prompt(TEMPLATE, SCOPE, [], "[09:00:00] Ben: morning")
    assert p.index("### What this was") < p.index("### Actions") < p.index("### Anything else")
    assert "Who was in it." in p
    assert "## House style" in p and "One sentence per item." in p


def test_the_action_list_is_given_as_data_not_left_to_the_model():
    items = [
        {"action": "Chase the H1 statement", "owner": "Ben", "deadline": "2026-09-12"},
        {"action": "Send the roofing prices", "owner": None, "deadline": None},
    ]
    p = report_template.render_prompt(TEMPLATE, SCOPE, items, "[09:00:00] Ben: morning")
    assert "Chase the H1 statement" in p and "Ben" in p and "2026-09-12" in p
    assert "no owner recorded" in p and "no date" in p
    assert "Do not invent an owner or a date" in p


def test_no_placeholder_survives_rendering():
    p = report_template.render_prompt(TEMPLATE, SCOPE, [], "[09:00:00] Ben: morning")
    head = p.split("## The recording")[0]
    assert "{" not in head and "}" not in head


def test_excluded_subjects_become_a_leave_out_instruction():
    tpl = dict(TEMPLATE, excluded_subjects=[{"label": "commercial", "covers": "rates, margin"}])
    p = report_template.render_prompt(tpl, SCOPE, [], "x")
    assert "## Leave out" in p and "rates, margin" in p


def test_the_scope_is_stated_so_the_model_knows_what_it_did_not_see():
    p = report_template.render_prompt(TEMPLATE, SCOPE, [], "x")
    assert "Ben_UCPK2" in p and "2026-09-10" in p and "09:00" in p and "11:30" in p
    assert "70" in p
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy --with tzdata pytest tests/unit/test_report_template.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'report_template'`.

- [ ] **Step 3: Copy the template in**

Copy `docs/superpowers/specs/templates/personal-meeting.v3.json` from the spec repo (`C:/Users/camil/fswork/report-scope-spec`) to `src/report_templates/personal-meeting.v3.json`, byte for byte. It is the version the owner adopted; the sample it produced is committed beside it.

- [ ] **Step 4: Write `src/report_template.py`**

```python
"""Templates are data, and this module is the only thing that turns them into a prompt.

A template names sections and says what each is for. It does NOT say how to write
them: the phrasing constraints are what made the old schema-driven report read like
a form (spec 2026-09-15 §6.1). House style -- how long, one sentence per item, what
never to repeat -- rides with the template because it is a house decision, not a
property of this code.
"""
import io
import json
import os
import re

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report_templates")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class TemplateNotFound(Exception):
    """No built-in template with that id and version. Never fall back to another
    one: a report names the template it was written to, and a silent substitution
    would make that name a lie."""


def load_template(template_id, version):
    if not _ID_RE.match(template_id or "") or not isinstance(version, int):
        raise TemplateNotFound("%s v%s" % (template_id, version))
    path = os.path.join(TEMPLATE_DIR, "%s.v%d.json" % (template_id, version))
    if not os.path.isfile(path):
        raise TemplateNotFound("%s v%s" % (template_id, version))
    with io.open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _action_lines(action_items):
    out = []
    for a in action_items or []:
        owner = (a.get("owner") or "").strip() or "no owner recorded"
        due = (a.get("deadline") or "").strip() or "no date"
        out.append("- %s | owner: %s | when: %s" % ((a.get("action") or "").strip(), owner, due))
    return out


def render_prompt(template, scope, action_items, transcript):
    """One prompt: what this recording is, the section plan, the house style, the
    action items as DATA, and the transcript."""
    sections = []
    for s in list(template.get("sections") or []) + [template["catch_all"]]:
        sections.append("### %s\n%s" % (s["title"], s["purpose"]))

    leave_out = ""
    if template.get("excluded_subjects"):
        covers = "; ".join(e["covers"] for e in template["excluded_subjects"])
        leave_out = (
            "\n## Leave out\n"
            "Do not report on: %s.\n"
            "If a stretch was mostly about that, leave it out entirely, including the "
            "final section. Do not summarise it under another heading.\n" % covers)

    style = ""
    if template.get("style"):
        style = "\n## House style\n" + "\n".join("- " + s for s in template["style"]) + "\n"

    lines = _action_lines(action_items)
    if lines:
        actions = (
            "\n## The actions already on record\n"
            "These were captured from this recording. Write them into the Actions "
            "section using the owner and date given here, one line each, as\n"
            "**Owner** - what they will do - *when*.\n"
            "Where the owner reads 'no owner recorded' or the date reads 'no date', "
            "write it that way. **Do not invent an owner or a date**, and do not add "
            "actions that are not in this list.\n\n" + "\n".join(lines) + "\n")
    else:
        actions = (
            "\n## The actions already on record\n"
            "None were captured from this recording. Write \"Nothing here.\" under the "
            "Actions heading. **Do not invent an owner or a date** and do not invent "
            "actions from the transcript.\n")

    return (
        "## The recording\n"
        "Site folder: {folder}\n"
        "Date:        {date}\n"
        "Window:      {frm} - {to}   ({n} recordings)\n"
        "\n## Sections\n"
        "Write one section for each heading below, in this order, using these headings\n"
        "exactly as written. The note under each heading says what that section is for.\n"
        "It is a description of purpose, not a format and not a list of fields.\n\n"
        "{sections}\n"
        "{leave_out}{style}{actions}"
        "\n## How to write it\n"
        "- Plain sentences. Write the way you would tell a colleague who has just got\n"
        "  back what happened. Short paragraphs; a list only where the thing is a list.\n"
        "- Use people's names where the recording makes clear who said or did something.\n"
        "  Labels like spk_0 are the transcription provider's own, assigned per API call\n"
        "  and not carried between calls. They are not identities. Never print them.\n"
        "- If a section has nothing behind it, write \"Nothing here.\" Do not pad it.\n"
        "- Say only what the recording supports. Where a figure or date was spoken as\n"
        "  provisional, say so alongside it.\n"
        "- Report the work. Do not quote swearing or personal remarks about people.\n"
        "\n## The recording\n{transcript}\n"
    ).format(folder=scope["folder"], date=scope["date"], frm=scope["from"], to=scope["to"],
             n=scope["recordings"], sections="\n\n".join(sections), leave_out=leave_out,
             style=style, actions=actions, transcript=transcript)
```

- [ ] **Step 5: Run the test**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy --with tzdata pytest tests/unit/test_report_template.py -q`
Expected: PASS (7 tests).

- [ ] **Step 6: Revert-check**

Delete the `style` block from `render_prompt` (return `style=""`), confirm `test_every_section_reaches_the_prompt_in_order_with_its_purpose` fails, restore, confirm PASS.

- [ ] **Step 7: Commit**

```bash
git add src/report_templates/personal-meeting.v3.json src/report_template.py tests/unit/test_report_template.py
git commit -m "A template is a section plan, and this renders it into one prompt"
```

---

### Task 2: Selecting and assembling a window of transcript

**Files:**
- Create: `src/transcript_window.py`
- Test: `tests/unit/test_transcript_window.py`

**Interfaces:**
- Consumes: `transcript_utils.compute_segment_base_time(filename) -> datetime|None`, `transcript_utils.extract_vad_metadata_from_filename(filename) -> dict` (key `segment_duration`), `transcript_utils.normalize_transcript(body, filename) -> dict|None`, `transcript_utils.format_turns_for_prompt(normalized, use_absolute_time=True) -> list[str]`.
- Produces:
  - `transcript_window.select_keys(s3, bucket, folder, date, win_from, win_to) -> list[tuple[datetime, str]]` (sorted by start; `win_from`/`win_to` are naive `datetime`).
  - `transcript_window.assemble(s3, bucket, keys) -> list[dict]` — each `{"at": datetime, "line": str}`.

- [ ] **Step 1: Write the failing test**

```python
"""Time is the only axis present on every recording: it comes out of the filename
plus the VAD offset, so this works on recordings that predate sessions and on days
with no session at all."""
import datetime as dt
import json

import transcript_window


BODY = {"results": {"transcripts": [{"transcript": "morning"}],
                    "items": [], "speaker_labels": {"segments": []}}}


class FakeS3:
    def __init__(self, keys):
        self._keys = keys
        self.listed = []

    def list_objects_v2(self, **kw):
        self.listed.append(kw.get("Prefix"))
        page = [{"Key": k} for k in self._keys[:2]] if "ContinuationToken" not in kw else \
               [{"Key": k} for k in self._keys[2:]]
        more = "ContinuationToken" not in kw and len(self._keys) > 2
        out = {"Contents": page, "IsTruncated": more}
        if more:
            out["NextContinuationToken"] = "t"
        return out

    def get_object(self, Bucket=None, Key=None):
        return {"Body": type("B", (), {"read": staticmethod(
            lambda: json.dumps(BODY).encode("utf-8"))})()}


def _k(name):
    return "transcripts/Ben_UCPK2/2026-09-10/" + name


KEY_0858 = _k("Ben_UCPK2_2026-09-10_08-58-00_to180_vad.json")   # ends 09:01 -- straddles
KEY_0930 = _k("Ben_UCPK2_2026-09-10_09-30-00_to120_vad.json")   # inside
KEY_1200 = _k("Ben_UCPK2_2026-09-10_12-00-00_to120_vad.json")   # after


def test_a_recording_straddling_the_start_is_kept_whole():
    s3 = FakeS3([KEY_0858, KEY_0930, KEY_1200])
    picked = transcript_window.select_keys(
        s3, "b", "Ben_UCPK2", "2026-09-10",
        dt.datetime(2026, 9, 10, 9, 0), dt.datetime(2026, 9, 10, 11, 30))
    assert [k for _, k in picked] == [KEY_0858, KEY_0930]


def test_every_page_of_the_listing_is_read():
    s3 = FakeS3([KEY_0858, KEY_0930, KEY_1200])
    transcript_window.select_keys(
        s3, "b", "Ben_UCPK2", "2026-09-10",
        dt.datetime(2026, 9, 10, 0, 0), dt.datetime(2026, 9, 10, 23, 59))
    assert s3.listed == ["transcripts/Ben_UCPK2/2026-09-10/", "transcripts/Ben_UCPK2/2026-09-10/"]


def test_an_unreadable_filename_is_skipped_not_guessed():
    s3 = FakeS3([_k("not-a-recording.json"), KEY_0930])
    picked = transcript_window.select_keys(
        s3, "b", "Ben_UCPK2", "2026-09-10",
        dt.datetime(2026, 9, 10, 9, 0), dt.datetime(2026, 9, 10, 11, 30))
    assert [k for _, k in picked] == [KEY_0930]


def test_assemble_returns_timestamped_lines_in_order():
    s3 = FakeS3([KEY_0858, KEY_0930])
    picked = transcript_window.select_keys(
        s3, "b", "Ben_UCPK2", "2026-09-10",
        dt.datetime(2026, 9, 10, 9, 0), dt.datetime(2026, 9, 10, 11, 30))
    turns = transcript_window.assemble(s3, "b", picked)
    assert turns and all("at" in t and "line" in t for t in turns)
    assert turns == sorted(turns, key=lambda t: t["at"])
```

- [ ] **Step 2: Run it to verify it fails**

Run: `… pytest tests/unit/test_transcript_window.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'transcript_window'`.

- [ ] **Step 3: Write `src/transcript_window.py` (selection and assembly only)**

```python
"""A stretch of a day's recorded speech, assembled from the transcript objects.

Selection is by OVERLAP, not by start: a recording that straddles the start of the
window carries the beginning of the conversation, and dropping it loses exactly the
part a reader needs. Nothing here reads Aurora -- the worker that uses it is
non-VPC and must not import psycopg.
"""
import datetime as dt
import json
import logging
import re

import transcript_utils as tu

logger = logging.getLogger()

_DURATION_RE = re.compile(r"_to(\d+(?:\.\d+)?)_")
_DEFAULT_DURATION_SEC = 60.0


def _duration(name):
    meta = tu.extract_vad_metadata_from_filename(name)
    dur = meta.get("segment_duration") or 0.0
    if dur > 0:
        return float(dur)
    m = _DURATION_RE.search(name)
    return float(m.group(1)) if m else _DEFAULT_DURATION_SEC


def select_keys(s3, bucket, folder, date, win_from, win_to):
    """Keys whose audio overlaps [win_from, win_to), oldest first."""
    prefix = "transcripts/%s/%s/" % (folder, date)
    keys, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        keys += [o["Key"] for o in page.get("Contents", [])]
        if not page.get("IsTruncated"):
            break
        token = page["NextContinuationToken"]

    picked = []
    for key in keys:
        name = key.rsplit("/", 1)[-1]
        start = tu.compute_segment_base_time(name)
        if start is None:
            logger.info("window: no time in %s -- skipped", name)
            continue
        end = start + dt.timedelta(seconds=_duration(name))
        if end > win_from and start < win_to:
            picked.append((start, key))
    picked.sort()
    return picked


def assemble(s3, bucket, picked):
    """One entry per speaker turn: {"at": datetime, "line": "[HH:MM:SS - HH:MM:SS] ..."}."""
    turns = []
    for start, key in picked:
        name = key.rsplit("/", 1)[-1]
        body = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8"))
        norm = tu.normalize_transcript(body, name)
        if not norm:
            logger.info("window: %s did not normalise -- skipped", name)
            continue
        lines = tu.format_turns_for_prompt(norm, use_absolute_time=True)
        for i, line in enumerate(lines):
            if not line or not line.strip():
                continue
            turn = (norm.get("speaker_turns") or [])[i] if i < len(norm.get("speaker_turns") or []) else None
            at = (turn or {}).get("abs_start") or start
            turns.append({"at": at, "line": line})
    turns.sort(key=lambda t: t["at"])
    return turns
```

- [ ] **Step 4: Run the test**

Run: `… pytest tests/unit/test_transcript_window.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Revert-check**

Change the overlap condition to `start >= win_from and start < win_to`; confirm `test_a_recording_straddling_the_start_is_kept_whole` fails; restore; confirm PASS.

- [ ] **Step 6: Commit**

```bash
git add src/transcript_window.py tests/unit/test_transcript_window.py
git commit -m "Assemble the stretch of the day a report is about"
```

---

### Task 3: Cutting excluded speech out before the prompt

**Files:**
- Modify: `src/transcript_window.py` (add to the end)
- Test: `tests/unit/test_transcript_window_clipping.py`

**Interfaces:**
- Consumes: `chunking.parse_time_range(value) -> tuple[int, int] | None` (seconds from midnight).
- Produces:
  - `transcript_window.UnplaceableExclusion` (Exception)
  - `transcript_window.excluded_spans(date, excluded_topics) -> list[tuple[datetime, datetime]]`
  - `transcript_window.drop_spans(turns, spans) -> list[dict]`

- [ ] **Step 1: Write the failing test**

```python
"""Raw transcripts carry speech that is hidden only at topic level: a redacted topic
or one marked non-work. The topic list hides it; the transcript does not. If that
speech reaches the prompt it reaches the customer's report (spec 2026-09-15 §5.3)."""
import datetime as dt

import pytest

import transcript_window as tw


DATE = "2026-09-10"


def _turn(h, m, text="something"):
    return {"at": dt.datetime(2026, 9, 10, h, m), "line": "[%02d:%02d:00] Ben: %s" % (h, m, text)}


def test_a_redacted_topics_minutes_are_cut_out():
    spans = tw.excluded_spans(DATE, [{"id": "t1", "time_range": "10:00 - 10:20"}])
    turns = [_turn(9, 50), _turn(10, 5, "the private part"), _turn(10, 30)]
    kept = tw.drop_spans(turns, spans)
    assert [t["at"].hour * 60 + t["at"].minute for t in kept] == [9 * 60 + 50, 10 * 60 + 30]
    assert not any("private" in t["line"] for t in kept)


def test_a_turn_on_the_boundary_is_cut_not_kept():
    spans = tw.excluded_spans(DATE, [{"id": "t1", "time_range": "10:00 - 10:20"}])
    assert tw.drop_spans([_turn(10, 0), _turn(10, 20)], spans) == []


def test_an_excluded_topic_with_no_usable_time_fails_closed():
    with pytest.raises(tw.UnplaceableExclusion):
        tw.excluded_spans(DATE, [{"id": "t9", "time_range": ""}])
    with pytest.raises(tw.UnplaceableExclusion):
        tw.excluded_spans(DATE, [{"id": "t9", "time_range": "all morning"}])


def test_no_exclusions_keeps_everything():
    assert tw.drop_spans([_turn(9, 0), _turn(10, 0)], []) == [_turn(9, 0), _turn(10, 0)]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `… pytest tests/unit/test_transcript_window_clipping.py -q`
Expected: FAIL — `AttributeError: module 'transcript_window' has no attribute 'excluded_spans'`.

- [ ] **Step 3: Add the clipping code to `src/transcript_window.py`**

```python
class UnplaceableExclusion(Exception):
    """An excluded topic whose time_range cannot be parsed. The stretch it covers
    cannot be cut out, so the request fails: generating from the unclipped
    transcript would put hidden speech in a customer's report."""


def excluded_spans(date, excluded_topics):
    """Wall-clock spans to remove. `excluded_topics` are the day's topics that are
    redacted or non-work -- the ones the report scope already refuses to show."""
    day = dt.datetime.strptime(date, "%Y-%m-%d")
    spans = []
    for t in excluded_topics or []:
        import chunking
        parsed = chunking.parse_time_range(t.get("time_range"))
        if not parsed:
            raise UnplaceableExclusion(
                "topic %s is excluded but its time_range %r cannot be placed"
                % (t.get("id"), t.get("time_range")))
        start_s, end_s = parsed
        spans.append((day + dt.timedelta(seconds=start_s), day + dt.timedelta(seconds=end_s)))
    return spans


def drop_spans(turns, spans):
    """Every turn that starts inside an excluded span goes, boundaries included."""
    if not spans:
        return list(turns)
    kept = []
    for t in turns:
        at = t["at"]
        if any(s <= at <= e for s, e in spans):
            continue
        kept.append(t)
    return kept
```

Move the `import chunking` to the module's import block if the linter objects; it is inside the function above only to keep the import list of this module honest about what the selection half needs.

- [ ] **Step 4: Run both window test files**

Run: `… pytest tests/unit/test_transcript_window.py tests/unit/test_transcript_window_clipping.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Revert-check**

Make `excluded_spans` skip unparseable ranges (`continue` instead of `raise`); confirm `test_an_excluded_topic_with_no_usable_time_fails_closed` fails; restore; confirm PASS.

- [ ] **Step 6: Commit**

```bash
git add src/transcript_window.py tests/unit/test_transcript_window_clipping.py
git commit -m "Cut hidden speech out of the transcript before it reaches a prompt"
```

---

### Task 4: A document made of prose sections

**Files:**
- Modify: `src/lambda_meeting_minutes.py` (add `generate_prose_document` directly above `def generate_word_document`)
- Test: `tests/unit/test_prose_document.py`

**Interfaces:**
- Produces: `lambda_meeting_minutes.generate_prose_document(title: str, subtitle: str, sections: list[dict], actions: list[dict]) -> io.BytesIO | None`
  - each section: `{"title": str, "paragraphs": list[str]}`
  - each action: `{"action": str, "owner": str|None, "deadline": str|None}`
  - returns `None` when python-docx is unavailable, exactly like `generate_word_document`.

- [ ] **Step 1: Write the failing test**

```python
"""The minutes layout is fixed by design. A template-shaped record needs headings it
chooses itself, so it gets its own renderer rather than more branches inside that one."""
import io
import zipfile

import pytest

import lambda_meeting_minutes as mm

pytestmark = pytest.mark.skipif(not mm.DOCX_AVAILABLE, reason="python-docx not installed")

SECTIONS = [
    {"title": "What this was", "paragraphs": ["Ben's site meeting at Waipuna Rise."]},
    {"title": "Still open", "paragraphs": ["Plumbing RFI 217 is outstanding. (Grounding)"]},
]
ACTIONS = [
    {"action": "Raise the flooding item", "owner": "Me", "deadline": "today"},
    {"action": "Send roofing prices", "owner": None, "deadline": None},
]


def _text(buf):
    xml = zipfile.ZipFile(io.BytesIO(buf.getvalue())).read("word/document.xml").decode("utf-8")
    return xml


def test_the_sections_are_the_documents_headings_in_order():
    xml = _text(mm.generate_prose_document("Meeting Notes", "Waipuna Rise", SECTIONS, []))
    assert xml.index("What this was") < xml.index("Still open")
    assert "Discussion Topics" not in xml, "this is not the minutes layout"


def test_an_action_without_an_owner_or_date_says_so_rather_than_inventing_one():
    xml = _text(mm.generate_prose_document("Meeting Notes", "Waipuna Rise", SECTIONS, ACTIONS))
    assert "Raise the flooding item" in xml and "Me" in xml and "today" in xml
    assert "Send roofing prices" in xml
    assert "no owner recorded" in xml and "no date" in xml


def test_a_record_with_no_actions_renders_without_an_actions_table():
    xml = _text(mm.generate_prose_document("Meeting Notes", "Waipuna Rise", SECTIONS, []))
    assert "Actions" not in xml
```

- [ ] **Step 2: Run it to verify it fails**

Run: `… pytest tests/unit/test_prose_document.py -q`
Expected: FAIL — `AttributeError: module 'lambda_meeting_minutes' has no attribute 'generate_prose_document'`.

- [ ] **Step 3: Implement**

```python
def generate_prose_document(title, subtitle, sections, actions):
    """A record whose headings come from its template, not from this function.

    `generate_word_document` below renders the fixed meeting-minutes layout and is
    unchanged: a template-shaped record has its own sections, so it gets its own
    renderer rather than another branch inside that one.
    """
    if not DOCX_AVAILABLE:
        logger.warning("prose document requested but python-docx is unavailable")
        return None

    doc = Document()
    doc.add_heading(title, level=0)
    if subtitle:
        p = doc.add_paragraph(subtitle)
        p.runs[0].italic = True

    for section in sections or []:
        doc.add_heading(section.get("title") or "", level=1)
        for para in section.get("paragraphs") or []:
            text = (para or "").strip()
            if not text:
                continue
            if text.startswith("- ") or text.startswith("* "):
                doc.add_paragraph(text[2:].strip(), style="List Bullet")
            else:
                doc.add_paragraph(text)

    if actions:
        doc.add_heading("Actions", level=1)
        table = doc.add_table(rows=1, cols=3)
        table.style = "Table Grid"
        for cell, head in zip(table.rows[0].cells, ("Action", "Owner", "When")):
            cell.text = head
        for a in actions:
            row = table.add_row().cells
            row[0].text = (a.get("action") or "").strip()
            row[1].text = (a.get("owner") or "").strip() or "no owner recorded"
            row[2].text = (a.get("deadline") or "").strip() or "no date"

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf
```

- [ ] **Step 4: Run the test and the existing minutes tests**

Run: `… pytest tests/unit/test_prose_document.py tests/unit/test_session_report_worker.py -q`
Expected: PASS, and the worker tests unchanged.

- [ ] **Step 5: Revert-check**

Make the owner fallback `""` instead of `"no owner recorded"`; confirm `test_an_action_without_an_owner_or_date_says_so_rather_than_inventing_one` fails; restore; confirm PASS.

- [ ] **Step 6: Commit**

```bash
git add src/lambda_meeting_minutes.py tests/unit/test_prose_document.py
git commit -m "Render a record whose headings come from its template"
```

---

### Task 5: The worker generates when the request asks for it

**Files:**
- Modify: `src/lambda_session_report.py`
- Modify: `src/template.yaml` (the `SessionReportFunction` block)
- Test: `tests/unit/test_session_report_generates.py`

**Interfaces:**
- Consumes: `report_template.load_template`, `report_template.render_prompt`, `transcript_window.select_keys/assemble/excluded_spans/drop_spans/UnplaceableExclusion`, `lambda_meeting_minutes.generate_prose_document`, `llm_utils.call_llm(prompt, max_tokens=…, deadline=…) -> (text, err)`, `llm_utils.active_model()`.
- Produces: result JSON gains `generated: true`, `templateId`, `templateVersion`, `model`, `promptChars`; on failure `status: "error"` with `error`.
- Artifact keys read (written by Task 6): `generate: {"templateId": str, "templateVersion": int}`, `window: {"from": "HH:MM", "to": "HH:MM"}`, `excludedTopics: [{"id": str, "time_range": str}]`.

- [ ] **Step 1: Write the failing test**

```python
"""One model call, inside the worker, because org-api is in-VPC and cannot reach one.
Everything the model is given is already filtered: the actions come from extraction and
the excluded spans are gone from the transcript before the prompt is built."""
import datetime as dt
import json

import pytest

import lambda_session_report as sr


ARTIFACT = {
    "requestId": "r1", "folder": "Ben_UCPK2", "date": "2026-09-10",
    "sessionId": "sid" + "a" * 32, "resultKey": "session_report_results/x.json",
    "title": "Meeting Notes", "deliver": "download",
    "generate": {"templateId": "personal-meeting", "templateVersion": 3},
    "window": {"from": "09:00", "to": "11:30"},
    "excludedTopics": [],
    "content": {"date": "2026-09-10", "participants": ["Ben"], "topics": [
        {"topic_title": "Roofing", "summary": "Xtreme withdrew.", "time_range": "09:10 - 09:20",
         "action_items": [{"action": "Send roofing prices", "responsible": "Alex", "deadline": None}]},
    ]},
}

PROSE = ("### What this was\nBen's site meeting.\n\n"
         "### Actions\n- **Alex** - send roofing prices - *no date*\n")


@pytest.fixture
def wired(monkeypatch):
    calls = {}
    monkeypatch.setattr(sr.transcript_window, "select_keys",
                        lambda *a, **k: [(dt.datetime(2026, 9, 10, 9, 5), "transcripts/x.json")])
    monkeypatch.setattr(sr.transcript_window, "assemble",
                        lambda *a, **k: [{"at": dt.datetime(2026, 9, 10, 9, 5),
                                          "line": "[09:05:00] Ben: roofing"}])

    def fake_call(prompt, **kw):
        calls["prompt"] = prompt
        calls["kw"] = kw
        return PROSE, None
    monkeypatch.setattr(sr.llm_utils, "call_llm", fake_call)
    monkeypatch.setattr(sr.llm_utils, "active_model", lambda **k: "muse-spark-1.3")
    monkeypatch.setattr(sr.lambda_meeting_minutes, "generate_prose_document",
                        lambda *a, **k: __import__("io").BytesIO(b"PK-docx"))
    written = []
    monkeypatch.setattr(sr, "_write_result", lambda key, payload: written.append(payload))
    monkeypatch.setattr(sr, "_put_document", lambda *a, **k: "session_reports/x.docx")
    monkeypatch.setattr(sr, "_session_was_deleted", lambda artifact: False)
    return calls, written


def test_a_request_that_names_a_template_is_generated_not_assembled(wired):
    calls, written = wired
    sr.process_request(dict(ARTIFACT))
    assert written[0]["status"] == "done"
    assert written[0]["generated"] is True
    assert written[0]["templateId"] == "personal-meeting" and written[0]["templateVersion"] == 3
    assert written[0]["model"] == "muse-spark-1.3"
    assert "### What this was" in calls["prompt"] or "What this was" in calls["prompt"]
    assert "Send roofing prices" in calls["prompt"] and "Alex" in calls["prompt"]


def test_the_model_call_is_bounded_so_it_cannot_outlive_the_function(wired):
    calls, _ = wired
    sr.process_request(dict(ARTIFACT))
    assert calls["kw"].get("deadline"), "an unbounded retry ladder outlives Timeout: 300"


def test_an_empty_answer_is_an_error_not_an_empty_report(wired, monkeypatch):
    _, written = wired
    monkeypatch.setattr(sr.llm_utils, "call_llm",
                        lambda prompt, **kw: (None, "empty answer from model (finish_reason=length)"))
    sr.process_request(dict(ARTIFACT))
    assert written[0]["status"] == "error"
    assert "empty answer" in written[0]["error"]


def test_an_unplaceable_exclusion_stops_the_report(wired):
    _, written = wired
    art = dict(ARTIFACT, excludedTopics=[{"id": "t9", "time_range": "all morning"}])
    sr.process_request(art)
    assert written[0]["status"] == "error"
    assert "cannot be placed" in written[0]["error"]


def test_a_request_without_generate_still_assembles_exactly_as_before(wired, monkeypatch):
    _, written = wired
    seen = {}
    monkeypatch.setattr(sr.lambda_meeting_minutes, "generate_word_document",
                        lambda data, title: seen.setdefault("data", data) or __import__("io").BytesIO(b"PK"))
    art = dict(ARTIFACT)
    art.pop("generate")
    sr.process_request(art)
    assert written[0]["status"] == "done"
    assert "generated" not in written[0]
    assert seen["data"]["topics"], "the old path still renders from topics"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `… pytest tests/unit/test_session_report_generates.py -q`
Expected: FAIL — `AttributeError: module 'lambda_session_report' has no attribute 'transcript_window'`.

- [ ] **Step 3: Implement in `src/lambda_session_report.py`**

Add to the imports at the top of the file, beside the existing `import lambda_meeting_minutes`:

```python
import llm_utils
import report_template
import transcript_window
```

Add these two helpers directly above `def process_request(`:

```python
# The function has Timeout: 300 and llm_utils retries up to four times at
# LLM_HTTP_TIMEOUT each, so an unbounded ladder outlives the function and writes no
# result at all -- the poller then spins forever. Bound it well inside the timeout.
GENERATION_BUDGET_SECONDS = float(os.environ.get("GENERATION_BUDGET_SECONDS", "210"))


def _action_items_for_prompt(content):
    """The actions the model is given: extraction's, not its own reading of the
    transcript. Owner and date are already recorded against them."""
    out = []
    for topic in (content.get("topics") or []):
        for a in (topic.get("action_items") or []):
            out.append({"action": a.get("action") or a.get("text"),
                        "owner": a.get("owner") or a.get("responsible"),
                        "deadline": a.get("deadline")})
    return out


def _prose_sections(text):
    """Split the model's markdown back into {title, paragraphs}. Anything before the
    first heading is kept under an empty title rather than dropped."""
    sections, current = [], {"title": "", "paragraphs": []}
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if line.startswith("#"):
            if current["title"] or current["paragraphs"]:
                sections.append(current)
            current = {"title": line.lstrip("#").strip(), "paragraphs": []}
        elif line.strip():
            current["paragraphs"].append(line.strip())
    if current["title"] or current["paragraphs"]:
        sections.append(current)
    return [s for s in sections if s["title"] or s["paragraphs"]]


def _generate_document(artifact):
    """Returns (buffer, meta). Raises on anything that must not produce a document."""
    gen = artifact["generate"]
    template = report_template.load_template(gen["templateId"], int(gen["templateVersion"]))
    content = artifact.get("content") or {}
    date = artifact.get("date") or content.get("date")
    window = artifact.get("window") or {}
    win_from = _clock(date, window.get("from") or "00:00")
    win_to = _clock(date, window.get("to") or "23:59")

    spans = transcript_window.excluded_spans(date, artifact.get("excludedTopics") or [])
    client = boto3.client("s3")
    picked = transcript_window.select_keys(client, S3_BUCKET, artifact["folder"], date,
                                           win_from, win_to)
    turns = transcript_window.drop_spans(
        transcript_window.assemble(client, S3_BUCKET, picked), spans)
    if not turns:
        raise RuntimeError("no recorded speech in this window after exclusions")

    prompt = report_template.render_prompt(
        template,
        {"folder": artifact["folder"], "date": date,
         "from": window.get("from") or "00:00", "to": window.get("to") or "23:59",
         "recordings": len(picked)},
        _action_items_for_prompt(content),
        "\n".join(t["line"] for t in turns))

    text, err = llm_utils.call_llm(prompt, max_tokens=8000,
                                   deadline=time.time() + GENERATION_BUDGET_SECONDS)
    if err or not (text or "").strip():
        raise RuntimeError(err or "empty answer from model")

    buf = lambda_meeting_minutes.generate_prose_document(
        artifact.get("title") or template.get("name") or "Report",
        "%s  %s - %s" % (date, window.get("from") or "00:00", window.get("to") or "23:59"),
        _prose_sections(text),
        _action_items_for_prompt(content))
    meta = {"generated": True, "templateId": template["template_id"],
            "templateVersion": template["version"], "model": llm_utils.active_model(),
            "promptChars": len(prompt)}
    return buf, meta
```

Add the small clock helper beside them:

```python
def _clock(date, hhmm):
    """A wall-clock time on the report's own date. No timezone conversion happens
    anywhere on this path (spec 2026-09-15 global constraints)."""
    return datetime.datetime.strptime("%s %s" % (date, hhmm), "%Y-%m-%d %H:%M")
```

In `process_request`, immediately after the artifact is loaded and the deletion check has passed, and before the existing `_content_to_minutes(...)` call, insert:

```python
    if artifact.get("generate"):
        try:
            buf, meta = _generate_document(artifact)
        except Exception as exc:                      # noqa: BLE001 -- recorded, not retried
            logger.exception("report: generation failed for %s", artifact.get("requestId"))
            _write_result(artifact["resultKey"],
                          dict({"status": "error", "requestId": artifact.get("requestId"),
                                "error": str(exc)}, **_scope_result_fields(artifact)))
            return
        doc_key = _put_document(artifact, buf)
        _write_result(artifact["resultKey"],
                      dict({"status": "done", "requestId": artifact.get("requestId"),
                            "docKey": doc_key, "emailed": False}, **meta,
                           **_scope_result_fields(artifact)))
        return
```

If `_put_document` does not exist under that name in this file, use the same call the existing path uses to put the docx and return its key, and keep the name in the test in step 1 aligned with it.

- [ ] **Step 4: Add the worker's model environment and transcript read in `src/template.yaml`**

In the `SessionReportFunction` `Environment.Variables` block, beside `S3_BUCKET`, add the same keys `ReportGeneratorFunction` already carries, plus the two this path needs:

```yaml
          LLM_PROVIDER: !Ref LlmProvider
          LLM_REASONING_EFFORT: !Ref LlmReasoningEffort
          LLM_TEMPERATURE: !Ref LlmTemperature
          ANTHROPIC_API_KEY: !Ref AnthropicApiKey
          CLAUDE_MODEL: !Ref ClaudeModel
          QWEN_API_KEY: !Ref QwenApiKey
          QWEN_BASE_URL: !Ref QwenBaseUrl
          QWEN_MODEL: !Ref QwenModel
          QWEN_MODEL_NONTHINKING: !Ref QwenModelNonThinking
          QWEN_ENABLE_THINKING: !Ref QwenEnableThinking
          # One attempt must fit inside Timeout: 300 with room for the docx render.
          LLM_HTTP_TIMEOUT: '120'
          GENERATION_BUDGET_SECONDS: '210'
```

Use the exact parameter names `ReportGeneratorFunction` uses; if a name differs, copy that block's line verbatim rather than inventing a parameter.

In the same function's policies, add the read it does not have today:

```yaml
            - Effect: Allow
              Action: s3:GetObject
              Resource: !Sub 'arn:aws:s3:::${IngestBucketName}/transcripts/*'
            - Effect: Allow
              Action: s3:ListBucket
              Resource: !Sub 'arn:aws:s3:::${IngestBucketName}'
              Condition:
                StringLike:
                  s3:prefix: 'transcripts/*'
```

- [ ] **Step 5: Run the generation tests and every existing worker test**

Run: `… pytest tests/unit/test_session_report_generates.py tests/unit/test_session_report_worker.py tests/unit/test_session_report_worker_day_scope.py tests/unit/test_session_report_skips_deleted.py tests/unit/test_session_report_topic_selection.py -q`
Expected: PASS, with the pre-existing worker tests unchanged.

- [ ] **Step 6: Run the whole unit suite in the foreground**

Run: `… pytest tests/unit -q` (Bash timeout 600000 — do not background it)
Expected: no new failures.

- [ ] **Step 7: Revert-check**

Remove `deadline=` from the `call_llm` call; confirm `test_the_model_call_is_bounded_so_it_cannot_outlive_the_function` fails; restore; confirm PASS.

- [ ] **Step 8: Commit**

```bash
git add src/lambda_session_report.py src/template.yaml tests/unit/test_session_report_generates.py
git commit -m "The worker writes the report, instead of pasting the topics"
```

---

### Task 6: org-api asks for a generated report

**Files:**
- Modify: `src/lambda_org_api.py` (`session_report_generate` and `day_report_generate`)
- Test: `tests/unit/test_org_api_generate_request.py`

**Interfaces:**
- Consumes: `report_template.load_template` (validation only — org-api never calls a model).
- Produces: request artifact keys `generate: {"templateId", "templateVersion"}`, `window: {"from", "to"}`, `excludedTopics: [{"id", "time_range"}]`.
- Body accepted: `templateId` (str), `templateVersion` (int), optional `from`/`to` (`"HH:MM"`).

- [ ] **Step 1: Write the failing test**

```python
"""org-api decides WHAT a report covers; the worker decides how it reads. The artifact
is the whole contract between them, so this pins the three keys generation needs."""
import json

import pytest

import lambda_org_api as org


def _body(**kw):
    base = {"deliver": "download", "templateId": "personal-meeting", "templateVersion": 3}
    base.update(kw)
    return base


def test_a_named_template_reaches_the_worker_with_the_window(day_generate):
    put = day_generate(_body(**{"from": "09:00", "to": "11:30"}))
    artifact = json.loads(put["Body"])
    assert artifact["generate"] == {"templateId": "personal-meeting", "templateVersion": 3}
    assert artifact["window"] == {"from": "09:00", "to": "11:30"}


def test_an_unknown_template_is_refused_before_anything_is_enqueued(day_generate_raw):
    res, puts = day_generate_raw(_body(templateId="does-not-exist"))
    assert res["statusCode"] == 400
    assert "template" in json.loads(res["body"])["error"].lower()
    assert puts == [], "nothing may be enqueued for a template that does not exist"


def test_the_excluded_topics_travel_with_their_times(day_generate):
    put = day_generate(_body())
    artifact = json.loads(put["Body"])
    assert {"id", "time_range"} <= set(artifact["excludedTopics"][0])


def test_a_request_without_a_template_is_still_the_old_assembled_report(day_generate):
    put = day_generate({"deliver": "download"})
    artifact = json.loads(put["Body"])
    assert "generate" not in artifact
```

Build `day_generate` / `day_generate_raw` as fixtures in this file on the pattern already used by `tests/unit/test_org_api_day_report.py`: stub the folder resolver, `_report_rows_in_scope`, `redactions.deleted_source_prefixes` and the S3 client, call `org.day_report_generate(...)`, and return the `put_object` kwargs (or the response plus the list of puts).

- [ ] **Step 2: Run it to verify it fails**

Run: `… pytest tests/unit/test_org_api_generate_request.py -q`
Expected: FAIL — the artifact has no `generate` key.

- [ ] **Step 3: Implement**

In `src/lambda_org_api.py`, beside the other module imports, add `import report_template`.

Add this helper directly above `def day_report_preview(`:

```python
def _generation_request(body):
    """The template a report is written to, validated here so a bad name fails the
    request instead of the worker -- the caller is still on the line at this point.
    Absent template = today's assembled report (spec 2026-09-15 §5.3)."""
    template_id = (body or {}).get("templateId")
    if not template_id:
        return None, None
    try:
        version = int((body or {}).get("templateVersion"))
    except (TypeError, ValueError):
        return None, "templateVersion must be a number"
    try:
        report_template.load_template(template_id, version)
    except report_template.TemplateNotFound:
        return None, "no such template: %s v%s" % (template_id, version)
    return {"templateId": template_id, "templateVersion": version}, None
```

In **both** `session_report_generate` and `day_report_generate`, directly after the body is parsed and before the artifact dict is built:

```python
    generate, gen_error = _generation_request(body)
    if gen_error:
        return error(gen_error, 400)
```

and when building the artifact dict, add:

```python
        **({"generate": generate,
            "window": {"from": (body.get("from") or "00:00"), "to": (body.get("to") or "23:59")},
            "excludedTopics": _excluded_topics_for(conn, caller, folder, date)} if generate else {}),
```

Add the excluded-topic reader beside `_report_rows_in_scope`:

```python
def _excluded_topics_for(conn, caller, folder, date):
    """The day's topics the report scope refuses to show -- redacted or non-work --
    with the time_range the worker needs to cut them out of the transcript. Only the
    id and the range travel: the text of a hidden topic never leaves the database."""
    rows = topics.list_topics_for_day(conn, folder, date)
    hidden = redactions.company_excluded_topic_ids(conn, caller["company_id"])
    return [{"id": str(r["id"]), "time_range": r.get("time_range")}
            for r in rows
            if str(r["id"]) in hidden or (r.get("work_class") == "non_work")]
```

Use the repository function this file already calls for a day's topics; if its name differs from `topics.list_topics_for_day`, use the existing one rather than adding a query.

- [ ] **Step 4: Run the new test plus the existing route tests**

Run: `… pytest tests/unit/test_org_api_generate_request.py tests/unit/test_org_api_day_report.py tests/unit/test_org_api_day_report_status.py tests/unit/test_org_api_sessions.py -q`
Expected: PASS.

- [ ] **Step 5: Run the whole unit suite in the foreground**

Run: `… pytest tests/unit -q` (Bash timeout 600000)
Expected: no new failures.

- [ ] **Step 6: Revert-check**

Make `_generation_request` skip validation (return the dict without calling `load_template`); confirm `test_an_unknown_template_is_refused_before_anything_is_enqueued` fails; restore; confirm PASS.

- [ ] **Step 7: Commit**

```bash
git add src/lambda_org_api.py tests/unit/test_org_api_generate_request.py
git commit -m "A report request can name the template it is written to"
```

---

### Task 7: Deploy to TEST and prove it on a real day

**Files:** none (release procedure). A green unit suite says nothing about what is deployed.

- [ ] **Step 1: Open the PR**

```bash
git push -u origin <branch>
gh pr create --base develop --title "Write the report to a template, instead of pasting the topics" --body "<what + why; tests; revert-checks; the TEST plan below>"
```

Wait for CI, then **stop and ask the owner to merge** — merges and prod promotion are theirs.

- [ ] **Step 2: Confirm TEST carries it**

```bash
export AWS_PROFILE=fieldsight-deployer
D="C:/Users/camil/AppData/Local/Temp/claude/verify"; mkdir -p "$D"
aws lambda get-function --function-name fieldsight-test-session-report --query Code.Location --output text | xargs curl -sL -o "$D/sr.zip"
unzip -p "$D/sr.zip" report_template.py | grep -c "def render_prompt"
unzip -l "$D/sr.zip" | grep -c "report_templates/personal-meeting.v3.json"
aws lambda get-function-configuration --function-name fieldsight-test-session-report \
  --query "Environment.Variables.[LLM_PROVIDER,LLM_HTTP_TIMEOUT,GENERATION_BUDGET_SECONDS]" --output text
```

Expected: both counts ≥ 1, and the three env values present. An env key that is missing here is the classic unwired toggle: the code reads a default and nothing reports it.

- [ ] **Step 3: Generate a real one**

Use the day and window the template was adopted against — `Ben_UCPK2`, `2026-09-10`, `09:00`–`11:30` — through the TEST gateway with an admin token, then poll to `done` and download the docx.

Expected: sections named by the template, an Actions table whose owners and dates match the day's extracted action items, no `spk_N` anywhere, and nothing from a redacted or non-work topic.

- [ ] **Step 4: Compare against the sample**

Diff the document's headings and action list against `docs/superpowers/specs/templates/sample-personal-meeting-v3-2026-09-10-0900-1130.md` in the spec repo. The wording will differ run to run; the sections and the action count should not. Record the differences in the PR before promotion.

- [ ] **Step 5: Report to the owner**

Post the result on the PR: the generated document, the time it took end to end, and the model reported in the result JSON. Promotion to prod is the owner's call.

---

## Self-Review

**Spec coverage.** §5.3 generation → Tasks 1, 2, 5; §5.3 clipping and fail-closed → Task 3; §6.1 template as a section plan, `style`, actions from extraction → Tasks 1, 4, 5; §7 "model returns empty content with HTTP 200" → Task 5 (`test_an_empty_answer_is_an_error_not_an_empty_report`); §7 "template placeholder copied into a customer report" → Task 1 (`test_no_placeholder_survives_rendering`); §7 "whole worn day exceeds worker timeout" → Task 5's bounded deadline plus Task 7's measured run. **Not covered here, by design:** §6 template storage and the Library (its own plan), the frontend template picker and poll deadline (same plan), and the daily report's own template (a second template file once the shape is proven on this one).

**Placeholder scan.** Two steps name a fallback rather than a fixed value — `_put_document` in Task 5 and `topics.list_topics_for_day` in Task 6 — because the exact local helper names must match what the file already uses. Both say precisely what to do instead, and neither hides a design decision.

**Type consistency.** `load_template(template_id, version) -> dict` and `TemplateNotFound` are used identically in Tasks 1, 5 and 6. `render_prompt(template, scope, action_items, transcript)` takes the same four arguments in Tasks 1 and 5. `generate_prose_document(title, subtitle, sections, actions)` matches between Tasks 4 and 5. The action-item dict is `{action, owner, deadline}` everywhere, including the docx table. `excluded_spans(date, excluded_topics)` consumes exactly the `{"id", "time_range"}` shape Task 6 writes onto the artifact.
