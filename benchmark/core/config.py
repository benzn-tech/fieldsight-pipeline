"""Configuration loading.

Precedence (low -> high): .env file, process env vars, Streamlit secrets, and
finally any per-session overrides typed into the sidebar. Returns a plain dict
consumed by the provider adapters.
"""
from __future__ import annotations

import os

CONFIG_KEYS = [
    "ANTHROPIC_API_KEY", "JUDGE_MODEL",
    "CARTESIA_API_KEY", "CARTESIA_VERSION", "CARTESIA_MODEL",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION",
    "AWS_TRANSCRIBE_BUCKET", "AWS_TRANSCRIBE_PREFIX",
    "ZHIPU_API_KEY", "ZHIPU_BASE_URL", "ZHIPU_MODEL",
    "DASHSCOPE_API_KEY", "DASHSCOPE_REGION", "QWEN_ASR_MODEL", "PARAFORMER_MODEL",
    "XFYUN_APPID", "XFYUN_SECRET_KEY",
]

_DEFAULTS = {
    "JUDGE_MODEL": "claude-sonnet-4-6",
    "CARTESIA_VERSION": "2025-04-16",
    "CARTESIA_MODEL": "ink-whisper",
    "AWS_REGION": "ap-southeast-2",
    "AWS_TRANSCRIBE_BUCKET": "fieldsight-data-509194952652",
    "AWS_TRANSCRIBE_PREFIX": "asr-benchmark/",
    "ZHIPU_BASE_URL": "https://api.z.ai/api/paas/v4",
    "ZHIPU_MODEL": "glm-asr-2512",
    "DASHSCOPE_REGION": "intl",
    "QWEN_ASR_MODEL": "qwen3-asr-flash",
    "PARAFORMER_MODEL": "paraformer-v2",
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
