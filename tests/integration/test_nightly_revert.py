"""Integration: scripts/nightly_revert.py (nightly re-match, spec §5 Revert) on real Postgres.

The core functions (`plan`, `apply`) take a connection and never commit, so every test runs
on the `db` fixture and is rolled back. Nightly links are written through the real J2 writer
(lambda_nightly_support_writer) so the revert is tested against the rows it will meet. No
decision_ledger row is written. All text is synthetic.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import datetime as _dt
import os

import pytest

import nightly_turn_pick as ntp
import tests.integration.test_nightly_support_writer as wt
from repositories import event_links

pytestmark = pytest.mark.integration

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SINCE = _dt.date(2026, 1, 1)


def _revert():
    """Loaded by path per test (a missing script is a per-test failure, not a collection
    error), so scripts/ never goes onto sys.path ahead of src for later test modules."""
    import importlib.util
    path = os.path.join(REPO, "scripts", "nightly_revert.py")
    spec = importlib.util.spec_from_file_location("nightly_revert", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_nightly(db, s, items):
    out = wt.writer.write_support(db, s.event([s.row(it) for it in items]))
    assert out["written"] == len(items)


def _link_ids(db, it):
    return [r[0] for r in wt._links(db, it)]


def _decision_records(db, *sites):
    return db.execute("SELECT count(*) FROM decision_records WHERE site_id = ANY(%s::uuid[])",
                      ([str(s.site["id"]) for s in sites],)).fetchone()[0]


def _claim(db, it):
    table = event_links.KIND_TABLE[it["kind"]]
    return db.execute(f"SELECT claim_type, claim_basis FROM {table} WHERE id = %s",
                      (it["row_id"],)).fetchone()


def test_revert_refuses_without_site():
    rv = _revert()
    with pytest.raises(SystemExit):
        rv.parse_args(["--since", "2026-11-01"])
    with pytest.raises(SystemExit):
        rv.parse_args(["--site", "not-a-uuid", "--since", "2026-11-01"])
    args = rv.parse_args(["--site", "00000000-0000-0000-0000-000000000001",
                          "--since", "2026-11-01"])
    assert args.apply is False and args.rule_version is None


def test_revert_dry_run_deletes_nothing(db):
    rv = _revert()
    s = wt.Seed(db)
    a, b = s.item("action_item"), s.item("finding")
    _write_nightly(db, s, [a, b])
    before = (_link_ids(db, a), _link_ids(db, b), _decision_records(db, s),
              _claim(db, a), _claim(db, b))
    got = rv.plan(db, site_id=s.site["id"], since=SINCE)
    assert got["links"] == 2 and got["sessions"] == 1
    assert got["by_rule_version"] == {ntp.RULE_VERSION: 2}
    after = (_link_ids(db, a), _link_ids(db, b), _decision_records(db, s),
             _claim(db, a), _claim(db, b))
    assert after == before and len(before[0]) == 1 and len(before[1]) == 1


def test_revert_plan_refuses_unknown_site(db):
    """The dry run refuses a site that does not exist, as apply does -- an all-zero plan for
    a mistyped id would read as "nothing to revert"."""
    import uuid
    rv = _revert()
    with pytest.raises(ValueError, match="no site"):
        rv.plan(db, site_id=uuid.uuid4(), since=SINCE)


def test_revert_deletes_only_nightly(db):
    rv = _revert()
    s = wt.Seed(db)
    other = wt.Seed(db, co=s.co)
    hit, old, lex_it = s.item("action_item"), s.item("decision"), s.item("finding")
    cont_it = s.item("question")
    elsewhere = other.item("action_item")
    _write_nightly(db, s, [hit, old])
    _write_nightly(db, other, [elsewhere])
    # Out of range: written before --since (NZ midnight of SINCE).
    db.execute("UPDATE event_links SET created_at = %s WHERE src_decision_id = %s",
               (_dt.datetime(2025, 12, 1, tzinfo=_dt.timezone.utc), old["row_id"]))
    lex = event_links.insert_supported_by(
        db, site_id=s.site["id"], src_kind="finding", src_row_id=lex_it["row_id"],
        source_s3_key=s.key, dst_turn=wt._locator(1.0), score=0.7, rule_version="v1",
        method="lexical_v1")
    # A nightly-method row that is not supported_by is not the revert's to delete.
    cont = event_links.insert_continues(
        db, site_id=s.site["id"], src_kind="question", src_row_id=cont_it["row_id"],
        src_stable_id=cont_it["stable_id"], dst_stable_id=old["stable_id"],
        source_s3_key=s.key, rule_version="v1", decision_record_id=None,
        method="nightly_echo_v1")
    # A method that only matches LIKE 'nightly_%' through the unescaped underscore.
    near = s.item("action_item")
    db.execute(
        "INSERT INTO event_links (site_id, link_type, src_kind, src_stable_id, "
        "src_action_item_id, source_s3_key, dst_kind, dst_turn, method, rule_version) "
        "VALUES (%s,'supported_by','action_item',%s,%s,%s,'turn',%s::jsonb,'nightlyXv1','v1')",
        (s.site["id"], near["stable_id"], near["row_id"], s.key,
         '{"source_stem": "stem-a", "start_sec": 1.0, "end_sec": 2.0}'))
    records = _decision_records(db, s, other)

    # --rule-version that matches nothing deletes nothing.
    none = rv.apply(db, site_id=s.site["id"], since=SINCE, rule_version="no_such_rule")
    assert none["deleted"] == 0 and len(_link_ids(db, hit)) == 1

    got = rv.apply(db, site_id=s.site["id"], since=SINCE, rule_version=ntp.RULE_VERSION)
    assert got["deleted"] == 1 and got["sessions"] == 1
    assert _link_ids(db, hit) == []
    assert len(_link_ids(db, old)) == 1                     # before --since
    assert _link_ids(db, lex_it) == [lex]                   # lexical, untouched
    assert len(_link_ids(db, elsewhere)) == 1               # another site
    assert len(_link_ids(db, near)) == 1                    # 'nightlyX...' is not nightly_
    assert db.execute("SELECT count(*) FROM event_links WHERE id = %s",
                      (cont,)).fetchone()[0] == 1           # continues, untouched
    assert _decision_records(db, s, other) == records       # never deletes records


def test_revert_recomputes_claim_type(db):
    rv = _revert()
    s = wt.Seed(db)
    it, lex_it = s.item("action_item"), s.item("finding")
    lex = event_links.insert_supported_by(
        db, site_id=s.site["id"], src_kind="finding", src_row_id=lex_it["row_id"],
        source_s3_key=s.key, dst_turn=wt._locator(1.0), score=0.7, rule_version="v1",
        method="lexical_v1")
    _write_nightly(db, s, [it])
    [nightly_link] = _link_ids(db, it)
    assert _claim(db, it)[1]["link_id"] == str(nightly_link)
    # Blank the lexical row's claim, so a recompute of the session is visible on it.
    db.execute("UPDATE findings SET claim_type = NULL, claim_basis = NULL WHERE id = %s",
               (lex_it["row_id"],))

    got = rv.apply(db, site_id=s.site["id"], since=SINCE)
    assert got["deleted"] == 1 and got["sessions"] == 1 and got["reset"] == 1

    ct, basis = _claim(db, it)
    assert ct is None
    assert "link_id" not in basis and basis["support_status"] is None
    lex_basis = _claim(db, lex_it)[1]
    assert lex_basis is not None and lex_basis["link_id"] == str(lex)


def test_revert_dry_run_connection_is_read_only(monkeypatch, migrated_db_url, capsys):
    """main() without --apply: the server itself refuses a write on the connection."""
    import psycopg
    rv = _revert()
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    conn = rv._connect(False)
    try:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("CREATE TEMP TABLE nightly_revert_probe (x int)")
    finally:
        conn.rollback()
        conn.close()
    # an unknown site is refused (plan reads sites, as apply does) ...
    with pytest.raises(ValueError, match="no site"):
        rv.main(["--site", "00000000-0000-0000-0000-000000000001", "--since", "2026-11-01"])
    # ... a real one with no nightly links plans zero. The site is committed (main opens
    # its own connection) and deleted afterwards.
    import uuid
    from db.connection import get_connection
    from repositories import companies, sites
    seed = get_connection(migrated_db_url, autocommit=True)
    co = companies.create_company(seed, f"NightlyR-Commit-{uuid.uuid4().hex[:8]}")
    site = sites.create_site(seed, co["id"], f"NightlyR-Site-{uuid.uuid4().hex[:8]}")
    try:
        assert rv.main(["--site", str(site["id"]), "--since", "2026-11-01"]) == 0
    finally:
        seed.execute("DELETE FROM sites WHERE id = %s", (site["id"],))
        seed.execute("DELETE FROM companies WHERE id = %s", (co["id"],))
        seed.close()
    out = capsys.readouterr().out
    assert '"links": 0' in out and "nothing was written" in out
