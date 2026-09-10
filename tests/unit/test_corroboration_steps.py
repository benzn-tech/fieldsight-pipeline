"""The four steps, and the two things they refuse to do.

Spec: docs/superpowers/specs/2026-08-31-ask-external-corroboration-design.md §5.2, §5.3

Every model call is stubbed. What is under test is not whether haiku classifies
well -- no unit test can show that -- but whether this module reports only what
it was actually told, and stays quiet when it was told nothing usable. Those are
the failures that turn a corroboration card into a claim we cannot support.
"""
import json

import pytest

steps = pytest.importorskip("corroboration")
client = pytest.importorskip("corroboration_client")


class FakeCall:
    """Stands in for `corroboration_client.call`, one queued Reply per step."""

    def __init__(self, *replies):
        self.queue = list(replies)
        self.calls = []

    def __call__(self, prompt, **kw):
        self.calls.append({"prompt": prompt, **kw})
        if not self.queue:
            raise AssertionError("a step called the model more times than expected")
        return self.queue.pop(0)


def reply(text="", results=(), error=None, search_error=None, searched=None,
          timed_out=False):
    """`searched` defaults to "there were results", which is the common case.
    Pass it explicitly for the two that matter on their own: a search that ran
    and honestly found nothing (searched=True, no results), and a model that
    wrote about a search it never performed (searched=False, prose present)."""
    return client.Reply(text=text,
                        search_results=[client.SearchResult(u, t) for u, t in results],
                        error=error, search_error=search_error,
                        searched=bool(results) if searched is None else searched,
                        timed_out=timed_out)


def extraction(*items):
    return reply(text=json.dumps(list(items)))


def verdicts(*items):
    return reply(text=json.dumps(list(items)))


NAYLOR = {"entity": "Naylor Love Construction", "kind": "company",
          "claim": "was the main contractor on the job"}
SOURCES = [("https://naylorlove.co.nz/about", "About Naylor Love")]


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("ENABLE_EXTERNAL_CORROBORATION", "true")


def _run(monkeypatch, *replies, question="who built it", answer="Naylor Love built it"):
    fake = FakeCall(*replies)
    monkeypatch.setattr(steps.client, "call", fake)
    return steps.corroborate(question, answer), fake


# ------------------------------------------------------------------ the happy path

def test_a_corroborated_claim_becomes_a_card_with_its_sources(monkeypatch):
    out, _ = _run(monkeypatch,
                  extraction(NAYLOR),
                  reply(text="Naylor Love is a large NZ contractor.", results=SOURCES),
                  verdicts({"entity": "Naylor Love Construction",
                            "state": "corroborated", "summary": "Public sources agree."}))
    assert len(out["corroborations"]) == 1
    card = out["corroborations"][0]
    assert card["state"] == "corroborated"
    assert card["claim"] == "was the main contractor on the job"
    assert card["sources"][0]["url"] == "https://naylorlove.co.nz/about"
    assert card["retrieved_at"].endswith("Z")
    assert out["timed_out"] is False and out["truncated"] is False


def test_conflicts_survives_the_pipeline(monkeypatch):
    """The state the feature exists for, and the easiest one to lose to an
    implementation that renders every finding as a summary."""
    out, _ = _run(monkeypatch, extraction(NAYLOR),
                  reply(text="...", results=SOURCES),
                  verdicts({"entity": "Naylor Love Construction",
                            "state": "conflicts", "summary": "Sources name another firm."}))
    assert out["corroborations"][0]["state"] == "conflicts"


def test_no_checkable_claim_carries_no_sources(monkeypatch):
    """A card that cites sources invites the reader to hear it as verification.
    This state is a judgement about our own answer, and it has nothing to cite."""
    out, _ = _run(monkeypatch, extraction(NAYLOR),
                  reply(text="...", results=SOURCES),
                  verdicts({"entity": "Naylor Love Construction",
                            "state": "no_checkable_claim", "summary": "Only named."}))
    card = out["corroborations"][0]
    assert card["state"] == "no_checkable_claim"
    assert card["sources"] == []


# ------------------------------------------------ it never reports a state it lacks

@pytest.mark.parametrize("bad", ["verified", "TRUE", "", None, "corroborated!", 1])
def test_a_state_outside_the_enum_drops_the_card(monkeypatch, bad):
    """Every available repair invents a finding: `corroborated` invents
    agreement, `not_found` invents an empty search, `no_checkable_claim` invents
    a judgement about the answer. So the entity is dropped and counted."""
    out, _ = _run(monkeypatch, extraction(NAYLOR),
                  reply(text="...", results=SOURCES),
                  verdicts({"entity": "Naylor Love Construction", "state": bad}))
    assert out["corroborations"] == []
    assert any(d["reason"] == "no usable state" for d in out["dropped"])


def test_an_entity_the_reconcile_step_ignored_is_dropped_not_guessed(monkeypatch):
    out, _ = _run(monkeypatch, extraction(NAYLOR),
                  reply(text="...", results=SOURCES),
                  verdicts())
    assert out["corroborations"] == []
    assert out["dropped"] == [{"entity": "Naylor Love Construction",
                               "reason": "no usable state"}]


def test_reconcile_returning_prose_instead_of_json_costs_the_cards(monkeypatch):
    out, _ = _run(monkeypatch, extraction(NAYLOR),
                  reply(text="...", results=SOURCES),
                  reply(text="Sure! Here is what I found: Naylor Love looks legitimate."))
    # No cards is the point; the model was chatty rather than late, and saying
    # "ran out of time" about a call that answered promptly invites a retry
    # that will fail in exactly the same way.
    assert out["corroborations"] == []
    assert out["failed"] is True and out["timed_out"] is False


def test_a_fenced_json_reply_is_still_read(monkeypatch):
    """Models add code fences. Treating that as unparseable would throw away a
    perfectly good verdict and report a timeout that did not happen."""
    fenced = reply(text='```json\n[{"entity": "Naylor Love Construction", '
                        '"state": "not_found", "summary": "nothing"}]\n```')
    out, _ = _run(monkeypatch, extraction(NAYLOR),
                  reply(text="...", results=SOURCES), fenced)
    assert out["corroborations"][0]["state"] == "not_found"


# ------------------------------------------------- it never salvages a failed search

def test_a_failed_search_yields_no_cards_and_says_so(monkeypatch):
    """Step 3 is one call covering every entity, so there is no per-entity
    progress to keep. A shorter list would read as "we checked and found
    nothing", which is a claim about the world rather than about us.

    Which of `timed_out` / `failed` it says is now the client's structural
    report, not the shape of the error string."""
    out, _ = _run(monkeypatch, extraction(NAYLOR),
                  reply(error="timeout", timed_out=True))
    assert out["corroborations"] == []
    assert out["timed_out"] is True


def test_a_failed_extraction_is_never_an_empty_answer(monkeypatch):
    """The original name said "is a timeout", and the intent behind it is the
    one that matters: a broken check must not look like a check that found
    nothing to do. That intent survives; the label got more precise.

    Reporting every step failure as a deadline was measured wrong on TEST
    (a format failure announced as a timeout in ten seconds), but simply
    dropping the flag would have made "nothing to check" and "the check broke"
    identical -- which is what this test was protecting against. Hence a third
    signal rather than one fewer."""
    late = _run(monkeypatch, reply(error="timeout", timed_out=True))[0]
    assert late["timed_out"] is True and late["failed"] is False

    broke = _run(monkeypatch, reply(error="HTTP 502"))[0]
    assert broke["timed_out"] is False and broke["failed"] is True

    nothing = _run(monkeypatch, extraction())[0]
    assert nothing["timed_out"] is False and nothing["failed"] is False


def test_an_answer_naming_nothing_external_is_not_a_timeout(monkeypatch):
    """An empty extraction is a correct result for most Ask answers. Reporting
    it as a timeout would make the honest case look like a fault."""
    out, _ = _run(monkeypatch, extraction())
    assert out == {"corroborations": [], "dropped": [], "truncated": False,
                   "timed_out": False, "searched": False, "failed": False}


# ------------------------------------------- a search that did not happen

# The rule these pin: **reconcile only ever runs on text the open web actually
# produced.** Until 2026-09-09 it ran on whatever the search step returned, and
# the test that used to sit here asserted that was fine -- on the reasoning that
# "the model still wrote something" and would report `not_found` on its own
# terms. Its fixture said "I could not search.", so the assumption held there.
#
# The assumption is false. Measured on OpenRouter 2026-09-08, three runs,
# meta/muse-spark-1.3-contributor with a web plugin returned HTTP 200, 1200
# tokens, ZERO web results, and prose that read:
#
#     "I'll search the web to verify the NZS 3604 claim.
#      Initial results support the claim..."
#
# Fed to reconcile that becomes `corroborated`, and a card renders a "Confirmed"
# chip with no sources beneath it, because the renderer omits the source row
# when `sources` is empty. A fabricated external corroboration shown to a
# customer as verified is the worst output this system can produce, and it was
# reachable against the vendor in production, not only the one being evaluated.
#
# So the model's account of its own tool use is no longer evidence. The
# structural fact is: did web results come back.


def test_prose_about_a_search_that_never_ran_produces_nothing(monkeypatch):
    """The muse-spark shape. Confident, plausible, and entirely ungrounded."""
    out, fake = _run(monkeypatch, extraction(NAYLOR),
                     reply(text="I'll search the web to verify this. "
                                "Initial results support the claim.",
                           searched=False))
    assert out["corroborations"] == []
    assert out["searched"] is False
    assert len(fake.calls) == 2, "reconcile must not have run on ungrounded text"


def test_not_searching_is_not_reported_as_running_out_of_time(monkeypatch):
    """`truncated` and `timed_out` are already kept apart because a reader who
    sees three cards deserves to know which. "We did not search" is a third
    thing: the request finished in four seconds. Calling it a timeout is a
    false statement about our own system."""
    out, _ = _run(monkeypatch, extraction(NAYLOR),
                  reply(text="Initial results support the claim.", searched=False))
    assert out["timed_out"] is False
    assert out["searched"] is False


def test_a_tool_error_with_nothing_to_show_for_it_produces_nothing(monkeypatch):
    """A `web_search_tool_result` error block with no results is the same
    situation wearing a different hat: the prose is ungrounded."""
    out, fake = _run(monkeypatch, extraction(NAYLOR),
                     reply(text="I could not search.",
                           search_error="max_uses_exceeded", searched=True))
    assert out["corroborations"] == []
    assert out["searched"] is False
    assert len(fake.calls) == 2


def test_a_search_that_ran_and_found_nothing_still_reaches_reconcile(monkeypatch):
    """The guard must not swallow the honest empty result. A search that ran
    and returned no results is a finding about the WORLD, and `not_found` is
    the state that says so. Over-blocking here would delete the feature's most
    common true answer."""
    out, fake = _run(monkeypatch, extraction(NAYLOR),
                     reply(text="No sources discuss this.", searched=True),
                     verdicts({"entity": "Naylor Love Construction",
                               "state": "not_found", "summary": "No sources reached."}))
    assert out["corroborations"][0]["state"] == "not_found"
    assert out["searched"] is True
    assert len(fake.calls) == 3


def test_the_happy_path_says_the_web_was_consulted(monkeypatch):
    """The flag is what the UI reads to choose its words, so the true case has
    to carry it too -- a flag only ever set on failure is one the reader cannot
    distinguish from absent."""
    out, _ = _run(monkeypatch, extraction(NAYLOR), reply(results=SOURCES),
                  verdicts({"entity": "Naylor Love Construction",
                            "state": "corroborated", "summary": "Confirmed."}))
    assert out["searched"] is True


# --------------------------------------------------------------- truncated vs timed_out

def test_truncated_is_the_cap_biting_and_not_a_deadline(monkeypatch):
    four = [dict(NAYLOR, entity=n) for n in
            ["Naylor Love Ltd", "Fletcher Building", "Hawkins Ltd", "Dominion Constructors"]]
    vs = [{"entity": e["entity"], "state": "not_found", "summary": "x"} for e in four]
    out, _ = _run(monkeypatch, extraction(*four),
                  reply(text="...", results=SOURCES), verdicts(*vs))
    assert out["truncated"] is True
    assert out["timed_out"] is False
    assert len(out["corroborations"]) == 3


def test_the_gate_refusing_everything_is_not_a_timeout(monkeypatch):
    """A privacy refusal and a missed deadline are different facts, and merging
    them would report the gate doing its job as an outage."""
    person = {"entity": "John Smith", "kind": "company", "claim": "was on site"}
    out, fake = _run(monkeypatch, extraction(person))
    assert out["timed_out"] is False
    assert out["dropped"][0]["reason"] == "shaped like a person's name"
    assert len(fake.calls) == 1, "it searched anyway"


def test_a_refused_entity_never_reaches_the_search_prompt(monkeypatch):
    """The gate's whole purpose, asserted against the string that actually
    leaves: the search step's prompt."""
    out, fake = _run(monkeypatch,
                     extraction(NAYLOR, {"entity": "Naylor Love $40k variation",
                                         "kind": "company", "claim": "was disputed"}),
                     reply(text="...", results=SOURCES),
                     verdicts({"entity": "Naylor Love Construction",
                               "state": "not_found", "summary": "x"}))
    search_prompt = fake.calls[1]["prompt"]
    assert "40k" not in search_prompt and "variation" not in search_prompt
    assert "Naylor Love Construction" in search_prompt


# ------------------------------------------------------------------------- the flag

def test_the_flag_is_read_at_call_time_not_import_time(monkeypatch):
    """A flag captured at import is a flag whose value is whatever the container
    started with. This repository has shipped switches that looked wired and only
    ever returned their default."""
    monkeypatch.setenv("ENABLE_EXTERNAL_CORROBORATION", "false")
    assert steps.enabled() is False
    monkeypatch.setenv("ENABLE_EXTERNAL_CORROBORATION", "true")
    assert steps.enabled() is True


def test_the_flag_defaults_to_off(monkeypatch):
    monkeypatch.delenv("ENABLE_EXTERNAL_CORROBORATION", raising=False)
    assert steps.enabled() is False


# ---------------------------------------------------------- the budget is respected

def test_no_step_is_given_more_than_its_slice(monkeypatch):
    out, fake = _run(monkeypatch, extraction(NAYLOR),
                     reply(text="...", results=SOURCES),
                     verdicts({"entity": "Naylor Love Construction",
                               "state": "not_found", "summary": "x"}))
    assert fake.calls[0]["timeout"] <= steps.EXTRACT_BUDGET
    assert fake.calls[1]["timeout"] <= steps.SEARCH_BUDGET
    assert fake.calls[2]["timeout"] <= steps.RECONCILE_BUDGET
    assert sum(c["timeout"] for c in fake.calls) <= steps.HARD_STOP_SECONDS


def test_a_slow_first_step_leaves_the_later_steps_less_not_more(monkeypatch):
    """The budget is a share of one deadline, not three independent ones. A step
    that reads its own constant would let a slow predecessor push the total past
    the proxy's 30 s and turn a shaped body into a gateway error."""
    ticks = iter([0.0, 20.0, 20.0, 20.0, 20.0, 20.0])
    fake = FakeCall(extraction(NAYLOR), reply(text="...", results=SOURCES),
                    verdicts({"entity": "Naylor Love Construction",
                              "state": "not_found", "summary": "x"}))
    monkeypatch.setattr(steps.client, "call", fake)
    steps.corroborate("q", "a", clock=lambda: next(ticks))
    # Expressed against the constant, not a literal: the property is "what is
    # left of the deadline", and a hard-coded number silently becomes an
    # assertion about the old budget the next time one changes.
    left = steps.HARD_STOP_SECONDS - 20.0
    assert fake.calls[1]["timeout"] <= left, "the search step ignored the elapsed time"
    assert fake.calls[1]["timeout"] < steps.SEARCH_BUDGET, "it took its full slice anyway"


def test_the_cheap_steps_never_send_effort(monkeypatch):
    """`output_config.effort` is a 400 on haiku. The client drops it, and these
    two call sites are why that guard exists."""
    _, fake = _run(monkeypatch, extraction(NAYLOR),
                   reply(text="...", results=SOURCES),
                   verdicts({"entity": "Naylor Love Construction",
                             "state": "not_found", "summary": "x"}))
    assert fake.calls[0]["effort"] is None
    assert fake.calls[2]["effort"] is None


def test_only_the_search_step_searches(monkeypatch):
    """Three steps, one budget. A classification step that quietly searched
    would spend the search step's slice a second time, inside a hard stop with
    no room for it -- and the reader would be told the check timed out."""
    _, fake = _run(monkeypatch, extraction(NAYLOR),
                   reply(text="...", results=SOURCES),
                   verdicts({"entity": "Naylor Love Construction",
                             "state": "not_found", "summary": "x"}))
    assert fake.calls[0].get("web") in (None, False)
    assert fake.calls[1]["web"] is True
    assert fake.calls[2].get("web") in (None, False)


# --------------------------------------------------------------- it never raises

@pytest.mark.parametrize("bad", [None, "", 0])
def test_a_missing_question_or_answer_is_an_empty_body(monkeypatch, bad):
    monkeypatch.setattr(steps.client, "call", FakeCall())
    assert steps.corroborate(bad, "answer")["corroborations"] == []
    assert steps.corroborate("question", bad)["corroborations"] == []


def test_extraction_returning_junk_does_not_raise(monkeypatch):
    for junk in ['{"not": "a list"}', "not json at all", ""]:
        out, _ = _run(monkeypatch, reply(text=junk))
        assert out["corroborations"] == []

def test_a_verdict_with_no_reason_is_not_a_card(monkeypatch):
    """Measured on the real API: haiku returned `conflicts` with an empty summary
    on a claim that was in fact correct. "The web disagrees" with nothing after
    it is an assertion the reader can neither check nor dismiss."""
    for state in ("corroborated", "conflicts"):
        out, _ = _run(monkeypatch, extraction(NAYLOR),
                      reply(text="...", results=SOURCES),
                      verdicts({"entity": "Naylor Love Construction",
                                "state": state, "summary": "   "}))
        assert out["corroborations"] == [], state
        assert out["dropped"][-1]["reason"] == "a verdict with no reason"


@pytest.mark.parametrize("state", ["not_found", "no_checkable_claim"])
def test_the_other_two_states_survive_an_empty_summary(monkeypatch, state):
    """"Nothing usable came back" and "nothing to check" are complete statements
    on their own; requiring prose there would drop honest results."""
    out, _ = _run(monkeypatch, extraction(NAYLOR),
                  reply(text="...", results=SOURCES),
                  verdicts({"entity": "Naylor Love Construction",
                            "state": state, "summary": ""}))
    assert out["corroborations"][0]["state"] == state


# ------------------------------------------ a source has to say where it is from

# The card puts the source domain under every claim, and it is the trust
# carrier: a reader decides "is this a source I believe" from the host, not from
# the URL. The UI derived that host by parsing the URL, which worked while the
# vendor returned real result URLs.
#
# This vendor does not. Measured 2026-09-08, a real annotation:
#
#   url:   https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQFb5s...
#   title: "wikipedia.org"
#
# So the host lives in `title` and the URL is an opaque Google redirect. Parsed
# naively, EVERY source under EVERY claim reads `vertexaisearch.cloud.google.com`
# -- which destroys the one thing this feature exists to give the reader, on a
# path whose whole promise is that external evidence is visibly separate from
# what the room said.


def _source_of(url, title):
    r = client.Reply(search_results=[client.SearchResult(url, title)])
    return steps._sources(r)[0]


def test_a_redirect_url_does_not_become_the_source_domain():
    s = _source_of("https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZ",
                   "wikipedia.org")
    assert s["domain"] == "wikipedia.org"


def test_a_real_url_still_names_its_own_host():
    """A vendor that returns real URLs puts a headline in `title`, not a host.
    Falling back to the URL is what keeps this correct for both shapes."""
    s = _source_of("https://naylorlove.co.nz/about", "About - Naylor Love")
    assert s["domain"] == "naylorlove.co.nz"


def test_www_is_not_part_of_who_published_it():
    s = _source_of("https://www.standards.govt.nz/nzs3604", "Standards New Zealand")
    assert s["domain"] == "standards.govt.nz"


def test_a_title_that_is_prose_is_never_mistaken_for_a_host():
    """`title` is only trusted when it IS a hostname. "Fletcher Building Ltd."
    ends in a dot-word and would pass a lazy check."""
    s = _source_of("https://fletcherbuilding.com/news", "Fletcher Building Ltd.")
    assert s["domain"] == "fletcherbuilding.com"


def test_a_source_with_no_usable_host_says_nothing_rather_than_guessing():
    s = _source_of("not a url", None)
    assert s["domain"] is None


def test_the_url_and_title_are_still_carried_unchanged():
    """The link still has to work, and the redirect is the only URL we were
    given. Inventing one from the domain would fabricate a citation."""
    s = _source_of("https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZ",
                   "wikipedia.org")
    assert s["url"].startswith("https://vertexaisearch.cloud.google.com/")
    assert s["title"] == "wikipedia.org"


def test_a_vendor_that_gives_no_date_leaves_it_empty_rather_than_inventing_one():
    """`published` is the second trust carrier next to the domain, and this
    vendor supplies no page age. Absent is honest; today's date would not be."""
    s = _source_of("https://example.org/a", "example.org")
    assert s["published"] is None


# ------------------------------------------- each slice against what was measured

# The existing test above asserts only that the slices SUM under the hard stop.
# Three wrong numbers satisfy that as easily as three right ones, and an earlier
# draft of this change proposed exactly that: extract 3 + search 15 + reconcile 4,
# reasoned from "classification is cheap" and never measured. It would have
# shipped green.
#
# Measured 2026-09-09 against the deployed vendor, USING THE PROMPTS IN THIS FILE
# rather than a paraphrase of them -- the paraphrase is what made the first
# attempt wrong, and it over-stated extract by more than double:
#
#     extract    n=8   2.41 2.47 2.48 2.50 2.58 2.72 2.76 2.79   max 2.79
#     reconcile  n=8   2.16 2.38 2.63 2.80 3.10 3.10 3.11 3.58   max 3.58
#     search     n=3   10.3 11.4 11.3                            max 11.4
#
# So of the three, only SEARCH was ever near its slice: 11.4s against 12s. The
# other two have been comfortable all along, and the budget did not need the
# rebalance the plan called for.
#
# One 13.09s outlier was seen on a single extract call and never reproduced in
# the following sixteen. It is why the slices keep real margin rather than
# hugging the maximum, and why nothing here is sized from a median.

MEASURED_WORST = {"extract": 2.79, "search": 11.4, "reconcile": 3.58}

# API Gateway terminates the integration at 29s no matter what this file says.
# A hard stop past it converts a shaped `timed_out` body into a raw gateway
# error, which is strictly worse: the module's contract is that a missed
# deadline is REPORTED, never disguised.
GATEWAY_CEILING = 29.0


@pytest.mark.parametrize("name,budget", [
    ("extract", "EXTRACT_BUDGET"),
    ("search", "SEARCH_BUDGET"),
    ("reconcile", "RECONCILE_BUDGET"),
])
def test_every_slice_clears_its_measured_worst_case(name, budget):
    """Not merely bigger -- bigger with room. A slice that overruns does not
    fail politely, it spends the NEXT step's budget and the reader is told the
    check ran out of time."""
    slice_s = getattr(steps, budget)
    worst = MEASURED_WORST[name]
    assert slice_s >= worst * 1.25, (
        "%s has %.1fs for a step measured at %.2fs; less than 25%% margin"
        % (name, slice_s, worst))


def test_the_hard_stop_still_fits_inside_the_gateway():
    """The one ceiling this file does not own."""
    assert steps.HARD_STOP_SECONDS < GATEWAY_CEILING


def test_the_slices_fit_inside_the_hard_stop_with_the_floor_to_spare():
    """Each step also needs MIN_USEFUL_TIMEOUT of slack before it, or the last
    one is refused for having no time left -- which reads to the caller as a
    timeout rather than as arithmetic."""
    total = steps.EXTRACT_BUDGET + steps.SEARCH_BUDGET + steps.RECONCILE_BUDGET
    assert total + client.MIN_USEFUL_TIMEOUT <= steps.HARD_STOP_SECONDS, (
        "%.1fs of slices plus the floor does not fit in %.1fs"
        % (total, steps.HARD_STOP_SECONDS))


# --------------------------------- a step that failed is not a step that ran late

# Measured on TEST, 2026-09-10, a real question against the real corpus:
#
#     corroboration: extraction failed: extraction did not return a list
#     timed_out: true      elapsed 10.7s
#
# The model returned something that was not a JSON array. That is a format
# failure, and the reader was told the check ran out of time -- in ten seconds,
# against a budget of twenty-seven. Same shape as the `searched` fix one
# section up: a failure wearing another failure's label sends whoever reads it
# at the wrong thing, and a timeout invites "try again later" when trying again
# will fail identically.
#
# There is already a truthful place for these. `searched: false` means the open
# web was not consulted, which is exactly what happened, and the UI renders it
# as "Couldn't check the web for this answer". No new flag is needed -- what is
# needed is for `timed_out` to stop claiming cases it does not own.


def test_an_unparseable_extraction_is_not_a_timeout(monkeypatch):
    """The measured case. Ten seconds is not a deadline being missed."""
    out, _ = _run(monkeypatch, reply(text="I could not find any entities."))
    assert out["timed_out"] is False, "a format failure was reported as a deadline"
    assert out["searched"] is False
    assert out["corroborations"] == []


def test_a_search_that_errored_is_not_a_timeout(monkeypatch):
    """An HTTP error from the vendor is the vendor's fault and ours to report;
    it is not our clock."""
    out, _ = _run(monkeypatch, extraction(NAYLOR), reply(error="HTTP 502"))
    assert out["timed_out"] is False
    assert out["searched"] is False


def test_a_reconcile_that_errored_is_not_a_timeout(monkeypatch):
    out, _ = _run(monkeypatch, extraction(NAYLOR), reply(results=SOURCES),
                  reply(text="not json at all"))
    assert out["timed_out"] is False


def test_a_step_that_really_ran_late_still_says_timed_out(monkeypatch):
    """The guard must not empty the flag of meaning. A client that reports a
    deadline structurally is still a deadline."""
    out, _ = _run(monkeypatch, extraction(NAYLOR),
                  reply(error="no time left (0.4s)", timed_out=True))
    assert out["timed_out"] is True


def test_running_out_of_budget_between_steps_still_says_timed_out(monkeypatch):
    """`left()` falling under the floor is this module's own clock, and the one
    case the flag was always right about."""
    ticks = iter([0.0, 0.0, 99.0, 99.0, 99.0, 99.0])
    fake = FakeCall(extraction(NAYLOR))
    monkeypatch.setattr(steps.client, "call", fake)
    out = steps.corroborate("q", "a", clock=lambda: next(ticks))
    assert out["timed_out"] is True
