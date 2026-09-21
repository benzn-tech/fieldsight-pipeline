"""Shared, dependency-free text -> literal-term extraction.

Split out of lambda_ask_agent.py (where `_lexical_terms` originated, driving
`_aggregate_topics`' title-substring ranking) so that lambda_rag_search.py can
use the SAME extraction to build the keyword arm's tsquery (2026-09-22 fix:
"the keyword arm can actually fire"). Both lambdas import this module rather
than one importing the other's file -- lambda_ask_agent.py pulls in boto3
clients, S3 config, and several sibling modules (batch_stitch,
deletion_mirror, transcript_utils) at import time that lambda_rag_search.py
(in-VPC, no internet egress, BUG-36) has no business depending on.

Pure functions only: no psycopg, no boto3, no I/O.
"""
import re

# Latin/digit runs, and the scripts written without spaces between words (CJK
# ideographs and kana). They need different handling, which is the whole point:
# splitting on `[^a-z0-9]+` produced NO terms at all for a Chinese question, so
# no row could ever be `lexical`, the lexical-first ordering did nothing, and
# only rows inside _NO_LEX_MAX_DIST survived. The same question asked in Chinese
# returned strictly less than in English, and sometimes nothing.
_LATIN_RUN = re.compile(r"[a-z0-9]+")
_UNSPACED_RUN = re.compile(r"[㐀-䶿一-鿿぀-ヿ豈-﫿]+")
# A token mixing letters and digits is an identifier — "b2", "k1", "sb1108" —
# and the 3-character floor was dropping exactly the zone and grid references
# people search for, in English as much as in Chinese.
_IDENTIFIER = re.compile(r"[a-z]\d|\d[a-z]")


def lexical_terms(question):
    """Terms for the literal-containment half of the hybrid ranking, and
    (2026-09-22) the source of the keyword arm's OR query in lambda_rag_search.

    Latin runs keep the 3-character floor, which exists so "we"/"is"/"of" do not
    make every row lexical; an identifier is exempt from it.

    A run of an unspaced script becomes overlapping 2-character shingles rather
    than one long token: "钢筋合格证" as a whole almost never appears verbatim in
    a title, while "钢筋" does. Standard cheap approach, and it leaves the
    ranking design untouched — Chinese simply gets to participate in it.

    Other scripts (Cyrillic, Greek, …) yield no terms, exactly as today.
    Shingling a space-separated script would over-match, and no product language
    needs it yet.
    """
    q = (question or "").lower()
    terms = [t for t in _LATIN_RUN.findall(q)
             if len(t) >= 3 or _IDENTIFIER.search(t)]
    for run in _UNSPACED_RUN.findall(q):
        terms.extend(run[i:i + 2] for i in range(len(run) - 1))
    return terms


def or_query(terms):
    """Join extracted terms into ONE `websearch_to_tsquery` input string that
    parses as an OR, not an AND -- the fix for Cause A of "the keyword arm can
    actually fire" (2026-09-22): binding the caller's whole question as
    `%(q_text)s` made `websearch_to_tsquery` AND every non-stopword term
    together, which a natural-language question essentially never satisfies.

    Measured: `websearch_to_tsquery('simple', 'ps4 or light or pole')` ->
    `'ps4' | 'light' | 'pole'`. `lexical_terms` only ever emits lowercase
    alnum runs (no spaces, no quotes, no literal "or"), so the terms are safe
    to join with the bare word `or` and no further escaping.

    An empty term list returns "" -- `websearch_to_tsquery('simple', '')` is
    an empty tsquery that matches NOTHING, never everything. This is the
    property `test_a_conceptual_query_with_no_literal_terms_is_unaffected`
    (and lambda_rag_search's own tests) depend on: a question with no literal
    terms must leave the keyword arm inert, not turn it into a match-all.
    """
    return " or ".join(terms)
