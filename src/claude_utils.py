"""
claude_utils.py — Shared direct-Claude helper (Phase 4b, Task 2).

Copies the urllib3 HTTP call pattern and three-tier JSON parse verbatim from
lambda_report_generator.py (call_claude_structured :410-441,
extract_json_from_response :443-462), renamed for use by non-report Lambdas
(starting with lambda_extract_session) that need a direct Claude call without
importing the report generator module. The report generator itself is left
untouched here — a shared-core refactor is a separate concern.

Environment Variables:
    ANTHROPIC_API_KEY   - Anthropic API key (sk-ant-xxx)
    CLAUDE_MODEL        - Claude model ID (default: claude-sonnet-4-6)
"""
import json
import logging
import os
import re

import urllib3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
CLAUDE_MODEL = os.environ.get('CLAUDE_MODEL', 'claude-sonnet-4-6')


def call_claude(prompt, max_tokens=4096):
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set")
        return None, "ANTHROPIC_API_KEY not configured"
    http = urllib3.PoolManager()
    body = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}]
    })
    try:
        resp = http.request(
            'POST', 'https://api.anthropic.com/v1/messages',
            body=body,
            headers={
                'Content-Type': 'application/json',
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
            },
            # 150s, not the Lambda function's 180s Timeout (template.yaml
            # ExtractSessionFunction): the HTTP client must lose that race
            # so we get a catchable urllib3 TimeoutError -- with both at
            # 180s the Lambda runtime can hard-kill the invocation first,
            # skipping our error handling/logging entirely.
            timeout=150.0,
        )
        data = json.loads(resp.data.decode('utf-8'))
        if resp.status == 200:
            text_blocks = [b['text'] for b in data.get('content', []) if b.get('type') == 'text']
            return '\n'.join(text_blocks), None
        else:
            err = data.get('error', {}).get('message', f'HTTP {resp.status}')
            logger.error(f"Claude API error: {err}")
            return None, err
    except Exception as e:
        logger.error(f"Claude API call failed: {str(e)}")
        return None, str(e)


def extract_json(raw_text):
    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw_text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(raw_text.strip())
    except json.JSONDecodeError:
        pass
    first_brace = raw_text.find('{')
    last_brace = raw_text.rfind('}')
    if first_brace != -1 and last_brace != -1:
        try:
            return json.loads(raw_text[first_brace:last_brace + 1])
        except json.JSONDecodeError:
            pass
    logger.error(f"Failed to extract JSON from Claude response: {raw_text[:500]}")
    return None
