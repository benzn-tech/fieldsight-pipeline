"""Shared AWS credential detection (no network calls).

Used so the UI only shows AWS Transcribe / Fun-ASR as "configured" (🟢) when
credentials can actually be resolved — not just because a default bucket name is
present. Avoids the misleading "green but fails at runtime" trap.
"""
from __future__ import annotations

import os


def aws_creds_available(config: dict) -> bool:
    if config.get("AWS_ACCESS_KEY_ID") and config.get("AWS_SECRET_ACCESS_KEY"):
        return True
    if (os.environ.get("AWS_ACCESS_KEY_ID")
            or os.environ.get("AWS_PROFILE")
            or os.environ.get("AWS_ROLE_ARN")
            or os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE")):
        return True
    return os.path.exists(os.path.expanduser("~/.aws/credentials")) or \
        os.path.exists(os.path.expanduser("~/.aws/config"))
