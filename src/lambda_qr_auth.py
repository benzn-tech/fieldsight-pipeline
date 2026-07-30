"""Cognito custom-auth triggers for QR passwordless sign-in.

Flow: DefineAuthChallenge -> CreateAuthChallenge -> VerifyAuthChallengeResponse.
The terminal answers a single CUSTOM_CHALLENGE with a one-time code minted by an
authenticated web session (org-api POST /api/org/auth/qr/create) and stored in
DynamoDB. Identity is matched by the Cognito `sub` (robust to email-alias pools).
The code value is never logged.
"""
import logging
import os
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("QR_CODES_TABLE", "fieldsight-qr-login-codes")
_ddb_table = None


def _table():
    global _ddb_table
    if _ddb_table is None:
        _ddb_table = boto3.resource("dynamodb").Table(TABLE_NAME)
    return _ddb_table


def define_auth_challenge(event, context):
    session = event["request"].get("session") or []
    resp = event["response"]
    if not session:
        # First step: present our custom challenge.
        resp["issueTokens"] = False
        resp["failAuthentication"] = False
        resp["challengeName"] = "CUSTOM_CHALLENGE"
    else:
        last = session[-1]
        if last.get("challengeName") == "CUSTOM_CHALLENGE" and last.get("challengeResult") is True:
            resp["issueTokens"] = True
            resp["failAuthentication"] = False
        else:
            resp["issueTokens"] = False
            resp["failAuthentication"] = True
    return event


def create_auth_challenge(event, context):
    if event["request"].get("challengeName") == "CUSTOM_CHALLENGE":
        # Nothing to send — the terminal already holds the code from the QR.
        event["response"]["publicChallengeParameters"] = {}
        event["response"]["privateChallengeParameters"] = {}
        event["response"]["challengeMetadata"] = "QR_CODE"
    return event


def verify_auth_challenge_response(event, context):
    answer = (event["request"].get("challengeAnswer") or "").strip()
    sub = (event["request"].get("userAttributes") or {}).get("sub", "")
    event["response"]["answerCorrect"] = _verify_and_consume(sub, answer)
    return event


def _verify_and_consume(sub, code):
    if not code or not sub:
        return False
    try:
        item = _table().get_item(Key={"code": code}).get("Item")
    except Exception:
        logger.exception("qr verify get_item failed")
        return False
    if not item or item.get("sub") != sub or item.get("consumed"):
        return False
    if int(time.time()) >= int(item.get("expiresAt", 0)):
        return False
    # Atomic single-use: only the first verifier flips consumed false->true.
    try:
        _table().update_item(
            Key={"code": code},
            UpdateExpression="SET consumed = :t",
            ConditionExpression="consumed = :f",
            ExpressionAttributeValues={":t": True, ":f": False},
        )
    except Exception:
        return False  # lost the race / conditional failed → invalid
    return True
