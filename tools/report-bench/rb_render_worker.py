"""Cold render worker: one prompt from a snapshot of src/ at some sha.

Run as `python -I -X utf8 rb_render_worker.py <src_dir>` by rb_snapshot.render,
with an environment built from scratch (rb_common.subprocess_env). `-I` puts
no script dir and no PYTHONPATH on sys.path; the worker inserts <src_dir>
itself, so every module comes from the snapshot, imported cold under the
spec §2 import-time variables.

Request (stdin, UTF-8 JSON): {"task": "X"|"T"|"R", "inputs": {...}}.
Response (stdout, UTF-8 JSON): {"prompt": "..."}. Any failure: nonzero exit,
the reason on stderr's last line (never prompt text).

Per task, kwargs are filtered by each sha's own signature (inspect.signature),
so an older function that lacks `speaker_names` / `continuity_block` / `topics`
still renders and never raises TypeError:

  X  lambda_extract_session.build_extraction_prompt(user_folder, date,
     session_base, turns, n_segments[, speaker_names][, continuity_block])
     -> [0] of the tuple (a bare str is accepted too). Every candidate sha
     (763b3082, 83abd0d4, 8703e69a, 82036cf2, develop) has this signature;
     8703e69a+ add continuity_block (DECLARE_CONTINUITY=false -> unused).
  T  report_template.render_prompt(template, scope, action_items, transcript,
     source, topics) -- the same signature at e4e2ea19, 751481a3 and develop.
     template = the snapshot's built-in `personal-meeting` v3, passed through
     `report_facts.split_plan` when the snapshot has it (751481a3+ as prod
     did; e4e2ea19 has no report_facts and sends the whole plan).
     action_items = inputs["action_items"] when given, else the snapshot's
     lambda_session_report._action_items_for_prompt(content[, offer][, date]).
     topics = the snapshot's lambda_session_report._offered_topics(artifact, 0,
     day 00:00, day 23:59) when inputs carry `artifact` ({folder, date,
     content}): f2191232+ adds each topic's content ("the record", #997), so
     the T-1001 vs T-cur difference lives there, not in render_prompt.
  R  lambda_report_generator.build_daily_prompt(**filtered inputs) with the
     served S3 template injected as `_prompt_templates_cache`.
"""
import inspect
import json
import os
import sys


def _filtered(fn, kwargs):
    params = inspect.signature(fn).parameters
    if any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return dict(kwargs)
    return {k: v for k, v in kwargs.items() if k in params}


def _first(result):
    if isinstance(result, (tuple, list)):
        return result[0]
    return result


def render_x(inputs):
    import lambda_extract_session as les
    kw = {k: inputs[k] for k in ("user_folder", "date", "session_base", "turns",
                                 "n_segments") if k in inputs}
    rows = inputs.get("speaker_names")
    if rows:
        # The artifact's `speakerNames` rows -> {(source_filename, label): name},
        # the key every candidate sha's render_transcript reads (JSON cannot
        # carry tuple keys, so the rows travel and are keyed here).
        kw["speaker_names"] = {(r["source_filename"], r["speaker_label"]): r["display_name"]
                               for r in rows}
    return _first(les.build_extraction_prompt(**_filtered(les.build_extraction_prompt, kw)))


def _template(inputs):
    import report_template as rt
    if inputs.get("template") is not None:
        template = inputs["template"]
    else:
        template = rt.load_template(inputs.get("template_id", "personal-meeting"),
                                    int(inputs.get("template_version", 3)))
    try:
        import report_facts
    except ImportError:
        return template
    split = getattr(report_facts, "split_plan", None)
    return split(template)[0] if split else template


def _offer(inputs):
    """The snapshot's own topic offer for a whole-day window (no photographs:
    the bench sends none, so nothing is fetched)."""
    if "artifact" not in inputs:
        return inputs.get("topics")
    import datetime
    import lambda_session_report as lsr
    day = datetime.datetime.strptime(inputs["scope"]["date"], "%Y-%m-%d")
    fn = lsr._offered_topics
    kw = {"artifact": inputs["artifact"], "budget": 0, "win_from": day,
          "win_to": day + datetime.timedelta(hours=23, minutes=59)}
    return _first(fn(**_filtered(fn, kw)))


def render_t(inputs):
    import report_template as rt
    template = _template(inputs)
    topics = _offer(inputs)
    if "action_items" in inputs:
        actions = inputs["action_items"]
    else:
        import lambda_session_report as lsr
        fn = lsr._action_items_for_prompt
        content = (inputs.get("artifact") or {}).get("content") or inputs.get("content")
        actions = fn(**_filtered(fn, {"content": content, "offer": topics,
                                      "date": inputs["scope"]["date"]}))
    kw = {"template": template, "scope": inputs["scope"], "action_items": actions,
          "transcript": inputs["transcript"], "source": rt.SOURCE_BUILTIN, "topics": topics}
    return rt.render_prompt(**_filtered(rt.render_prompt, kw))


def render_r(inputs):
    import lambda_report_generator as rg
    rg._prompt_templates_cache = inputs["served_templates"]
    fn = rg.build_daily_prompt
    return _first(fn(**_filtered(fn, inputs["kwargs"])))


def main():
    src = os.path.abspath(sys.argv[1])
    if "LLM_TEMPERATURE" in os.environ:
        raise SystemExit("LLM_TEMPERATURE must not be set for a snapshot render")
    sys.path.insert(0, src)
    req = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    task = req["task"]
    fn = {"X": render_x, "T": render_t, "R": render_r}[task]
    prompt = fn(req["inputs"])
    if not isinstance(prompt, str):
        raise SystemExit(f"task {task}: renderer returned {type(prompt).__name__}")
    sys.stdout.buffer.write(json.dumps({"prompt": prompt}, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:                        # noqa: BLE001 - reported by type
        sys.stderr.write(f"render failed: {type(exc).__name__}: {str(exc)[:200]}\n")
        raise SystemExit(1)
