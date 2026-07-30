"""Tests for src/session_scope.py read-side helpers. Focused on
device_session_id: mapping a chunk session's `sid{32hex}` base back to the
32-hex meeting_session key (and rejecting legacy whole-file bases). Pure at
import."""
import session_scope as ss

HEX = "9f8c1e2a4b6d47f0a1b2c3d4e5f60718"   # 32 lowercase hex


def test_device_session_id_extracts_hex_from_a_chunk_base():
    assert ss.device_session_id(f"sid{HEX}") == HEX


def test_device_session_id_is_none_for_a_legacy_whole_file_base():
    # `{device}_{date}_{HH-MM-SS}` -> no device session
    assert ss.device_session_id("Benl1_2026-07-25_13-05-12") is None


def test_device_session_id_is_none_for_empty_or_none():
    assert ss.device_session_id("") is None
    assert ss.device_session_id(None) is None


def test_device_session_id_rejects_wrong_length_or_non_hex():
    assert ss.device_session_id("sid" + "a" * 31) is None      # too short
    assert ss.device_session_id("sid" + "a" * 33) is None      # too long
    assert ss.device_session_id("sid" + "g" * 32) is None      # non-hex
    assert ss.device_session_id(f"sid{HEX.upper()}") is None   # must be lowercase


def test_device_session_id_requires_the_bare_base_not_a_full_chunk_key():
    # the base is `sid{hex}`, not a full chunk key with device/index tokens;
    # a stray leading token or a chunk suffix is NOT a session base.
    assert ss.device_session_id(f"Benl1_2026-07-25_13-05-12_sid{HEX}") is None
    assert ss.device_session_id(f"sid{HEX}_c0007") is None
