"""Configuration loading.

Precedence (low -> high): .env file, process env vars, Streamlit secrets, and
finally any per-session overrides typed into the sidebar. Returns a plain dict
consumed by the provider adapters.
"""
from __future__ import annotations

import os

CONFIG_KEYS = [
    "JUDGE_MODEL",
    "CARTESIA_API_KEY", "CARTESIA_VERSION", "CARTESIA_MODEL",
    "ELEVENLABS_API_KEY", "ELEVENLABS_MODEL",
    "PLAUD_CLIENT_ID", "PLAUD_API_KEY", "PLAUD_SECRET_KEY", "PLAUD_REGION_HOST", "PLAUD_MODEL",
    "PLAUD_AUDIO_SOURCE",
    "SONIOX_API_KEY", "SONIOX_MODEL",
    "DOUBAO_API_KEY", "DOUBAO_APP_ID", "DOUBAO_ACCESS_TOKEN", "DOUBAO_RESOURCE_ID",
    "DOUBAO_API_BASE",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION",
    "AWS_TRANSCRIBE_BUCKET", "AWS_TRANSCRIBE_PREFIX",
    "ZHIPU_API_KEY", "ZHIPU_BASE_URL", "ZHIPU_MODEL",
    "DASHSCOPE_API_KEY", "DASHSCOPE_REGION", "QWEN_ASR_MODEL", "FUNASR_MODEL",
]

_DEFAULTS = {
    "JUDGE_MODEL": "qwen3.7-max",
    "CARTESIA_VERSION": "2025-04-16",
    "CARTESIA_MODEL": "ink-2",
    "ELEVENLABS_MODEL": "scribe_v2",
    "PLAUD_REGION_HOST": "https://platform-us.plaud.ai/developer/api",
    "PLAUD_MODEL": "plaud-fast-whisper",
    "PLAUD_AUDIO_SOURCE": "auto",
    "SONIOX_MODEL": "stt-async-v5",
    "DOUBAO_RESOURCE_ID": "volc.seedasr.auc",
    "DOUBAO_API_BASE": "https://openspeech.bytedance.com",
    "AWS_REGION": "ap-southeast-2",
    "AWS_TRANSCRIBE_BUCKET": "fieldsight-data-509194952652",
    "AWS_TRANSCRIBE_PREFIX": "asr-benchmark/",
    "ZHIPU_BASE_URL": "https://api.z.ai/api/paas/v4",
    "ZHIPU_MODEL": "glm-asr-2512",
    "DASHSCOPE_REGION": "intl",
    "QWEN_ASR_MODEL": "qwen3-asr-flash",
    "FUNASR_MODEL": "fun-asr",
}


def load_config(overrides: dict | None = None) -> dict:
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
    except Exception:
        pass

    cfg = dict(_DEFAULTS)
    for k in CONFIG_KEYS:
        v = os.environ.get(k)
        if v:
            cfg[k] = v

    # Streamlit secrets (optional)
    try:
        import streamlit as st
        for k in CONFIG_KEYS:
            if k in st.secrets:
                cfg[k] = st.secrets[k]
    except Exception:
        pass

    if overrides:
        for k, v in overrides.items():
            if v:
                cfg[k] = v
    return cfg
