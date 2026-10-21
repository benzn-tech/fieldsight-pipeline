"""Pilot step 0 probes (plan T11) against fakes: IAM simulation, the read-only probe,
the schema preflight (review B2), the identity gate (review B1), the parity smoke, and
the typed search SQL shared with run.py."""
import ast
import subprocess

import pytest

from askv2 import preflight, typed_sql
from askv2.adapter import AdapterLog, DataApiConnection
from askv2.srcref import REPO
from fakes import FakeRdsData, col, result, s

from repositories import search_sql

USER = "f531eb99-d79c-4e6c-bac1-ff944b802349"
COMPANY = "11111111-1111-4111-8111-111111111111"
SITE = "4326dbc7-14a2-4251-8381-9d0067595a85"


def conn(client):
    return DataApiConnection(client, cluster="c", secret="s", database="d", log=AdapterLog())


# --- typed SQL (run.py's spelling, now shared) -----------------------------------

def _old_run_py_data_api_sql(family):
    """run.py's data_api_sql as committed before this change, executed from git."""
    src = subprocess.run(["git", "-C", str(REPO), "show",
                          "4879398e:tools/retrieval-eval/run.py"],
                         capture_output=True, text=True, check=True, encoding="utf-8").stdout
    tree = ast.parse(src)
    keep = [n for n in tree.body
            if (isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None) in ("REWRITES", "OPTIONAL_REWRITES"))
            or (isinstance(n, ast.FunctionDef) and n.name == "data_api_sql")]
    ns = {"search_sql": search_sql, "FAMILY": family}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "old_run_py", "exec"), ns)
    return ns["data_api_sql"]()


@pytest.mark.parametrize("family", [None, "report", "session"])
def test_typed_sql_is_run_py_spelling_unchanged(family):
    assert typed_sql.typed_search_sql(search_sql.build_search_sql(), family) == \
        _old_run_py_data_api_sql(family)


def test_typed_sql_without_optional_rewrites_keeps_the_superseded_predicate():
    with_opt = typed_sql.typed_search_sql(search_sql.build_search_sql(), "report")
    without = typed_sql.typed_search_sql(search_sql.build_search_sql(), "report",
                                         optional_rewrites=False)
    assert "superseded_at" not in with_opt and "superseded_at" in without
    assert "%(" not in without and ":source_family" in without


# --- IAM -------------------------------------------------------------------------

class FakeSts:
    def get_caller_identity(self):
        return {"Arn": "arn:aws:sts::509194952652:assumed-role/Deployer/botocore-session-1",
                "Account": "509194952652"}


class FakeIam:
    def __init__(self, denied=(), unavailable=False):
        self.denied, self.unavailable, self.asked = set(denied), unavailable, []

    def get_role(self, RoleName):
        return {"Role": {"Arn": "arn:aws:iam::509194952652:role/path/" + RoleName}}

    def simulate_principal_policy(self, PolicySourceArn, ActionNames):
        if self.unavailable:
            raise RuntimeError("AccessDenied: iam:SimulatePrincipalPolicy")
        self.asked.append(PolicySourceArn)
        return {"EvaluationResults": [
            {"EvalActionName": a, "EvalDecision": "explicitDeny" if a in self.denied else "allowed"}
            for a in ActionNames]}


def test_iam_simulation_maps_the_assumed_role_and_passes():
    iam = FakeIam()
    out = preflight.iam_check(iam, FakeSts())
    assert iam.asked == ["arn:aws:iam::509194952652:role/path/Deployer"]
    assert out["verified"] is True and set(out["allowed"]) == set(preflight.IAM_ACTIONS)


def test_iam_denied_action_stops():
    with pytest.raises(SystemExit, match="rds-data:BeginTransaction"):
        preflight.iam_check(FakeIam(denied={"rds-data:BeginTransaction"}), FakeSts())


def test_iam_simulation_unavailable_is_reported_not_fatal():
    out = preflight.iam_check(FakeIam(unavailable=True), FakeSts())
    assert out["verified"] is False and "SimulatePrincipalPolicy" in out["note"]


# --- read-only probe ---------------------------------------------------------------

class ProbeClient(FakeRdsData):
    def __init__(self, refuse=True):
        super().__init__()
        self.refuse = refuse

    def execute_statement(self, **kw):
        if kw["sql"].startswith("CREATE TEMP TABLE"):
            self.calls.append(("execute_statement", kw))
            if self.refuse:
                raise RuntimeError("ERROR: cannot execute CREATE TABLE in a read-only "
                                   "transaction; SQLState: 25006")
            return result()
        return super().execute_statement(**kw)


def test_ro_probe_requires_25006_and_rolls_back():
    client = ProbeClient()
    assert preflight.ro_probe(client, "c", "s", "d") == "refused with 25006"
    assert client.calls[-1][0] == "rollback_transaction"


def test_ro_probe_that_could_write_stops():
    client = ProbeClient(refuse=False)
    with pytest.raises(SystemExit, match="NOT read-only"):
        preflight.ro_probe(client, "c", "s", "d")
    assert client.calls[-1][0] == "rollback_transaction"


# --- schema ------------------------------------------------------------------------

def test_schema_preflight_lists_what_is_missing():
    client = FakeRdsData([(r"information_schema\.columns", result(
        [col("table_name", "name"), col("column_name", "name")],
        [[s("topics"), s("superseded_at")], [s("redactions"), s("target_key")],
         [s("redactions"), s("reverted_at")], [s("project_documents"), s("id")]]))])
    missing = preflight.schema_missing(conn(client))
    assert missing == ["memberships.external", "project_doc_chunks"]


# --- identity ------------------------------------------------------------------------

def _identity_client(role="gm", sub="sub-ben", sites=(SITE,)):
    return FakeRdsData([
        (r"FROM users WHERE id=", result([col("cognito_sub", "text"), col("global_role", "text")],
                                         [[s(sub), s(role)]])),
        (r"WHERE users\.cognito_sub=", result(
            [col("id", "uuid"), col("company_id", "uuid"), col("global_role", "text"),
             col("folder_name", "text")], [[s(USER), s(COMPANY), s(role), s("Ben")]])),
        (r"FROM sites WHERE company_id=", result([col("id", "uuid")], [[s(x)] for x in sites])),
    ])


def test_identity_gate_passes_for_a_company_wide_caller_and_hides_the_sub():
    sub, info = preflight.identity(conn(_identity_client()), USER, SITE)
    assert sub == "sub-ben"
    assert info["has_login"] is True and info["global_role"] == "gm"
    assert info["user_scope"] == "ALL" and info["author_count"] is None
    assert info["gold_site_visible"] and info["gold_author_in_scope"] and info["ok"]
    assert "sub-ben" not in repr(info) and len(info["sub_hash"]) == 12


def test_identity_gate_fails_without_a_login():
    sub, info = preflight.identity(conn(_identity_client(sub=None)), USER, SITE)
    assert sub is None and info["has_login"] is False and info["ok"] is False


def test_identity_gate_fails_when_the_gold_site_is_out_of_reach():
    _, info = preflight.identity(conn(_identity_client(sites=())), USER, SITE)
    assert info["gold_site_visible"] is False and info["ok"] is False


# --- parity smoke ----------------------------------------------------------------------

ROW = [[s("a1"), {"doubleValue": 0.31}], [s("a2"), {"doubleValue": 0.4}]]


def _parity_client(typed_rows):
    def answer(sql):
        rows = typed_rows if "CAST(:q AS vector)" in sql else ROW
        return result([col("id", "text"), col("distance", "float8")], rows)
    return FakeRdsData([(r"lexical_hit|vec", answer)])


def test_parity_smoke_matches():
    client = _parity_client(ROW)
    out = preflight.parity_smoke(conn(client), "what did we pour", SITE, USER, "report")
    assert out == {"rows": 2, "identical": True}
    typed = [kw for m, kw in client.calls if m == "execute_statement" and "parameters" in kw]
    assert len(typed) == 1 and {p["name"] for p in typed[0]["parameters"]} >= {"q", "k", "site_ids"}


def test_parity_smoke_mismatch_stops():
    with pytest.raises(SystemExit, match="parity"):
        preflight.parity_smoke(conn(_parity_client(ROW[:1])), "q", SITE, USER, "report")
