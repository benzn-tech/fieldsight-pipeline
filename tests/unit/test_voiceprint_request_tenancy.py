"""Unit: project-owned tenancy P5 -- the match request on another company's site.

The request is made in the SITE company and names the recorder's home company and user, so
the writer can build candidates = site-enrolled + the recorder's own. Home-site and older
requests carry none of the new fields.
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

import speaker_match_request as smr  # noqa: E402

SID = "sid" + "b" * 32
BASE = f"eve_x_2026-10-09_09-00-00_{SID}"
TURNS = [{"source_filename": f"{BASE}_c0000_srcwav.json", "start_sec": 0.0, "end_sec": 5.0,
          "speaker_label": "spk_0"}]


def _build(**kw):
    return smr.build("co-B", BASE, "Eve_X", "2026-10-09", TURNS, "on", "finalize", **kw)


def test_external_recorder_request_names_home_company_and_recorder():
    (req,) = _build(home_company_id="co-A", recorder_user_id="u-eve")
    assert req["company_id"] == "co-B"
    assert req["home_company_id"] == "co-A" and req["recorder_user_id"] == "u-eve"


def test_home_site_request_is_byte_identical_to_before():
    plain = _build()
    same = _build(home_company_id="co-B", recorder_user_id="u-eve")
    assert "home_company_id" not in plain[0] and "recorder_user_id" not in plain[0]
    assert "home_company_id" not in same[0]
    for r in plain + same:
        r.pop("request_id")
    assert plain == same


def test_request_match_goes_out_in_the_site_company_with_the_home_company(monkeypatch):
    iw = pytest.importorskip("lambda_item_writer", reason="requires psycopg")
    monkeypatch.setattr(iw, "MATCH_ON_FINALIZE", True)
    monkeypatch.setattr(iw, "SPEAKER_IDENTITY_MODE", "on")
    puts = []
    ok = iw._request_match("co-B", BASE, {"speaker_turns": TURNS, "userFolder": "Eve_X",
                                           "date": "2026-10-09"},
                           site_id="s1", put=lambda k, b: puts.append((k, b)),
                           home_company_id="co-A", recorder_user_id="u-eve")
    assert ok
    key, body = puts[0]
    assert key.startswith(f"voiceprint_requests/co-B/{BASE}/")
    assert body["company_id"] == "co-B" and body["home_company_id"] == "co-A"
    assert body["recorder_user_id"] == "u-eve"


def test_finalize_wiring_passes_site_company_and_recorder():
    src = open(os.path.join(ROOT, "src", "lambda_item_writer.py"), encoding="utf-8").read()
    call = src[src.index("_request_match(owner_company_id"):]
    call = call[:call.index(")\n")]
    assert "home_company_id=company[\"id\"]" in call and "recorder_user_id=user_id" in call
    assert "_request_rebind(owner_company_id" in src


def test_embedder_forwards_home_and_recorder_to_the_profiles_invoke(monkeypatch):
    se = pytest.importorskip("lambda_speaker_embed", reason="requires numpy/onnx layer")
    sent = []
    monkeypatch.setattr(se, "invoke_writer",
                        lambda p: sent.append(p) or {"profiles": [], "company_floor": None})
    base = {"session_base": SID, "company_id": "co-B", "user_folder": "u",
            "date": "2026-10-09", "turns": [], "label_map": []}
    for doc in (dict(base, home_company_id="co-A", recorder_user_id="u-eve"), base):
        monkeypatch.setattr(se, "_get", lambda k, d=doc: json.dumps(d).encode())
        se._from_match_artifact("b", "voiceprint_requests/co-B/s/match-1.json")
    assert sent[0]["home_company_id"] == "co-A" and sent[0]["recorder_user_id"] == "u-eve"
    assert "home_company_id" not in sent[1], "old-format request must be sent as before"
