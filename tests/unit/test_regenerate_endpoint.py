"""Regenerating an extraction with the names a person confirmed.

Renaming a speaker does not change Overview, Action Items or the draft email, and
that is not a synchronisation bug: they hold different names. The transcript's
names say who was TALKING. `action_items.responsible` holds names people SAID OUT
LOUD, which on prod are routinely not speakers at all -- real values include
"Design team", "IT Support", "Karina and Anton", "Tony or contractor team".

Nothing records which speaker an extracted name came from (`action_items` has no
speaker column), so a find-and-replace would silently reassign a task from one
Jesse to a different Jesse, and would edit a substring of a field naming two
people. Handing the model the confirmed names and re-reasoning is the sound
alternative, and it is worth more than a rename: with `spk_0` in the prompt, "I'll
chase the supplier" has no owner the model can name.
"""
import json

import pytest

import lambda_org_api as api


CALL_A = "ben_2026-08-27_11-06-35_sid93_c0000.json"
CALL_B = "ben_2026-08-27_11-08-03_sid93_c0003.json"
SESSION = "sid" + "9" * 32


def _seg(fn, label, name=None, state=None):
    s = {"source_filename": fn, "speaker_label": label}
    if name is not None:
        s["speaker_name"] = name
    if state is not None:
        s["speaker_state"] = state
    return s


class _S3:
    def __init__(self):
        self.puts = []

    def put_object(self, **kw):
        self.puts.append(kw)
        return {}


@pytest.fixture
def wired(monkeypatch):
    s3 = _S3()
    monkeypatch.setattr(api, "S3_BUCKET", "bucket")
    monkeypatch.setattr(api.boto3, "client", lambda name, **kw: s3)
    monkeypatch.setattr(api, "_resolve_org_media_folder",
                        lambda conn, caller, user, what: ("Ben_UCPK2", None))
    return s3


def _call(monkeypatch, wired, segs, session=SESSION, role="pm", body=None):
    monkeypatch.setattr(api, "_read_org_transcripts",
                        lambda date, folder, a, b, conn=None: {"speaker_segments": segs})
    monkeypatch.setattr(api, "_apply_speaker_names", lambda conn, caller, out: out)
    caller = {"global_role": role, "id": "u1", "company_id": "c1"}
    # `body if body is not None`, not `body or` -- an empty dict is a REAL case
    # here (a caller that sent no date) and `or` would quietly replace it with the
    # valid default, so the test would assert nothing.
    default = {"date": "2026-08-27", "user": "Ben_UCPK2"}
    event = {"body": json.dumps(default if body is None else body)}
    return api.regenerate_session(None, caller, session, event)


def _sent(wired):
    return json.loads(wired.puts[0]["Body"].decode("utf-8"))


# --------------------------------------------------------------------------
# What gets sent
# --------------------------------------------------------------------------

def test_confirmed_names_are_sent_keyed_by_call_and_label(monkeypatch, wired):
    """Keyed on (source_filename, speaker_label), never the label alone: `spk_0`
    is scoped to ONE call, and the same label later is usually a different
    person. A session-level map would undo what the re-bind exists to repair."""
    resp = _call(monkeypatch, wired, [
        _seg(CALL_A, "spk_0", "Ben", "confirmed"),
        _seg(CALL_B, "spk_0", "Mark", "confirmed")])
    assert resp["statusCode"] == 202
    assert _sent(wired)["speakerNames"] == [
        {"source_filename": CALL_A, "speaker_label": "spk_0", "display_name": "Ben"},
        {"source_filename": CALL_B, "speaker_label": "spk_0", "display_name": "Mark"}]


def test_a_tentative_name_is_never_sent(monkeypatch, wired):
    """`tentative` is the SYSTEM's guess. Handed to the model as a confirmed name,
    a guess comes back as a fact in a report -- and the reader cannot tell."""
    _call(monkeypatch, wired, [
        _seg(CALL_A, "spk_0", "Ben", "tentative"),
        _seg(CALL_B, "spk_1", "Mark", "confirmed")])
    sent = _sent(wired)
    assert [r["display_name"] for r in sent["speakerNames"]] == ["Mark"]


def test_a_segment_with_no_name_contributes_nothing(monkeypatch, wired):
    _call(monkeypatch, wired, [_seg(CALL_A, "spk_0"),
                               _seg(CALL_B, "spk_1", "Mark", "confirmed")])
    assert len(_sent(wired)["speakerNames"]) == 1


def test_one_row_per_pair_however_many_turns(monkeypatch, wired):
    """The prompt needs the mapping once, not once per turn. A 300-turn session
    would otherwise put 300 duplicate rows into an artifact."""
    _call(monkeypatch, wired, [_seg(CALL_A, "spk_0", "Ben", "confirmed")] * 5)
    assert len(_sent(wired)["speakerNames"]) == 1


def test_no_confirmed_names_still_regenerates_but_says_so(monkeypatch, wired):
    """Regenerating with zero names re-runs the same prompt for the same answer
    and costs a model call. The caller has to be able to say that, rather than
    watch nothing change for the second time."""
    resp = _call(monkeypatch, wired, [_seg(CALL_A, "spk_0")])
    assert resp["statusCode"] == 202
    assert json.loads(resp["body"])["namedTurns"] == 0
    assert json.loads(resp["body"])["willChangeNames"] is False
    assert "speakerNames" not in _sent(wired)


def test_the_request_lands_on_the_existing_trigger(monkeypatch, wired):
    """Same key the finalize sweep writes, so this rides the S3 notification that
    already exists. Every notification on this bucket is hand-wired outside the
    template (BUG-33), so a new prefix would be a new manual step."""
    _call(monkeypatch, wired, [_seg(CALL_A, "spk_0", "Ben", "confirmed")])
    assert wired.puts[0]["Key"] == f"extraction_requests/{SESSION[3:]}.json"
    sent = _sent(wired)
    assert sent["sessionBase"] == SESSION
    assert sent["userFolder"] == "Ben_UCPK2"
    assert sent["date"] == "2026-08-27"


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------

def test_a_worker_may_not_regenerate(monkeypatch, wired):
    resp = _call(monkeypatch, wired, [_seg(CALL_A, "spk_0", "Ben", "confirmed")],
                 role="worker")
    assert resp["statusCode"] == 403
    assert not wired.puts


@pytest.mark.parametrize("bad", ["", "sid", "sidZZZ", "../../etc/passwd",
                                 "sid" + "9" * 31, "sid" + "9" * 33])
def test_a_session_that_is_not_a_session_is_refused(monkeypatch, wired, bad):
    """The value becomes an S3 key. Anchored rather than escaped: this shape is
    the only one the producers write, and refusing what we do not recognise is a
    smaller claim to defend than escaping it correctly."""
    resp = _call(monkeypatch, wired, [], session=bad)
    assert resp["statusCode"] == 400
    assert not wired.puts


@pytest.mark.parametrize("body", [{}, {"date": "yesterday"}, {"date": "27-08-2026"}])
def test_a_bad_date_is_refused_before_anything_is_written(monkeypatch, wired, body):
    resp = _call(monkeypatch, wired, [], body=body)
    assert resp["statusCode"] == 400
    assert not wired.puts


def test_a_folder_refusal_stops_it(monkeypatch, wired):
    """Cross-tenant reach is decided by the shared resolver, not re-litigated
    here. What this pins is that its refusal is honoured before the write."""
    monkeypatch.setattr(api, "_resolve_org_media_folder",
                        lambda conn, caller, user, what: (None, api.error("nope", 403)))
    resp = _call(monkeypatch, wired, [_seg(CALL_A, "spk_0", "Ben", "confirmed")])
    assert resp["statusCode"] == 403
    assert not wired.puts
