"""The screen turn's progress states are a contract with the UI.

fieldsight-ui scripts/api/ask.js PROGRESS_STATUSES lists every non-terminal
status a screen turn may report. A state added here without that list learning
it reads as a failure there: the first poll cancels a healthy turn and the
screen says "FieldSight is busy" (2026-10-21, fixed by ui#443). Change this
pin only together with PROGRESS_STATUSES.
"""
import ask_voice_turn
import lambda_fieldsight_api as api


def test_screen_states_match_the_ui_progress_list():
    assert ask_voice_turn._SCREEN_STATES == ("searching_records", "searching_web", "writing")


def test_every_screen_state_is_accepted_as_since():
    for state in ("pending",) + ask_voice_turn._SCREEN_STATES:
        assert state in api._SCREEN_SINCE
