import pytest

ce = pytest.importorskip("repositories.content_edits",
                         reason="requires psycopg (installed in CI)")


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, rows):
        self.cur = FakeCursor(rows)

    def cursor(self, *a, **k):
        return self.cur


def test_append_binds_before_after_actor():
    conn = FakeConn([{"id": "e-1"}])
    ce.append_content_edit(conn, "co-1", "topics", "t-1", "title",
                           "Mackon", "McCahon", "u-1", "site_manager")
    assert conn.cur.params == ("co-1", "topics", "t-1", "title",
                               "Mackon", "McCahon", "u-1", "site_manager")


def test_list_is_company_guarded_newest_first():
    conn = FakeConn([{"id": "e-2"}, {"id": "e-1"}])
    rows = ce.list_content_edits(conn, "co-1", "topics", "t-1")
    assert len(rows) == 2
    assert "company_id=%s" in conn.cur.sql or "company_id = %s" in conn.cur.sql
    # PR #117 aliased content_edits as `ce` to JOIN users for actor_name, so the
    # ORDER BY became qualified. Assert on the qualified column, not the bare one.
    assert "order by ce.created_at desc" in conn.cur.sql.lower()


# ----------------------------------------------------------
# count_action_closures_by_day (fix/week-kpi) — the weekly KPI aggregate.
# Counts ONLY status->done transitions, in ONE grouped query, scoped by the
# caller's site reach and (unless cross-company) the tenant.
# ----------------------------------------------------------
import datetime as _dt

_SITES = ["a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1"]


def _agg_conn(rows=None):
    return FakeConn(rows if rows is not None else
                    [{"close_date": _dt.date(2026, 7, 20), "closed": 2},
                     {"close_date": _dt.date(2026, 7, 23), "closed": 5}])


def test_closures_returns_iso_keyed_day_buckets():
    conn = _agg_conn()
    got = ce.count_action_closures_by_day(conn, _SITES, "2026-07-20", "2026-07-25",
                                          company_id="co-1")
    assert got == {"2026-07-20": 2, "2026-07-23": 5}


def test_closures_counts_only_status_transitions_to_done():
    conn = _agg_conn()
    ce.count_action_closures_by_day(conn, _SITES, "2026-07-20", "2026-07-25",
                                    company_id="co-1")
    sql = conn.cur.sql.lower()
    assert "ce.table_name = 'action_items'" in sql
    assert "ce.field = 'status'" in sql          # priority/deadline/responsible excluded
    assert "ce.after_text = %s" in sql and "ce.before_text is distinct from %s" in sql
    assert conn.cur.params[0:3] == ("Pacific/Auckland", "done", "done")


def test_closures_scopes_by_site_reach_via_the_action_item_join():
    """content_edits has no site_id — the reach gate rides the JOIN."""
    conn = _agg_conn()
    ce.count_action_closures_by_day(conn, _SITES, "2026-07-20", "2026-07-25",
                                    company_id="co-1")
    sql = conn.cur.sql.lower()
    assert "join action_items a on a.id = ce.row_id" in sql
    assert "a.site_id = any(%s::uuid[])" in sql
    assert _SITES in conn.cur.params


def test_closures_pins_the_tenant_when_a_company_is_given():
    conn = _agg_conn()
    ce.count_action_closures_by_day(conn, _SITES, "2026-07-20", "2026-07-25",
                                    company_id="co-1")
    assert "ce.company_id = %s" in conn.cur.sql
    assert conn.cur.params[-1] == "co-1"


def test_closures_omits_the_tenant_pin_only_for_a_cross_company_caller():
    conn = _agg_conn()
    ce.count_action_closures_by_day(conn, _SITES, "2026-07-20", "2026-07-25",
                                    company_id=None)
    assert "ce.company_id" not in conn.cur.sql
    assert "a.site_id = ANY(%s::uuid[])" in conn.cur.sql   # reach still applied


def test_closures_buckets_on_the_nz_local_day_not_utc():
    conn = _agg_conn()
    ce.count_action_closures_by_day(conn, _SITES, "2026-07-20", "2026-07-25",
                                    company_id="co-1")
    sql = conn.cur.sql
    assert "(ce.created_at AT TIME ZONE %s)::date AS close_date" in sql
    # window bounds are local midnights, end-exclusive on the day AFTER `to`
    assert "ce.created_at >= (%s::date::timestamp AT TIME ZONE %s)" in sql
    assert "ce.created_at < ((%s::date + 1)::timestamp AT TIME ZONE %s)" in sql
    assert ce.CLOSURE_TZ == "Pacific/Auckland"


def test_closures_empty_reach_returns_no_rows_without_a_query():
    conn = _agg_conn()
    assert ce.count_action_closures_by_day(conn, [], "2026-07-20", "2026-07-25",
                                           company_id="co-1") == {}
    assert conn.cur.sql is None      # empty list must never mean "no filter"


def test_closures_no_matching_rows_is_an_empty_dict_not_an_error():
    conn = _agg_conn(rows=[])
    assert ce.count_action_closures_by_day(conn, _SITES, "2026-07-20", "2026-07-25",
                                           company_id="co-1") == {}
