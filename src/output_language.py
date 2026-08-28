"""The language every customer-facing string is written in.

One definition, imported by every prompt builder that produces text a customer reads --
extraction, the daily/weekly/monthly reports, meeting minutes. Written once because three
copies of a language rule is three chances for one of them to drift, and a drifted copy
shows up as "some of my report is in Chinese" rather than as an error.

WHY THIS EXISTS: site conversation here is routinely Chinese, or Chinese and English mixed
in one sentence. Left alone the model answers in whatever language it heard, so a customer
opens their day and finds half the summaries in a language their organisation does not read.
The transcript is the record and stays as it was spoken; what is GENERATED for display is a
product surface, and it is English.

THE TWO CARVE-OUTS ARE NOT OPTIONAL

* **Verbatim quotes stay verbatim.** `evidence` entries are checked mechanically against
  the transcript the model actually saw (`evidence_match.check_quote`). A translated quote
  matches nothing, so translating them would not merely lose the original words -- it would
  turn every citation into an apparent fabrication. The same holds for any field whose whole
  purpose is "these are the words that were said".
* **Proper nouns are not translated.** People, sites, companies, streets, product names.
  A person named 小明 is not "Xiaoming" in one report and "Ming" in the next, and a site
  called 华南工地 is not something a reader can match against the site list. Transliterating
  identifiers is how two records of the same thing stop being the same thing.
"""

# Appended to the instruction block of every customer-facing prompt.
OUTPUT_LANGUAGE_RULE = """
LANGUAGE OF OUTPUT. Write EVERY field a person reads in ENGLISH, whatever language the
transcript is in -- titles, summaries, action items, findings, decisions, questions,
recommendations. A Chinese or mixed Chinese/English conversation still produces an English
record. Translate the MEANING; do not transliterate and do not leave the original beside it.
Two exceptions, both narrow:
  - Text that is explicitly a QUOTE of what was said stays exactly as spoken, in its
    original language and wording. Never translate a quote.
  - Proper nouns -- people, sites, companies, places, product names -- keep the form they
    appear in. Do not translate or romanise a name."""
