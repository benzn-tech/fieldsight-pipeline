"""A day report must stop being signable once any of its recordings is removed.

Both presign doors carried a rule shaped as *"is a deleted session's base inside this key?"*
That question is unanswerable for a day report: `reports/{date}/{folder}/daily_report.json`
is a synthesis of the whole day and its key names no session at all. So the check could
never fire for the one document that actually holds the words.

Measured on prod, 2026-08-31, on a real deletion rather than a hypothetical:

    redactions/Ben_UCPK2/2026-08-14/deleted_sessions.json
        -> {"sessions": ["sid8e77d3a13cb84fff8545390c5a101153"]}
    reports/2026-08-14/Ben_UCPK2/daily_report.json
        -> still names that session, LastModified unchanged since 2026-08-14
    GET /api/org/media/presigned-url?key=reports/2026-08-14/Ben_UCPK2/daily_report.json
        -> 200, URL issued

Seventeen days after the delete. The object's LastModified never moved, so the nightly
rebuild does not revisit past days: the exposure is permanent, not the overnight window it
resembles.

The position taken -- ANY deletion that day hides the whole document -- is not a new one.
`lambda_ask_agent` already refuses to serve a stored report for a day with deletions, and the
legacy gateway already refuses the cross-folder `summary_report.json`. This is the third copy
of one rule, and the two that existed disagreed about the document in the middle.

`reports/` also reverses the segment order: `reports/{date}/{folder}/` where every other
shape is `{prefix}/{folder}/{date}/`. Reading it the usual way looks up folder="2026-08-14"
and misses every time, silently, which is why both orders are asserted here.
"""
import pytest

oa = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

FOLDER = "Ben_UCPK2"
DATE = "2026-08-14"
DAY_REPORT = f"reports/{DATE}/{FOLDER}/daily_report.json"
DAY_DOCX = f"reports/{DATE}/{FOLDER}/daily_report.docx"
AUDIO = f"audio_segments/{FOLDER}/{DATE}/sid{'a' * 32}_off0.0_to30.0.wav"
DELETED_PREFIX = f"extractions/{FOLDER}/{DATE}/sid{'a' * 32}"


def _wire(monkeypatch, deleted_for):
    """`deleted_for` maps (folder, date) -> the tombstoned prefixes for that day."""
    seen = []

    def _lookup(conn, folder, date):
        seen.append((folder, date))
        return set(deleted_for.get((folder, date), ()))

    monkeypatch.setattr(oa, "_deleted_sessions_for_day", _lookup)
    return seen


@pytest.mark.parametrize("key", [DAY_REPORT, DAY_DOCX],
                         ids=["daily_report.json", "daily_report.docx"])
def test_a_day_report_is_refused_once_that_day_has_a_deletion(monkeypatch, key):
    _wire(monkeypatch, {(FOLDER, DATE): {DELETED_PREFIX}})
    assert oa._presign_target_is_deleted(object(), key) is True


def test_the_reports_shape_is_read_date_then_folder(monkeypatch):
    """The order is reversed for `reports/`. Reading it the usual way asks about a folder
    called "2026-08-14", finds nothing, and signs the document."""
    seen = _wire(monkeypatch, {(FOLDER, DATE): {DELETED_PREFIX}})
    oa._presign_target_is_deleted(object(), DAY_REPORT)
    assert seen == [(FOLDER, DATE)], f"looked up {seen}, so the segments were read backwards"


def test_a_day_with_no_deletion_still_signs(monkeypatch):
    """The guard must not become a blanket refusal — every day report on every clean day
    would then 404, and a leak test passes either way."""
    _wire(monkeypatch, {})
    assert oa._presign_target_is_deleted(object(), DAY_REPORT) is False


def test_another_folders_deletion_does_not_hide_this_folders_report(monkeypatch):
    """Per-folder, not per-day-across-the-company. The cross-folder aggregate
    (`summary_report.json`) is the one that has to fall back to any-folder, and it takes a
    different path."""
    _wire(monkeypatch, {("Someone_Else", DATE): {DELETED_PREFIX}})
    assert oa._presign_target_is_deleted(object(), DAY_REPORT) is False


def test_media_keys_still_go_through_the_session_id_match(monkeypatch):
    """The reports branch must not swallow the shapes that DO carry a session id — those
    are filtered per-object, not per-day, and hiding a whole day of audio because one
    recording went would be a different bug wearing this fix's clothes."""
    called = {"n": 0}

    def _boom(*a, **kw):
        called["n"] += 1
        return set()

    monkeypatch.setattr(oa, "_deleted_sessions_for_day", _boom)
    monkeypatch.setattr(oa.redactions, "deleted_source_prefixes",
                        lambda conn: [DELETED_PREFIX])
    assert oa._presign_target_is_deleted(object(), AUDIO) is True
    assert called["n"] == 0, "an audio key took the whole-day branch"
