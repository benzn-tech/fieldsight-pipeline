"""Final whole-branch review fixes for action packs (spec §14 S13/S15):
1. The priority values apply accepts are ONE constant (action_pack_view.ACTION_PRIORITIES);
   the writer lower-cases a priority and drops one apply would refuse, and is told the values.
   (`applicable` for a responsible who is not a site member: tests/integration.)
2. find_people is not an agent tool (it takes no site).
3. An uncited conclusion line is hidden once any non-web source is removed for the reader.
4. With ASK_JUDGE_FALLBACKS set, one model attempt gets a share of the window, not all of it.
"""
import json

import pytest

import action_pack_stages as st
import action_pack_view as v
from tests.unit.test_action_pack_stages import (SID2, WRITER, Model, Tools, Verify, deps,
                                                make_ctx, write_obj)


# ---- 1. priority ----------------------------------------------------------------------

def test_org_api_uses_the_shared_priority_set():
    org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")
    assert org.ALLOWED_ACTION_PRIORITY is v.ACTION_PRIORITIES
    assert sorted(v.ACTION_PRIORITIES) == ["high", "low", "medium"]


def _update(proposed):
    return {"type": "update_item", "target_stable_id": SID2, "proposed": proposed,
            "rationale": "r", "refs": ["R1"]}


def test_the_writer_lower_cases_priority_and_drops_one_apply_would_refuse():
    write = write_obj(actions=[
        _update({"priority": "High"}),
        _update({"priority": "urgent", "deadline": "2026-11-10"}),
        _update({"priority": "P1"}),                       # nothing else left: dropped
        {"type": "create_item", "proposed": {"text": "Book the pump", "priority": "ASAP"},
         "rationale": "r", "refs": ["R1"]}])
    verify = Verify()
    payload = st.run(make_ctx(), deps(model=Model(write=[write]), verify=verify))
    proposed = [a["proposed"] for a in verify.calls[0]["draft"]["actions"]]
    assert proposed == [{"priority": "high"}, {"deadline": "2026-11-10"},
                        {"text": "Book the pump"}]
    dropped = payload["run_trace"]["write_dropped"]
    assert dropped["priority"] == 3 and dropped["actions"] == 1


def test_the_writer_prompt_names_the_priority_values():
    model = Model()
    st.run(make_ctx(), deps(model=model))
    prompt = next(c["prompt"] for c in model.calls if c["caller"] == "action_write")
    assert '"priority": "low"|"medium"|"high"' in prompt


# ---- 2. find_people -------------------------------------------------------------------

def test_find_people_is_not_an_agent_tool():
    assert "find_people" not in st.RECORD_TOOLS
    rounds = [{"calls": [{"tool": "find_people", "args": {"query": "Sam"}}], "done": True}]
    tools = Tools()
    model = Model(rounds=rounds)
    payload = st.run(make_ctx(), deps(model=model, tools=tools))
    assert payload["run_trace"]["calls_refused"] == 1
    assert not any(c["name"] == "find_people" for b in tools.calls for c in b)
    catalog = next(c["prompt"] for c in model.calls if c["caller"] == "action_round")
    assert "find_people" not in catalog


# ---- 3. uncited conclusion lines (S13) ------------------------------------------------

def _pack():
    return {"id": "p", "site_id": "s", "created_by": "u", "state": "ready",
            "archived_at": None,
            "conclusion": [{"text": "cited", "refs": ["R1"], "confidence": "supported"},
                           {"text": "UNCITED-LINE", "refs": [], "confidence": "uncertain"},
                           {"text": "NO-REFS-KEY", "confidence": "uncertain"}],
            "sources": [{"ref": "R1", "kind": "topic", "provenance": "records"},
                        {"ref": "R2", "kind": "topic", "provenance": "records"},
                        {"ref": "W1", "kind": "web_source", "provenance": "web"}],
            "actions": []}


def _view(removed):
    return v.shape_pack(_pack(), removed_refs=removed, site_name=None, targets={},
                        applicable={}, viewer_id="u")


def test_uncited_lines_stay_while_every_non_web_source_is_live():
    assert [c["text"] for c in _view(set())["conclusion"]] == [
        "cited", "UNCITED-LINE", "NO-REFS-KEY"]
    # A web ref in the removed set is not a record the line may come from.
    assert len(_view({"W1"})["conclusion"]) == 3


def test_uncited_lines_are_hidden_once_any_non_web_source_is_removed():
    view = _view({"R2"})
    assert [c["text"] for c in view["conclusion"]] == ["cited"]
    md = v.render_markdown(view)
    assert "UNCITED-LINE" not in md and "NO-REFS-KEY" not in md and "cited" in md


# ---- 1b. applicable_reason in the shaped view -----------------------------------------

def test_shape_pack_carries_the_applicable_reason():
    pack = dict(_pack(), conclusion=[], actions=[
        {"id": "a1", "seq": 1, "type": "update_item", "refs": ["R1"], "verifier": "weak",
         "state": "proposed", "proposed": {"responsible": "Nobody Here"}},
        {"id": "a2", "seq": 2, "type": "note", "refs": ["R1"], "verifier": "weak",
         "state": "proposed"}])
    view = v.shape_pack(pack, removed_refs=set(), site_name=None, targets={},
                        applicable={"a1": False, "a2": False}, viewer_id="u",
                        reasons={"a1": v.REASON_RESPONSIBLE_NOT_MEMBER})
    a1, a2 = view["actions"]
    assert a1["applicable"] is False and a1["applicable_reason"] == "responsible_not_member"
    assert a2["applicable_reason"] is None
    # Without reasons (older callers) the key is still there, null.
    view = v.shape_pack(pack, removed_refs=set(), site_name=None, targets={},
                        applicable={}, viewer_id="u")
    assert [a["applicable_reason"] for a in view["actions"]] == [None, None]


# ---- 4. per-attempt share with fallbacks ----------------------------------------------

def _spy(monkeypatch):
    import llm_tools_client as tc
    seen = {}

    def complete_json(prompt, *, model, max_tokens, deadline, caller, attempt_timeout):
        seen.update(deadline=deadline, timeout=attempt_timeout)
        return tc.ChatTurn(text="{}", model=model, requested_model=model)
    monkeypatch.setattr(tc, "complete_json", complete_json)
    monkeypatch.setattr(tc, "_now", lambda: 1000.0)
    return seen


def test_without_fallbacks_one_attempt_may_use_the_whole_window(monkeypatch):
    monkeypatch.delenv("ASK_JUDGE_FALLBACKS", raising=False)
    seen = _spy(monkeypatch)
    st.judge_call("p", max_tokens=10, model=WRITER, deadline=100.0, caller="c", info={})
    assert seen == {"deadline": 1100.0, "timeout": 100.0}
    # A fallback list naming only the primary is no fallback either.
    monkeypatch.setenv("ASK_JUDGE_FALLBACKS", WRITER)
    st.judge_call("p", max_tokens=10, model=WRITER, deadline=100.0, caller="c", info={})
    assert seen == {"deadline": 1100.0, "timeout": 100.0}


def test_with_fallbacks_one_attempt_gets_a_share_of_the_window(monkeypatch):
    monkeypatch.setenv("ASK_JUDGE_FALLBACKS", "openai/gpt-6-luna-pro")
    seen = _spy(monkeypatch)
    st.judge_call("p", max_tokens=10, model=WRITER, deadline=100.0, caller="c", info={})
    assert seen["deadline"] == 1100.0                      # the window itself is unchanged
    assert seen["timeout"] == pytest.approx(100.0 * st.FALLBACK_ATTEMPT_SHARE)
    assert st.FALLBACK_ATTEMPT_SHARE == 0.6


def test_a_hung_primary_leaves_the_fallback_its_time(monkeypatch):
    """Through the real complete_json: the primary hangs for its whole attempt, the
    fallback still gets the rest of the window and answers."""
    import corroboration_client
    import llm_tools_client as tc
    import urllib3
    from tests.unit.test_llm_tools_client_json import _ok

    monkeypatch.setenv("CORROBORATION_API_KEY", "k")
    monkeypatch.setenv("ASK_JUDGE_MODEL", WRITER)
    monkeypatch.setenv("ASK_JUDGE_FALLBACKS", "openai/gpt-6-luna-pro")
    clock = {"t": 1000.0}
    monkeypatch.setattr(tc, "_now", lambda: clock["t"])
    calls = []

    class Pool:
        def request(self, method, url, body=None, headers=None, timeout=None):
            model = json.loads(body)["model"]
            calls.append((model, timeout))
            if model == WRITER:                            # hangs until its timeout
                clock["t"] += timeout
                raise urllib3.exceptions.ReadTimeoutError(None, url, "timed out")
            return _ok(served=model)
    monkeypatch.setattr(corroboration_client.urllib3, "PoolManager", lambda *a, **k: Pool())
    info = {}
    text, err = st.judge_call("p", max_tokens=10, model=WRITER, deadline=100.0,
                              caller="action_write", info=info)
    assert err is None and text == '{"a": 1}'
    assert info["model"] == "openai/gpt-6-luna-pro"
    assert calls[0] == (WRITER, pytest.approx(60.0))
    assert calls[1][0] == "openai/gpt-6-luna-pro" and calls[1][1] == pytest.approx(40.0)
