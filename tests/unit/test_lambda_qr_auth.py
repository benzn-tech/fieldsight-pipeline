import time
import pytest

qr = pytest.importorskip("lambda_qr_auth", reason="requires boto3 (installed in CI)")


class FakeTable:
    """Minimal DynamoDB Table double: get_item + conditional update_item."""
    def __init__(self, item=None):
        self.item = dict(item) if item else None
        self.updated = False

    def get_item(self, Key):
        if self.item and self.item.get("code") == Key["code"]:
            return {"Item": dict(self.item)}
        return {}

    def update_item(self, Key, UpdateExpression, ConditionExpression, ExpressionAttributeValues):
        # emulate ConditionExpression "consumed = :f"
        if not self.item or self.item.get("consumed") is not False:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        self.item["consumed"] = True
        self.updated = True
        return {}


def _define(session):
    return {"request": {"session": session}, "response": {}}


def test_define_issues_custom_challenge_on_first_call():
    ev = qr.define_auth_challenge(_define([]), None)
    assert ev["response"]["challengeName"] == "CUSTOM_CHALLENGE"
    assert ev["response"]["issueTokens"] is False
    assert ev["response"]["failAuthentication"] is False


def test_define_issues_tokens_after_correct_answer():
    session = [{"challengeName": "CUSTOM_CHALLENGE", "challengeResult": True}]
    ev = qr.define_auth_challenge(_define(session), None)
    assert ev["response"]["issueTokens"] is True
    assert ev["response"]["failAuthentication"] is False


def test_define_fails_after_wrong_answer():
    session = [{"challengeName": "CUSTOM_CHALLENGE", "challengeResult": False}]
    ev = qr.define_auth_challenge(_define(session), None)
    assert ev["response"]["issueTokens"] is False
    assert ev["response"]["failAuthentication"] is True


def test_create_sets_empty_challenge_params():
    ev = qr.create_auth_challenge(
        {"request": {"challengeName": "CUSTOM_CHALLENGE"}, "response": {}}, None)
    assert ev["response"]["publicChallengeParameters"] == {}
    assert ev["response"]["privateChallengeParameters"] == {}


def _verify_event(code, sub):
    return {
        "userName": "cognito-user-name",
        "request": {"challengeAnswer": code, "userAttributes": {"sub": sub}},
        "response": {},
    }


def test_verify_accepts_valid_code_and_consumes(monkeypatch):
    now = int(time.time())
    table = FakeTable({"code": "good", "sub": "sub-1", "consumed": False, "expiresAt": now + 90})
    monkeypatch.setattr(qr, "_table", lambda: table)
    ev = qr.verify_auth_challenge_response(_verify_event("good", "sub-1"), None)
    assert ev["response"]["answerCorrect"] is True
    assert table.updated is True


def test_verify_rejects_wrong_sub(monkeypatch):
    now = int(time.time())
    table = FakeTable({"code": "good", "sub": "sub-1", "consumed": False, "expiresAt": now + 90})
    monkeypatch.setattr(qr, "_table", lambda: table)
    ev = qr.verify_auth_challenge_response(_verify_event("good", "someone-else"), None)
    assert ev["response"]["answerCorrect"] is False


def test_verify_rejects_expired(monkeypatch):
    now = int(time.time())
    table = FakeTable({"code": "old", "sub": "sub-1", "consumed": False, "expiresAt": now - 1})
    monkeypatch.setattr(qr, "_table", lambda: table)
    ev = qr.verify_auth_challenge_response(_verify_event("old", "sub-1"), None)
    assert ev["response"]["answerCorrect"] is False


def test_verify_rejects_already_consumed(monkeypatch):
    now = int(time.time())
    table = FakeTable({"code": "used", "sub": "sub-1", "consumed": True, "expiresAt": now + 90})
    monkeypatch.setattr(qr, "_table", lambda: table)
    ev = qr.verify_auth_challenge_response(_verify_event("used", "sub-1"), None)
    assert ev["response"]["answerCorrect"] is False


def test_verify_rejects_unknown_code(monkeypatch):
    monkeypatch.setattr(qr, "_table", lambda: FakeTable(None))
    ev = qr.verify_auth_challenge_response(_verify_event("nope", "sub-1"), None)
    assert ev["response"]["answerCorrect"] is False
