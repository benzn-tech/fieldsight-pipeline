"""Event runs: one handler attempt over one or more site events (migration 0100).

run_events joins a run to every event it handled and carries the event's company_id/site_id
(tenancy columns, NOT NULL). The attempt count the fault and stop rules use is
site_events.attempts, not event_runs.attempt."""
from psycopg.rows import dict_row

_COLS = ("run_id, company_id, site_id, attempt, handler, claim_id, lease_until, started_at, "
         "ended_at, outcome, result_hash, error, tokens_in, tokens_out, cost_usd, model_calls, "
         "models")


def start(conn, company_id, site_id, event_ids, attempt, handler):
    """Insert the run (started_at = now()) and link each event of THIS tenant to it.
    ValueError when fewer distinct events were linked than given (an unknown event or one of
    another tenant); the run row is then already inserted, so the caller rolls back."""
    run = conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO event_runs (company_id, site_id, attempt, handler, started_at) "
        f"VALUES (%s,%s,%s,%s,now()) RETURNING {_COLS}",
        (company_id, site_id, attempt, handler)).fetchone()
    wanted = list(dict.fromkeys(str(e) for e in event_ids))
    linked = sum(add_event(conn, run["run_id"], e, company_id, site_id) for e in wanted)
    if linked < len(wanted):
        raise ValueError(f"event_runs.start: linked {linked} of {len(wanted)} events "
                         "(unknown or another tenant's)")
    return run


def add_event(conn, run_id, event_id, company_id, site_id):
    """Link an event to a run. Returns rows inserted: 1, or 0 when the link exists already or
    the run or event is not this tenant's (a link is never made across tenants)."""
    return conn.execute(
        "INSERT INTO run_events (run_id, event_id, company_id, site_id) "
        "SELECT r.run_id, e.event_id, e.company_id, e.site_id "
        "FROM event_runs r JOIN site_events e "
        "  ON e.company_id = r.company_id AND e.site_id = r.site_id "
        "WHERE r.run_id = %s AND e.event_id = %s AND r.company_id = %s AND r.site_id = %s "
        "ON CONFLICT (run_id, event_id) DO NOTHING",
        (run_id, event_id, company_id, site_id)).rowcount


def finish(conn, run_id, company_id, site_id, outcome, error=None, result_hash=None):
    """Close the run (ended_at = now()). Returns the row, or None for another tenant's run."""
    return conn.cursor(row_factory=dict_row).execute(
        "UPDATE event_runs SET outcome = %s, error = %s, result_hash = %s, ended_at = now() "
        f"WHERE run_id = %s AND company_id = %s AND site_id = %s RETURNING {_COLS}",
        (outcome, error, result_hash, run_id, company_id, site_id)).fetchone()


def add_usage(conn, run_id, company_id, site_id, *, tokens_in, tokens_out, model, cost_usd):
    """Add one model call's usage to THIS tenant's run: tokens summed (NULL counts as 0),
    model_calls + 1, model appended to the models array (in call order, repeats kept).
    Returns the row, or None (and changes nothing) when no run matches run_id AND
    company_id AND site_id -- an unknown run or another tenant's.

    run_id reaches the writer from the S3 request through the non-VPC worker, so it is
    request input: the tenant columns in the WHERE are what keep it on its own run.

    cost_usd None (cost unknown) leaves cost_usd as it is: a NULL is never turned into 0.
    Once any call of the run had an unknown cost, the stored cost_usd is therefore only a
    LOWER BOUND of the run's cost (model_calls / models still count every call)."""
    return conn.cursor(row_factory=dict_row).execute(
        "UPDATE event_runs SET "
        "  tokens_in = COALESCE(tokens_in, 0) + COALESCE(%s::int, 0), "
        "  tokens_out = COALESCE(tokens_out, 0) + COALESCE(%s::int, 0), "
        "  cost_usd = CASE WHEN %s::numeric IS NULL THEN cost_usd "
        "                  ELSE COALESCE(cost_usd, 0) + %s::numeric END, "
        "  model_calls = model_calls + 1, "
        "  models = COALESCE(models, '[]'::jsonb) || jsonb_build_array(%s::text) "
        f"WHERE run_id = %s AND company_id = %s AND site_id = %s RETURNING {_COLS}",
        (tokens_in, tokens_out, cost_usd, cost_usd, model, run_id, company_id,
         site_id)).fetchone()
