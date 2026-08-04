"""What gets pushed, and — more importantly — what does not.

The table is always current, so a push is not "here is the state"; it is
"something needs a decision". Pushing anything else trains people to ignore the
channel, and then the alerts that matter are ignored too.
"""

import src.device_notify as dn

URL = "https://app.notion.com/p/943da8c294734365b6c7294c2055c45d"


def result(device, alerts):
    return {"page_id": "p", "device": device, "status": "使用中",
            "alerts": alerts, "updates": {}}


def test_silence_when_nothing_needs_attention():
    assert dn.format_message([result("FS-01", [])], URL) is None


def test_silence_on_an_empty_ledger():
    assert dn.format_message([], URL) is None


def test_a_row_flag_alone_is_not_worth_a_message():
    """site_mismatch is shown in the table, never pushed."""
    assert dn.format_message([result("FS-05", ["site_mismatch_flag"])], URL) is None


def test_names_the_devices_and_links_the_table():
    msg = dn.format_message(
        [result("FS-02", ["due_back"]), result("FS-07", ["never_activated"])], URL)
    assert "FS-02" in msg
    assert "FS-07" in msg
    assert URL in msg


def test_groups_by_alert_rather_than_one_line_per_device():
    msg = dn.format_message(
        [result("FS-01", ["outdated_version"]), result("FS-02", ["outdated_version"])], URL)
    assert msg.count("FS-0") == 2
    assert len([ln for ln in msg.splitlines() if "版本落后" in ln]) == 1


def test_the_most_actionable_alert_comes_first():
    msg = dn.format_message(
        [result("FS-01", ["outdated_version"]), result("FS-02", ["due_back"])], URL)
    lines = [ln for ln in msg.splitlines() if ln.strip()]
    assert "该回收" in lines[0]


def test_a_device_with_several_alerts_appears_under_each():
    msg = dn.format_message([result("FS-03", ["due_back", "quiet"])], URL)
    assert msg.count("FS-03") == 2


def test_devices_are_listed_in_a_stable_order():
    a = dn.format_message([result("FS-09", ["due_back"]), result("FS-02", ["due_back"])], URL)
    b = dn.format_message([result("FS-02", ["due_back"]), result("FS-09", ["due_back"])], URL)
    assert a == b


# --- push ---

def test_push_is_a_no_op_without_a_destination():
    dn.push("anything", teams_webhook="", email_to=[], ses_sender="")


def test_push_is_a_no_op_with_nothing_to_say():
    calls = []
    dn.push(None, teams_webhook="https://example", email_to=["a@b.nz"], ses_sender="x",
            http=_FakeHttp(calls))
    assert calls == []


def test_push_posts_the_text_to_teams():
    calls = []
    dn.push("2 devices need attention", teams_webhook="https://example",
            email_to=[], ses_sender="", http=_FakeHttp(calls))
    assert len(calls) == 1
    assert b"2 devices need attention" in calls[0]["body"]


def test_a_failing_teams_post_does_not_stop_the_email():
    """One channel being down must not silence the other."""
    class Boom:
        def request(self, *a, **k):
            raise RuntimeError("teams down")

    sent = []
    dn.push("text", teams_webhook="https://example", email_to=["a@b.nz"],
            ses_sender="s@x.nz", http=Boom(), mailer=lambda **kw: sent.append(kw))
    assert len(sent) == 1


class _FakeHttp:
    def __init__(self, calls):
        self.calls = calls

    def request(self, method, url, headers=None, body=None):
        self.calls.append({"method": method, "url": url, "body": body})
        return type("R", (), {"status": 200})()
