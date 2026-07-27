"""Tests for src/email_sender.py — the pluggable outbound-email channel.
Pure module (stdlib only on the default path); boto3 is lazy-imported and here
the SES client is injected, so no AWS is touched."""
import email_sender as es


class FakeSes:
    def __init__(self):
        self.calls = []

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        return {"MessageId": "ses-msg-123"}


def test_stub_sends_nothing_but_returns_sentinel():
    out = es.StubEmailSender().send("a@x.nz", "Hi", "body text")
    assert out == "stub-not-sent"


def test_ses_builds_correct_envelope():
    fake = FakeSes()
    sender = es.SesEmailSender(client=fake, sender="noreply@x.nz")
    mid = sender.send("recorder@x.nz", "Your meeting summary", "text body",
                      body_html="<p>html body</p>")
    assert mid == "ses-msg-123"
    call = fake.calls[0]
    assert call["Source"] == "noreply@x.nz"
    assert call["Destination"] == {"ToAddresses": ["recorder@x.nz"]}
    assert call["Message"]["Subject"]["Data"] == "Your meeting summary"
    assert call["Message"]["Body"]["Text"]["Data"] == "text body"
    assert call["Message"]["Body"]["Html"]["Data"] == "<p>html body</p>"


def test_ses_text_only_omits_html():
    fake = FakeSes()
    es.SesEmailSender(client=fake, sender="noreply@x.nz").send("r@x.nz", "S", "only text")
    assert "Html" not in fake.calls[0]["Message"]["Body"]


def test_ses_without_sender_raises():
    import pytest
    with pytest.raises(RuntimeError):
        es.SesEmailSender(client=FakeSes(), sender="").send("r@x.nz", "S", "b")


def test_ses_accepts_list_of_recipients():
    fake = FakeSes()
    es.SesEmailSender(client=fake, sender="n@x.nz").send(["a@x.nz", "b@x.nz"], "S", "b")
    assert fake.calls[0]["Destination"] == {"ToAddresses": ["a@x.nz", "b@x.nz"]}


def test_factory_defaults_to_stub(monkeypatch):
    monkeypatch.setattr(es, "EMAIL_SENDER", "stub")
    assert isinstance(es.get_sender(), es.StubEmailSender)


def test_factory_unknown_value_falls_back_to_stub(monkeypatch):
    monkeypatch.setattr(es, "EMAIL_SENDER", "carrierpigeon")
    assert isinstance(es.get_sender(), es.StubEmailSender)


def test_factory_ses_when_configured(monkeypatch):
    monkeypatch.setattr(es, "EMAIL_SENDER", "ses")
    assert isinstance(es.get_sender(), es.SesEmailSender)
