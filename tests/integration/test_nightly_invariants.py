"""Integration: scripts/nightly_invariants.sql and the nightly mutation gate (spec 2026-11-10
nightly re-match §7 Engineering, plan Task 7).

Invariants: the file is loaded and split, and every query runs against a clean fixture (zero
rows) and against a planted violation of its own (at least one row) -- each query is shown to
be able to fail.

Mutations: each planted fault is a monkeypatch; the named catcher test (imported as a MODULE
attribute, so pytest does not collect it a second time here) is called directly with this
test's fixtures and must FAIL with an AssertionError under the fault. The patch is undone
(the `monkeypatch.context()` block exits) BEFORE `invariants()` runs, and the catcher then
passes again on the real code. If a fault is not caught, strengthen the catcher -- never
delete or weaken the mutation.

Scope: every invariant query returns a site_id; `invariants()` keeps the rows of the sites
created in this test's transaction (sites.created_at = now(), the transaction's start), so
rows other suites left in the shared local database never leak in. No decision_ledger row is
written. All text is synthetic. Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import os
import re
import sys
import uuid

import pytest

import nightly_turn_pick as ntp
import tests.integration.test_nightly_repo as repo_t
import tests.integration.test_nightly_support_writer as wt
import tests.unit.test_nightly_turn_pick as pick_t
from repositories import event_links, nightly

pytestmark = pytest.mark.integration

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SQL_PATH = os.path.join(REPO, "scripts", "nightly_invariants.sql")
NAMES = ("nightly_link_without_nightly_support_record", "two_nightly_links_for_one_source_row",
         "nightly_link_not_supported_by", "two_nightly_reemits_for_one_line_n_l",
         "nightly_link_on_row_not_live_at_write")
writer = wt.writer


def load_invariants(path=SQL_PATH):
    """[(name, sql)] in file order. Each statement starts at its `-- name:` line (the
    preamble before the first one is dropped); comment lines are removed and the statement
    must end in exactly one ';'."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    parts = re.split(r"^--\s*name:\s*(\S+)\s*$", text, flags=re.M)[1:]
    out = []
    for name, chunk in zip(parts[0::2], parts[1::2]):
        body = "\n".join(ln for ln in chunk.splitlines()
                         if not ln.lstrip().startswith("--")).strip()
        assert body.endswith(";") and body.count(";") == 1, f"{name}: one statement, one ';'"
        out.append((name, body[:-1].strip()))
    return out


def _tx_sites(db):
    return [r[0] for r in db.execute(
        "SELECT id::text FROM sites WHERE created_at = now()").fetchall()]


def invariants(db, site_ids=None):
    """{name: [offending rows]} for the invariants that fail on `site_ids` (default: the
    sites created in this transaction); {} when all hold."""
    sites = _tx_sites(db) if site_ids is None else [str(s) for s in site_ids]
    bad = {}
    for name, sql in load_invariants():
        rows = db.execute(f"SELECT * FROM ({sql.replace('%', '%%')}) q "
                          f"WHERE q.site_id = ANY(%s::uuid[])", (sites,)).fetchall()
        if rows:
            bad[name] = rows
    return bad


def _catcher_fails(fn):
    """The catcher must fail on an assertion (not on an SQL error or a missing fixture)."""
    with pytest.raises(AssertionError):
        fn()


def _catcher_passes(db, fn):
    with db.transaction(force_rollback=True):
        fn()


# ------------------------------------------------------------------------------- invariants

def test_invariant_file_has_the_five_queries():
    assert tuple(n for n, _ in load_invariants()) == NAMES


def _clean_world(db):
    """A site with every kind of row the invariants look at, all of them legal."""
    s = wt.Seed(db)
    acc1, acc2, rej, lex_it = (s.item("action_item"), s.item("finding"), s.item("decision"),
                               s.item("question"))
    out = writer.write_support(db, s.event([
        s.row(acc1), s.row(acc2, start=20.0), s.row(rej, outcome="rejected", guard="twin")]))
    assert out["written"] == 2
    event_links.insert_supported_by(
        db, site_id=s.site["id"], src_kind="question", src_row_id=lex_it["row_id"],
        source_s3_key=s.key, dst_turn=wt._locator(1.0), score=0.7, rule_version="v1",
        method="lexical_v1")
    kw = dict(site_id=s.site["id"], line="xday", n_key="extractions/f/2026-11-09/n.json",
              l_key="extractions/f/2026-11-09/l.json", artifact_key="a/x.json", night=wt.DAY)
    assert nightly.record_reemit(db, **kw) is True
    assert nightly.record_reemit(db, **{**kw, "line": "reconcile"}) is True
    assert nightly.record_reemit(db, **{**kw, "l_key": None}) is True
    assert nightly.record_reemit(
        db, **{**kw, "l_key": None, "night": wt.DAY.replace(day=10)}) is True
    return s, acc1


def test_invariants_clean_on_fixture(db):
    s, _ = _clean_world(db)
    assert str(s.site["id"]) in _tx_sites(db)
    assert invariants(db) == {}


def _nightly_link(db, s, it, *, record_id, start=12.0):
    return event_links.insert_supported_by(
        db, site_id=s.site["id"], src_kind=it["kind"], src_row_id=it["row_id"],
        source_s3_key=s.key, dst_turn=wt._locator(start), score=None,
        rule_version=ntp.RULE_VERSION, method=ntp.METHOD, decision_record_id=record_id)


def _plant_without_record(db, s, it):
    _nightly_link(db, s, it, record_id=str(uuid.uuid4()))   # no FK: any uuid is accepted


def _plant_two_links(db, s, it):
    db.execute("DROP INDEX uq_event_links_nightly_ai")       # rolled back with the fixture
    record = writer.decision_records.insert(
        db, company_id=s.co["id"], site_id=s.site["id"], kind="nightly_support",
        subject_type="action_item", subject_stable_id=it["stable_id"], object_ref=None,
        provider="anthropic", model=wt.MODEL, question_set=ntp.PROMPT_HASH,
        input_key="k", output={"turn": None, "guard": None}, auto_outcome="accepted")
    _nightly_link(db, s, it, record_id=record["id"])
    _nightly_link(db, s, it, record_id=record["id"], start=30.0)


def _plant_not_supported_by(db, s, it):
    other = s.item("action_item")
    event_links.insert_continues(
        db, site_id=s.site["id"], src_kind="action_item", src_row_id=it["row_id"],
        src_stable_id=it["stable_id"], dst_stable_id=other["stable_id"], source_s3_key=s.key,
        rule_version="v1", decision_record_id=None, method="nightly_echo_v1")


def _drop_reemit_uniques(db):
    for (name,) in db.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'nightly_reemits'::regclass "
            "AND contype = 'u'").fetchall():
        db.execute(f'ALTER TABLE nightly_reemits DROP CONSTRAINT "{name}"')
    db.execute("DROP INDEX uq_nightly_reemits_dropped")


def _plant_two_reemits(db, s, it):
    _drop_reemit_uniques(db)                                 # rolled back with the fixture
    for night in (wt.DAY, wt.DAY.replace(day=10)):
        db.execute(
            "INSERT INTO nightly_reemits (site_id, line, n_extraction_key, l_extraction_key, "
            "artifact_key, night) VALUES (%s,'xday','n.json','l.json','a.json',%s)",
            (s.site["id"], night))


def _plant_superseded_before_write(db, s, it):
    out = writer.write_support(db, s.event([s.row(it)]))
    assert out["written"] == 1
    db.execute("UPDATE topics SET superseded_at = now() - interval '1 hour' WHERE id = %s",
               (s.topic["id"],))


PLANTS = {
    "nightly_link_without_nightly_support_record": _plant_without_record,
    "two_nightly_links_for_one_source_row": _plant_two_links,
    "nightly_link_not_supported_by": _plant_not_supported_by,
    "two_nightly_reemits_for_one_line_n_l": _plant_two_reemits,
    "nightly_link_on_row_not_live_at_write": _plant_superseded_before_write,
}


@pytest.mark.parametrize("name", NAMES)
def test_invariant_catches_planted_row(db, name):
    s, _ = _clean_world(db)
    assert invariants(db) == {}
    PLANTS[name](db, s, s.item("action_item"))
    bad = invariants(db)
    assert name in bad and len(bad[name]) >= 1


def test_dropped_reemits_on_two_nights_are_not_a_violation(db):
    """The dropped-request arm (no L) is once per (line, N, night) by design."""
    s = wt.Seed(db)
    kw = dict(site_id=s.site["id"], line="xday", n_key="n.json", l_key=None,
              artifact_key="a.json", night=wt.DAY)
    assert nightly.record_reemit(db, **kw) is True
    assert nightly.record_reemit(db, **{**kw, "night": wt.DAY.replace(day=10)}) is True
    assert invariants(db) == {}


# -------------------------------------------------------------------------------- mutations

def test_mutation_drop_site_filter_caught(db, monkeypatch):
    """sessions_needing_support loses its sites.company_id (current owner) filter."""
    catcher = repo_t.test_sessions_needing_support_scoped_by_current_company
    real = nightly._needing_sql
    needle = "AND s.company_id = %(company_id)s "

    def mutant(kind):
        sql = real(kind)
        if needle not in sql:   # not an AssertionError: that would pass as "caught"
            raise RuntimeError("the site filter moved: update this mutation, do not drop it")
        return sql.replace(needle, "")

    with monkeypatch.context() as mp:
        mp.setattr(nightly, "_needing_sql", mutant)
        _catcher_fails(lambda: catcher(db))
    assert nightly._needing_sql is real
    assert invariants(db) == {}
    _catcher_passes(db, lambda: catcher(db))


def test_mutation_skip_window_guard_caught(monkeypatch):
    """apply_guards' window check sees every turn as inside the topic window. Only the
    guard's call is mutated: build_request still numbers the real window turns."""
    real = ntp._window_turns
    guard_calls = []

    def mutant(time_range, session_date, turns):
        if sys._getframe(1).f_code.co_name == "apply_guards":
            guard_calls.append(1)
            return range(len(turns))
        return real(time_range, session_date, turns)

    with monkeypatch.context() as mp:
        mp.setattr(ntp, "_window_turns", mutant)
        _catcher_fails(pick_t.test_turn_moved_out_of_window_is_window_drop)
        # The shape guard runs before the window guard, so the other-topic case stays a
        # shape drop under this fault (it is Review Focus 2's catcher, not this one's).
        pick_t.test_turn_from_other_topic_is_shape_drop()
    assert guard_calls, "the window guard no longer calls _window_turns: update the mutation"
    assert ntp._window_turns is real
    pick_t.test_turn_moved_out_of_window_is_window_drop()


def test_mutation_skip_twin_guard_caught(monkeypatch):
    """No item has a twin and no place value ever disagrees: every twin arm is off."""
    real_twins, real_flags = ntp._twins, ntp.twin_flags
    with monkeypatch.context() as mp:
        mp.setattr(ntp, "_twins", lambda items: {it["alias"]: set() for it in items})
        mp.setattr(ntp, "twin_flags", lambda item_text, turn_text: [])
        _catcher_fails(pick_t.test_twin_same_turn_both_rejected)
    assert ntp._twins is real_twins and ntp.twin_flags is real_flags
    pick_t.test_twin_same_turn_both_rejected()


def _raise_on_conflict(mp):
    real = event_links.insert_supported_by

    def insert(*a, **k):
        k["on_conflict"] = "raise"
        return real(*a, **k)
    mp.setattr(writer.event_links, "insert_supported_by", insert)


def test_writer_raise_alone_is_not_a_fault(db, monkeypatch):
    """Why the writer mutation also blinds the already-linked re-read: on_conflict="raise"
    alone never reaches the insert for a lexical-linked row (the writer rejects it as
    already_linked first), so the catcher stays green -- that variant is no fault at all."""
    with monkeypatch.context() as mp:
        _raise_on_conflict(mp)
        _catcher_passes(db, lambda: wt.test_writer_never_touches_lexical_link(db))


def test_mutation_writer_overwrites_lexical_link_caught(db, monkeypatch):
    """The writer ignores the row's existing (lexical) link and inserts with
    on_conflict="raise": the 0102 indexes cover nightly methods only, so a second link lands
    on the lexical-linked row."""
    catcher = wt.test_writer_never_touches_lexical_link
    real_reread = writer._reread

    def reread(conn, kind, row_id):
        found = real_reread(conn, kind, row_id)
        return None if found is None else (found[0], found[1], False)

    with monkeypatch.context() as mp:
        mp.setattr(writer, "_reread", reread)
        _raise_on_conflict(mp)
        _catcher_fails(lambda: catcher(db))
    assert writer._reread is real_reread
    assert writer.event_links.insert_supported_by is event_links.insert_supported_by
    # The extra link is a legal nightly link by the five invariants (record, one per row,
    # supported_by, live row); the catcher test is what sees this fault.
    assert invariants(db) == {}
    _catcher_passes(db, lambda: catcher(db))


def test_mutation_record_reemit_ignores_conflict_caught(db, monkeypatch):
    """record_reemit reports every call as new. (a) With the table's uniques in place the
    catcher sees it and the data stays legal. (b) With the conflict truly ignored -- the
    uniques gone, a plain INSERT -- the duplicate (line, N, L) lands and invariant
    two_nightly_reemits_for_one_line_n_l reports it once the patch is undone."""
    catcher = repo_t.test_record_reemit_unique_per_pair
    real = nightly.record_reemit

    def always_new(conn, **kw):
        real(conn, **kw)
        return True

    with monkeypatch.context() as mp:
        mp.setattr(nightly, "record_reemit", always_new)
        _catcher_fails(lambda: catcher(db))
    assert nightly.record_reemit is real
    assert invariants(db) == {}

    def plain_insert(conn, *, site_id, line, n_key, l_key, artifact_key, night):
        conn.execute(
            "INSERT INTO nightly_reemits (site_id, line, n_extraction_key, l_extraction_key, "
            "artifact_key, night) VALUES (%s,%s,%s,%s,%s,%s)",
            (site_id, line, n_key, l_key, artifact_key, night))
        return True

    _drop_reemit_uniques(db)                                  # rolled back with the fixture
    with monkeypatch.context() as mp:
        mp.setattr(nightly, "record_reemit", plain_insert)
        _catcher_fails(lambda: catcher(db))
    assert nightly.record_reemit is real
    bad = invariants(db)
    assert set(bad) == {"two_nightly_reemits_for_one_line_n_l"}
