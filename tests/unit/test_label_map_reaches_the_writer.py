"""Unit: the label map has to survive the middle hop, or the feature does not exist.

`label_map` is written by org-api into both request artifacts, and read by the writer out of
its event. The only thing between them is the embedder, which builds the writer's payload —
and it never carried the key across, so `_inherit_labels` received None on every call and
returned immediately. PR #508's whole label-inheritance feature, and the
`0044_speaker_name_rejections` guard it feeds, were unreachable code.

Nothing failed. Each side's tests exercised its own half and passed; this repo's CLAUDE.md
lists that exact shape — "I wrote the function and tested the function and never called it".
"""
import ast
import os

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src")


def _module(name):
    return open(os.path.join(SRC, name), encoding="utf-8").read()


def _writer_payload_dicts():
    """Every dict literal the embedder hands to invoke_writer."""
    tree = ast.parse(_module("lambda_speaker_embed.py"))
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "invoke_writer"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Dict):
                out.append({k.value for k in arg.keys if isinstance(k, ast.Constant)})
            elif isinstance(arg, ast.Name):
                # payload built above and passed by name — resolve it from the assignment
                for a in ast.walk(tree):
                    if (isinstance(a, ast.Assign) and isinstance(a.value, ast.Dict)
                            and any(getattr(t, "id", "") == arg.id for t in a.targets)):
                        out.append({k.value for k in a.value.keys
                                    if isinstance(k, ast.Constant)})
    return out


def test_the_writer_reads_a_key_the_embedder_never_sent():
    """Both ends agreed on the name; the middle never spoke it."""
    writer = _module("lambda_voiceprint_writer.py")
    assert 'event.get("label_map")' in writer, "the writer no longer reads it — update this"

    payloads = _writer_payload_dicts()
    assert payloads, "found no invoke_writer payloads to inspect — the AST walk is stale"

    # Only the ops that lead to _inherit_labels need it; `profiles` is a lookup.
    carrying = [p for p in payloads if "results" in p]
    assert carrying, "no result-bearing payload found"
    for keys in carrying:
        assert "label_map" in keys, (
            "a payload that reaches _inherit_labels does not carry label_map, so it "
            f"receives None and returns 0 — keys were {sorted(keys)}")


def _match_artifact(monkeypatch, doc, profiles):
    """Drive the real production entry point: an S3 artifact, not the direct op."""
    import json as _json
    import lambda_speaker_embed as se

    sent = []

    def _writer(payload):
        sent.append(payload)
        return {"profiles": profiles} if payload.get("op") == "profiles" else {"written": 3}

    monkeypatch.setattr(se, "invoke_writer", _writer)
    monkeypatch.setattr(se, "_get", lambda k: _json.dumps(doc))
    se._from_match_artifact("b", "voiceprint_requests/co-1/s/match-1.json")
    return sent


def test_inheritance_runs_even_when_the_company_holds_no_profile(monkeypatch):
    """Label inheritance does not need a voiceprint. It spreads names the session ALREADY
    holds — from somebody's correction — to the turns too short to embed, and it rides inside
    the writer's `match_names` op.

    The no-profiles branch returned before invoking that op, so "match this session" did
    nothing at all for a company with no profiles. That is not a corner: enrolment is refused
    on real site audio today, so no company HAS a profile, which makes this the only branch
    that runs.
    """
    sent = _match_artifact(monkeypatch, {
        "op": "match", "company_id": "co-1", "user_folder": "u", "date": "2026-08-11",
        "session_base": "s", "mode": "on", "turns": [],
        "label_map": [{"turn_ref": "x_c0000@0.0", "label": "spk_0"}]}, profiles=[])
    names = [p for p in sent if p.get("op") == "match_names"]
    assert names, ("the run returned on 'no profiles' without invoking the writer, so "
                   "inheritance never ran")
    assert names[0]["label_map"], "invoked without the map inheritance needs"
    assert names[0]["results"] == [], "no profiles means no matched names to write"


def test_no_profiles_and_no_label_map_still_writes_nothing(monkeypatch):
    """The original intent survives: an empty write is a round trip that reports success,
    and 'wrote 0 rows' then reads as 'the writer is fine'."""
    sent = _match_artifact(monkeypatch, {
        "op": "match", "company_id": "co-1", "user_folder": "u", "date": "2026-08-11",
        "session_base": "s", "mode": "on", "turns": []}, profiles=[])
    assert not [p for p in sent if p.get("op") == "match_names"]


def test_shadow_mode_never_writes_even_to_inherit(monkeypatch):
    """`shadow` computes and writes nothing. Inheritance is a write, so it must not sneak
    past the mode gate through the branch added for the no-profiles case."""
    sent = _match_artifact(monkeypatch, {
        "op": "match", "company_id": "co-1", "user_folder": "u", "date": "2026-08-11",
        "session_base": "s", "mode": "shadow", "turns": [],
        "label_map": [{"turn_ref": "x_c0000@0.0", "label": "spk_0"}]}, profiles=[])
    assert not [p for p in sent if p.get("op") == "match_names"]


def test_the_inherited_count_is_reported_not_discarded(monkeypatch):
    """The writer returns `written` and `inherited` separately, and only `inherited` moves on
    the no-profiles branch — `results` is empty there by construction, so `written` is
    structurally zero.

    Reading `written` therefore reported 0 however many turns inheritance actually named. The
    feature worked and its own log said it had done nothing, which is the shape that sends the
    next person looking for a defect in the half that was fine.
    """
    import lambda_speaker_embed as se
    import json as _json

    def _writer(payload):
        if payload.get("op") == "profiles":
            return {"profiles": []}
        return {"written": 0, "declined": 0, "inherited": 22}

    monkeypatch.setattr(se, "invoke_writer", _writer)
    monkeypatch.setattr(se, "_get", lambda k: _json.dumps({
        "op": "match", "company_id": "co-1", "user_folder": "u", "date": "2026-08-11",
        "session_base": "s", "mode": "on", "turns": [],
        "label_map": [{"turn_ref": "x_c0000@0.0", "label": "spk_0"}]}))

    out = se._from_match_artifact("b", "voiceprint_requests/co-1/s/match-1.json")
    assert out["inherited"] == 22, (
        "the writer said 22 turns were inherited and the caller reported %r" % out)
    assert out["matched"] == 0, "no profile matched anything; that must not read as 22"


def test_the_correction_log_reports_what_the_writer_actually_did(monkeypatch, caplog):
    """In production this function is driven only by an S3 event, and Lambda discards what an
    event-driven invocation returns. So this log line is the ENTIRE production signal for a
    correction, and it used to say "applied" and nothing else — indistinguishable between
    "named two turns and stored a sample" and "named two turns, refused the enrolment, and
    harvested nothing".
    """
    import logging
    import lambda_speaker_embed as se
    import json as _json
    import numpy as np

    monkeypatch.setattr(se, "invoke_writer", lambda p: {
        "written": 6, "declined": 1, "inherited": 22, "enrolled": 0,
        "enrolRefused": {"reason": "this window does not hold one voice"},
        "harvested": 0, "harvestRefused": 3})
    monkeypatch.setattr(se, "embed_audio", lambda a, sr: np.ones(192, dtype=np.float32))
    monkeypatch.setattr(se, "_propagate", lambda *a, **k: ([], [], None))

    def _wav():
        import io as _io
        import wave
        buf = _io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x01" * 16000 * 12)
        return buf.getvalue()

    artifact = _json.dumps({
        "op": "correction", "company_id": "co-1", "user_folder": "u",
        "date": "2026-08-11", "session_base": "s", "request_id": "r-1",
        "correction": {"display_name": "Ivy", "source_filename": "x_c0000.wav",
                       "start_sec": 0.0, "end_sec": 12.0},
        "turns": []})
    # Key-aware: one helper fetches both the artifact (JSON text) and the audio (wav bytes).
    monkeypatch.setattr(se, "_get", lambda k: _wav() if k.endswith(".wav") else artifact)

    with caplog.at_level(logging.INFO):
        se._from_request_artifact("b", "voiceprint_requests/co-1/s/r-1.json")

    line = next((r.getMessage() for r in caplog.records
                 if "correction applied" in r.getMessage()), None)
    assert line, "the correction produced no log line at all"
    for fragment in ("named=6", "inherited=22", "harvested=0",
                     "this window does not hold one voice"):
        assert fragment in line, f"{fragment!r} missing from: {line}"
