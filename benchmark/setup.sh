#!/usr/bin/env bash
# One-shot environment setup for the ASR benchmark (Codespaces / any Debian-ish
# Linux). Installs ffmpeg+ffprobe if missing and the Python dependencies.
# Usage:  bash benchmark/setup.sh
set -e
cd "$(dirname "$0")"

if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
    echo "→ installing ffmpeg/ffprobe…"
    if command -v sudo >/dev/null 2>&1; then
        sudo apt-get update -y && sudo apt-get install -y ffmpeg
    else
        apt-get update -y && apt-get install -y ffmpeg
    fi
else
    echo "✓ ffmpeg/ffprobe already present"
fi

echo "→ installing Python dependencies…"
pip install -r requirements.txt

echo
echo "✅ ready — start the app with:"
echo "   python -m streamlit run benchmark/app.py --server.address 0.0.0.0"
