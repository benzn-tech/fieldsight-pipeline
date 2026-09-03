"""answer_language.py -- what language a customer-facing answer comes back in.

Pure. No boto3, no psycopg, no network, no model.

THE INSTRUCTION WAS ALREADY THERE AND IT ALREADY LEAKED. Both Ask system
contexts have ended with "Answer in English" since before this module existed,
and measured against the deployed model (prod runs `qwen3.6-flash`, a
Chinese-first model) a Chinese question came back in Chinese 1 time in 13. So
this module is not another sentence in the prompt -- it is the two things that
work when a sentence does not:

  * POSITION. `tail_rule()` goes AFTER the user's question, which is the last
    thing the model reads. The existing rule sits in a list at the top, and what
    is nearest the end is what a model is most likely to still be holding.
  * A CHECK ON THE OUTPUT. `violates()` reads what came back rather than
    trusting that the instruction was followed. The caller retries once and logs
    either way, so a leak is visible instead of arriving at a customer.

`policy()` is the switch the user asked for: English only for now, with the
"follow the question" branch already written so turning it on is an env change
and not a code change. The number this module exists to move is the leak rate,
so `violates` must stay measurable on its own.
"""
import os
import re

__all__ = ["policy", "tail_rule", "violates", "EN", "QUESTION"]

EN = "en"
QUESTION = "question"

# CJK ideographs, kana and hangul. A stray character is not a violation -- a
# place name or a person's name can legitimately appear in an English answer,
# and prod transcripts carry both -- so the caller measures a RATIO, not a hit.
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯豈-﫿]")

# Above this share of the answer, the model changed language rather than quoting
# a name. Chosen to sit well above a quoted proper noun in an English paragraph
# and well below a Chinese sentence, which is almost entirely CJK.
_RATIO = 0.05


def policy():
    """`en` (default) or `question`.

    Default is `en` because that is the product decision today -- customer-facing
    answers are English-only until someone decides otherwise (user, 2026-09-03).
    An unknown value falls back to `en` rather than raising: this reads an env
    var, and a typo in a deploy must not take Ask down.
    """
    return QUESTION if os.environ.get("ASK_ANSWER_LANGUAGE") == QUESTION else EN


def tail_rule(insist=False):
    """The language rule, for the END of the prompt. None when nothing to say.

    `insist` is for the retry after a violation. It names what went wrong,
    because a second identical prompt is a second roll of the same dice.
    """
    if policy() != EN:
        # Follow the question. Not forced and not checked: there is no
        # deterministic target to check against, and guessing one from the
        # question would make the check less reliable than the model.
        return ("## Language\nAnswer in the same language the question was "
                "asked in.")
    if insist:
        return ("## Language\nYour previous answer was not in English. Write "
                "this answer entirely in English. Translate any quoted material "
                "into English rather than reproducing it in its original "
                "script.")
    return ("## Language\nAnswer in English, whatever language the question was "
            "asked in.")


def violates(text):
    """Whether this answer broke the `en` policy.

    False under the `question` policy -- there is nothing to violate. False for
    an empty answer: an empty string is a different failure and reporting it as
    a language leak would send the caller down the wrong path.
    """
    if policy() != EN or not text:
        return False
    cjk = len(_CJK.findall(text))
    return cjk > 0 and (cjk / len(text)) > _RATIO
