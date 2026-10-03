"""Naming an inspection of a place marks the place (owner, 2026-10-03).

Measured on TEST with the deployed model, final pass, the owner's prod demo
(Ben_Lin_Test 2026-10-02): "level one inspections" was marked 0/8 and "the
Tikaha room inspections" 1/8 -- the prompt allowed it but did not say it. The
measurement, not this test, is the evidence the rule works; this pins that
the rule reaches the prompt.
"""
import lambda_extract_session as es


def test_the_rule_is_in_the_extraction_instructions():
    block = es._instructions_block()
    assert "NAMING AN INSPECTION OF A PLACE MARKS THE PLACE." in block
    assert "here is the Te Kaha room inspection" in block
    assert block.index("NAMING AN INSPECTION") < block.index("1. Split the transcript")
