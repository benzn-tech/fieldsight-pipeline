"""Pilot step 0 (plan T11): read-only probes that must pass before any question runs.
Each aborts with SystemExit on failure. No model or vendor call happens here: the
parity smoke binds a fixed vector, not an embedding."""
from psycopg.rows import dict_row

IAM_ACTIONS = ("rds-data:BeginTransaction", "rds-data:ExecuteStatement",
               "rds-data:RollbackTransaction", "lambda:GetFunctionConfiguration")
# Review B2: columns/tables the reached SQL needs. `memberships._exec`'s deploy
# tolerance cannot fire under the adapter (it matches psycopg exception classes).
REQUIRED_COLUMNS = (("topics", "superseded_at"), ("memberships", "external"),
                    ("redactions", "target_key"), ("redactions", "reverted_at"))
REQUIRED_TABLES = ("project_documents", "project_doc_chunks")
PARITY_VECTOR = [1.0] + [0.0] * 1023       # %.7f-exact; binding parity, not relevance
PARITY_K = 16


def iam_check(iam, sts):
    """Simulate the eval's actions for the calling principal (review B3.5). A denied
    action stops the run; a simulator the profile may not call is reported, since the
    read-only probe that follows exercises the same rds-data actions for real."""
    arn = sts.get_caller_identity()["Arn"]
    principal = arn
    if ":assumed-role/" in arn:
        account = arn.split(":")[4]
        role = arn.split(":assumed-role/")[1].split("/")[0]
        try:
            principal = iam.get_role(RoleName=role)["Role"]["Arn"]
        except Exception:                                   # noqa: BLE001
            principal = "arn:aws:iam::%s:role/%s" % (account, role)
    try:
        res = iam.simulate_principal_policy(PolicySourceArn=principal,
                                            ActionNames=list(IAM_ACTIONS))
    except Exception as exc:                                # noqa: BLE001
        return {"principal": principal, "verified": False,
                "note": "simulation unavailable: %s" % str(exc)[:160]}
    decisions = {r["EvalActionName"]: r["EvalDecision"] for r in res["EvaluationResults"]}
    denied = [a for a in IAM_ACTIONS if decisions.get(a) != "allowed"]
    if denied:
        raise SystemExit("IAM denies %s for %s" % (", ".join(denied), principal))
    return {"principal": principal, "verified": True, "allowed": list(IAM_ACTIONS)}


def ro_probe(client, cluster, secret, database):
    """Inside a READ ONLY transaction, CREATE TEMP TABLE must fail with SQLSTATE 25006
    (review B3.2). Either way the transaction is rolled back: the temp table, if it was
    ever created, dies with it."""
    target = {"resourceArn": cluster, "secretArn": secret, "database": database}
    tx = client.begin_transaction(**target)["transactionId"]
    try:
        client.execute_statement(sql="SET TRANSACTION READ ONLY", transactionId=tx, **target)
        show = client.execute_statement(sql="SHOW transaction_read_only", transactionId=tx,
                                        includeResultMetadata=True, **target)
        if show["records"][0][0].get("stringValue") != "on":
            raise SystemExit("read-only probe: SHOW transaction_read_only is not on")
        try:
            client.execute_statement(sql="CREATE TEMP TABLE eval_ro_probe (x int)",
                                     transactionId=tx, **target)
        except Exception as exc:                            # noqa: BLE001
            if "25006" in str(exc):
                return "refused with 25006"
            raise SystemExit("read-only probe failed for another reason: %s" % str(exc)[:200])
        raise SystemExit("read-only probe: the transaction is NOT read-only (CREATE succeeded)")
    finally:
        client.rollback_transaction(transactionId=tx, resourceArn=cluster, secretArn=secret)


def schema_missing(conn):
    """`table.column` / `table` names the reached SQL needs and the database lacks."""
    tables = sorted({t for t, _ in REQUIRED_COLUMNS} | set(REQUIRED_TABLES))
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ANY(%s)", (tables,)).fetchall()
    have = {(str(r["table_name"]), str(r["column_name"])) for r in rows}
    missing = ["%s.%s" % tc for tc in REQUIRED_COLUMNS if tc not in have]
    missing += [t for t in REQUIRED_TABLES if not any(h[0] == t for h in have)]
    return missing


def identity(conn, author_id, site_id):
    """(caller_sub or None, info) for the gold author (review B1). `info` never holds the
    sub itself: only `sub_hash`. ok = has a login, the gold site is in reach, and the
    gold author is inside the author filter (or there is none)."""
    import ask_voice_turn
    from repositories import scope, users
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT cognito_sub, global_role FROM users WHERE id=%s", (author_id,)).fetchone()
    sub = (row or {}).get("cognito_sub") or None
    info = {"has_login": bool(sub), "global_role": (row or {}).get("global_role"),
            "user_scope": None, "site_count": None, "author_count": None,
            "cross_company": None, "gold_site_visible": False, "gold_author_in_scope": False,
            "sub_hash": ask_voice_turn.sub_hash(sub) if sub else None, "ok": False}
    if not sub:
        return None, info
    caller = users.get_user_by_sub(conn, sub)
    if caller is None:
        return sub, info
    vs = scope.visible_scope(conn, caller)
    sites = {str(x).lower() for x in vs["site_ids"]}
    authors = None if vs["author_ids"] is None else {str(a).lower() for a in vs["author_ids"]}
    info.update(user_scope=vs["user_scope"], site_count=len(sites),
                author_count=None if authors is None else len(authors),
                cross_company=bool(vs.get("cross_company")),
                gold_site_visible=site_id.lower() in sites,
                gold_author_in_scope=authors is None or author_id.lower() in authors)
    info["ok"] = info["gold_site_visible"] and info["gold_author_in_scope"]
    return sub, info


def parity_smoke(conn, question, site_id, author_id, family):
    """One build_search_sql statement through the adapter (rendered literals) and
    through run.py's typed spelling (Data API parameters), same values: identical
    (id, distance) lists or stop. Built WITHOUT the optional rewrites, so it compares
    bindings of the same statement (spec §2.4); it proves scalar/array binding, not
    float precision (review S3: both sides use %.7f)."""
    import lexical_terms
    from repositories import search_sql
    from askv2.typed_sql import typed_search_sql
    q_text = lexical_terms.or_query(lexical_terms.query_terms(question))
    sql = search_sql.build_search_sql()
    rendered = conn.cursor(row_factory=dict_row).execute(sql, {
        "q": PARITY_VECTOR, "q_text": q_text, "site_ids": [site_id], "k": PARITY_K,
        "date_from": None, "date_to": None, "author_ids": [author_id],
        "source_family": family}).fetchall()
    typed = conn.run_typed(typed_search_sql(sql, family, optional_rewrites=False), [
        {"name": "q", "value": {"stringValue": "[" + ",".join("%.7f" % x for x in PARITY_VECTOR)
                                + "]"}},
        {"name": "q_text", "value": {"stringValue": q_text}},
        {"name": "site_ids", "value": {"stringValue": "{%s}" % site_id}},
        {"name": "author_ids", "value": {"stringValue": "{%s}" % author_id}},
        {"name": "date_from", "value": {"isNull": True}},
        {"name": "date_to", "value": {"isNull": True}},
        {"name": "k", "value": {"longValue": PARITY_K}},
        {"name": "source_family", "value": {"stringValue": family}}])

    def key(rows):
        return [(str(r.get("id")), round(float(r["distance"]), 6)
                 if r.get("distance") is not None else None) for r in rows]
    if key(rendered) != key(typed):
        raise SystemExit("binding parity failed: %d rendered rows vs %d typed rows differ"
                         % (len(rendered), len(typed)))
    return {"rows": len(rendered), "identical": True}
