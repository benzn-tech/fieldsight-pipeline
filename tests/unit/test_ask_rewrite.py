import ask_rewrite


class FakeReply:
    def __init__(self, text=None, ok=True, error=None):
        self.ok, self.text, self.error = ok, text, error


def _call(reply, counter):
    def call(prompt, **kw):
        counter.append(dict(kw, prompt=prompt))
        if isinstance(reply, Exception):
            raise reply
        return reply
    return call


HISTORY = [{"question": "what did James say about the ceiling grid?",
            "answer": "James said the grid on level 3 is behind."}]


def test_no_history_costs_no_model_call():
    calls = []
    text, rewritten = ask_rewrite.standalone_question(
        "what is the fire rating?", [], call=_call(FakeReply("x"), calls), timeout=4.0)
    assert (text, rewritten) == ("what is the fire rating?", False)
    assert calls == [], "the common case must not pay for a model call"


def test_a_follow_up_is_rewritten():
    calls = []
    text, rewritten = ask_rewrite.standalone_question(
        "when is he finishing it?", HISTORY,
        call=_call(FakeReply("when is James finishing the level 3 ceiling grid?"), calls),
        timeout=4.0)
    assert text == "when is James finishing the level 3 ceiling grid?"
    assert rewritten is True
    assert len(calls) == 1


def test_a_raising_transport_returns_the_original():
    text, rewritten = ask_rewrite.standalone_question(
        "when is he finishing it?", HISTORY,
        call=_call(RuntimeError("boom"), []), timeout=4.0)
    assert (text, rewritten) == ("when is he finishing it?", False)


def test_an_overlong_reply_is_refused():
    text, rewritten = ask_rewrite.standalone_question(
        "when is he finishing it?", HISTORY,
        call=_call(FakeReply("x" * 301), []), timeout=4.0)
    assert rewritten is False


def test_a_multiline_reply_is_refused():
    text, rewritten = ask_rewrite.standalone_question(
        "when is he finishing it?", HISTORY,
        call=_call(FakeReply("a?\nb?"), []), timeout=4.0)
    assert rewritten is False


def test_an_empty_reply_is_refused():
    text, rewritten = ask_rewrite.standalone_question(
        "when is he finishing it?", HISTORY,
        call=_call(FakeReply("   "), []), timeout=4.0)
    assert rewritten is False


def test_a_budget_below_the_client_floor_skips_the_call():
    calls = []
    text, rewritten = ask_rewrite.standalone_question(
        "when is he finishing it?", HISTORY,
        call=_call(FakeReply("x?"), calls), timeout=1.0)
    assert (text, rewritten) == ("when is he finishing it?", False)
    assert calls == [], "below MIN_USEFUL_TIMEOUT the client refuses anyway"


def test_a_not_ok_reply_returns_the_original():
    text, rewritten = ask_rewrite.standalone_question(
        "when is he finishing it?", HISTORY,
        call=_call(FakeReply(None, ok=False, error="429"), []), timeout=4.0)
    assert (text, rewritten) == ("when is he finishing it?", False)


def test_the_rewrite_call_is_tagged():
    """The one production call site an AST alias-scoped guard cannot see,
    because `standalone_question` receives its transport as an injected `call`
    parameter rather than importing `corroboration_client` itself (see the
    module's own PURE docstring). Covered here directly instead: the LLM_USAGE
    comparison this telemetry exists for -- rewrite vs web verdict vs web
    answer -- depends on this tag never silently reverting to "unknown"."""
    calls = []
    ask_rewrite.standalone_question(
        "when is he finishing it?", HISTORY,
        call=_call(FakeReply("when is James finishing the level 3 ceiling grid?"), calls),
        timeout=4.0)
    assert len(calls) == 1
    assert calls[0]["caller"] == "rewrite"
