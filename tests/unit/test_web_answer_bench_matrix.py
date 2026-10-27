"""Web-answer bench, prompt x model matrix (plan 2026-10-28-web-answer-prompt-matrix).

Fakes only: no SDK, no network. The modules are loaded by path because the
directory name has a hyphen.
"""
import importlib.util
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BENCH_DIR = os.path.join(REPO, "tools", "web-answer-bench")
SRC = os.path.join(REPO, "src")
for _p in (BENCH_DIR, SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BENCH_DIR, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


import web_answer                                        # noqa: E402

prompts = _load("prompts")


# ------------------------------------------------------- Task 1: prompt texts

# git show 518900a8:src/web_answer.py, VERIFY_PROMPT ("colleague").
VERIFY_V1 = """A colleague answered the question below from memory. Search the open web
to check it. Keep what the sources confirm, correct what they contradict (citing
the source), and mark anything still unconfirmed as [unverified]. Be brief.

## Question
{question}

## Colleague's answer
{draft}
"""

# git show 518900a8^:src/web_answer.py, WEB_PROMPT.
WEB_V1 = """Search the open web and answer this question, for a reader working
on a New Zealand construction site.

Report only what public sources say. Do not speculate, and do not fill a gap with
general knowledge -- if the sources do not answer the question, say so plainly
rather than guessing.

Name the source of each fact you use. Be brief.

## Question
{question}
"""


def test_shipped_prompts_match_the_constants():
    assert prompts.load("draft_v1") == web_answer.DRAFT_PROMPT
    assert prompts.load("verify_v2") == web_answer.VERIFY_PROMPT


def test_historical_prompts_match_git():
    assert prompts.load("verify_v1") == VERIFY_V1
    assert prompts.load("web_v1") == WEB_V1


def test_candidates_and_placeholders():
    assert prompts.CANDIDATES == {"draft_v1": "draft", "verify_v1": "verify",
                                  "verify_v2": "verify", "web_v1": "single",
                                  "merged_v1": "single"}
    for pid, step in prompts.CANDIDATES.items():
        text = prompts.load(pid)
        assert "{question}" in text
        assert ("{draft}" in text) == (step == "verify"), pid
        # Each one fills with the keys its step supplies and nothing else.
        text.format(question="Q", draft="D")


def test_sha_is_of_the_file_text():
    import hashlib
    assert prompts.sha("draft_v1") == \
        hashlib.sha256(web_answer.DRAFT_PROMPT.encode("utf-8")).hexdigest()


def test_unknown_prompt_id_refused():
    with pytest.raises(KeyError):
        prompts.load("../set.v1")
