"""Test doubles shared by the askv2 tests. No network."""
import re


def col(label, type_name):
    return {"label": label, "name": label, "typeName": type_name}


def result(columns=(), records=()):
    """A Data API execute_statement response."""
    return {"columnMetadata": list(columns), "records": [list(r) for r in records],
            "numberOfRecordsUpdated": 0}


def s(v):
    return {"isNull": True} if v is None else {"stringValue": v}


class FakeRdsData:
    """Scripted `rds-data` client. `routes` is [(regex, response or callable(sql))]; the
    first regex that matches a statement answers it, else an empty result. Internal
    statements answer as a real read-only transaction would."""

    def __init__(self, routes=(), read_only="on"):
        self.routes = list(routes)
        self.read_only = read_only
        self.calls = []                     # (method, kwargs)
        self.fail = None                    # exception to raise on the next template
        self._n = 0

    def begin_transaction(self, **kw):
        self._n += 1
        self.calls.append(("begin_transaction", kw))
        return {"transactionId": "tx-%d" % self._n}

    def rollback_transaction(self, **kw):
        self.calls.append(("rollback_transaction", kw))
        return {"transactionStatus": "Rollback Complete"}

    def execute_statement(self, **kw):
        self.calls.append(("execute_statement", kw))
        sql = kw["sql"]
        if sql == "SHOW transaction_read_only":
            return result([col("transaction_read_only", "text")], [[s(self.read_only)]])
        if re.match(r"(SET TRANSACTION|SAVEPOINT|RELEASE|ROLLBACK TO)", sql):
            return result()
        if self.fail is not None:
            exc, self.fail = self.fail, None
            raise exc
        for pattern, resp in self.routes:
            if re.search(pattern, sql, re.S):
                return resp(sql) if callable(resp) else resp
        return result()

    def statements(self):
        return [kw["sql"] for m, kw in self.calls if m == "execute_statement"]
