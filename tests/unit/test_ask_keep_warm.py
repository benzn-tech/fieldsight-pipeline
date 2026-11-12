"""AskAgent keep-warm (2026-11-13): a 5-minute schedule invokes {"mode": "warm"}.

A device's first voice question after idle started in 4.8 s server-side (6.3 s at
the client) against 2.1 s warm, past the device's 5 s restatement window. The warm
call pays the imports and AWS clients and nothing else: no model, no HTTP, no S3."""
import os

import pytest

laa = pytest.importorskip("lambda_ask_agent")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_warm_returns_before_any_ask_work(monkeypatch):
    import ask_voice_turn
    made = []
    monkeypatch.setattr(ask_voice_turn, "_get_s3", lambda: made.append("s3"))
    monkeypatch.setattr(ask_voice_turn, "_get_lambda", lambda: made.append("lambda"))
    monkeypatch.setattr(ask_voice_turn, "start", lambda b: pytest.fail("warm must not start a turn"))
    monkeypatch.setattr(laa, "_stt", lambda *a, **k: pytest.fail("warm must not call STT"))
    assert laa.lambda_handler({"mode": "warm"}, None) == {"warm": True, "ok": True}
    assert made == ["s3", "lambda"]


def test_a_failing_warm_never_raises(monkeypatch):
    import ask_voice_turn

    def boom():
        raise RuntimeError("no credentials")
    monkeypatch.setattr(ask_voice_turn, "_get_s3", boom)
    assert laa.lambda_handler({"mode": "warm"}, None) == {"warm": True, "ok": False}


def _template():
    yaml = pytest.importorskip("yaml")

    class _L(yaml.SafeLoader):
        pass

    def _tag(loader, suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return {suffix: loader.construct_scalar(node)}
        if isinstance(node, yaml.MappingNode):
            return {suffix: loader.construct_mapping(node, deep=True)}
        return {suffix: loader.construct_sequence(node, deep=True)}
    _L.add_multi_constructor("!", _tag)
    with open(os.path.join(REPO, "src", "template.yaml"), encoding="utf-8") as f:
        return yaml.load(f, Loader=_L)


def test_the_schedule_is_wired_to_askagent_behind_its_own_flag():
    tpl = _template()
    ev = tpl["Resources"]["AskAgentFunction"]["Properties"]["Events"]["KeepWarm"]
    assert ev["Type"] == "Schedule"
    assert ev["Properties"]["Schedule"] == "rate(5 minutes)"
    assert ev["Properties"]["Input"] == '{"mode": "warm"}'
    assert ev["Properties"]["State"] == {"If": ["ShouldKeepAskWarm", "ENABLED", "DISABLED"]}
    assert tpl["Conditions"]["ShouldKeepAskWarm"] == {"Equals": [{"Ref": "AskKeepWarm"}, "true"]}
    assert tpl["Parameters"]["AskKeepWarm"]["Default"] == "false"


@pytest.mark.parametrize("wf,line", [
    ("deploy.yml", "\"AskKeepWarm=${{ vars.TEST_ASK_KEEP_WARM || 'true' }}\""),
    ("deploy-prod.yml", "\"AskKeepWarm=${{ vars.PROD_ASK_KEEP_WARM || 'false' }}\"")])
def test_each_stage_passes_the_flag(wf, line):
    """SAM keeps a parameter's previous value when a deploy omits it, so each stage
    passes it every time; prod stays off until the owner sets PROD_ASK_KEEP_WARM."""
    with open(os.path.join(REPO, ".github", "workflows", wf), encoding="utf-8") as f:
        assert line in f.read()


# ---- ApiFunction: the device's /ask/voice/start hop -------------------------

def test_api_warm_returns_before_the_legacy_call_line(monkeypatch, caplog):
    api = pytest.importorskip("lambda_fieldsight_api")
    made = []
    monkeypatch.setattr(api.ask_voice_turn, "_get_s3", lambda: made.append("s3"))
    with caplog.at_level("INFO"):
        assert api.lambda_handler({"mode": "warm"}, None) == {"warm": True, "ok": True}
    assert made == ["s3"]
    assert "LEGACY_CALL" not in caplog.text, "a ping must not count as a caller of this gateway"


def test_api_schedule_shares_the_askagent_flag():
    ev = _template()["Resources"]["ApiFunction"]["Properties"]["Events"]["KeepWarm"]
    assert ev["Properties"]["State"] == {"If": ["ShouldKeepAskWarm", "ENABLED", "DISABLED"]}
    assert ev["Properties"]["Input"] == '{"mode": "warm"}'
