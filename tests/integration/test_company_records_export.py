"""company_records_export against a real Postgres: every selection, ownership and tombstone
statement only exists here (a fake connection never parses SQL).

World: companies A and B, one site each, a home user each, each user ALSO an external member
on the other company's site, work by both users on both sites, site-less sessions for both,
plus tombstoned / superseded / archived / deleted rows on A's site. Exporting A must contain
A's rows (including B's person's work on A's site -- project-owned) and nothing of B's
(including A's person's work on B's site)."""
import json
import uuid
from datetime import date, datetime, timezone

import pytest

import company_records_export as cre
from repositories import redactions
from tests.unit.export_fakes import FakeS3

pytestmark = pytest.mark.integration

BKT = "exportbkt"
DAY = "2026-10-01"
NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)

ALL_FILES = {
    "company", "sites", "users", "memberships", "sessions", "topics", "action_items",
    "action_item_history", "decisions", "findings", "observations", "safety_observations",
    "compliance_resolutions", "programmes", "programme_versions", "programme_tasks",
    "programme_task_deps", "programme_task_assignees", "programme_delay_flags",
    "project_documents",
}


def one(db, sql, params=()):
    return db.execute(sql, params).fetchone()[0]


def sid():
    return uuid.uuid4().hex


class World:
    """Seeds the two-company world and remembers, per output file, which ids belong to A
    (must be exported), which belong to B (must never appear) and which of A's are
    tombstoned (must not appear either)."""

    def __init__(self, db):
        self.db = db
        self.want = {f: set() for f in ALL_FILES}      # A's live ids per file
        self.foreign = {f: set() for f in ALL_FILES}   # B's ids per file
        self.hidden = {f: set() for f in ALL_FILES}    # A's tombstoned/superseded ids
        tag = uuid.uuid4().hex[:6]
        self.A = one(db, "INSERT INTO companies (name) VALUES (%s) RETURNING id::text", (f"RecA-{tag}",))
        self.B = one(db, "INSERT INTO companies (name) VALUES (%s) RETURNING id::text", (f"RecB-{tag}",))
        self.want["company"].add(self.A)
        self.foreign["company"].add(self.B)
        self.sa = self._site(self.A, "A Tower")
        self.sb = self._site(self.B, "B Tower")
        self.sa_arch = self._site(self.A, "A Old", archived=True)      # archived: still A's
        self.ua = self._user(self.A, f"RecAda{tag}")
        self.ub = self._user(self.B, f"RecBob{tag}")
        self.fa = f"RecAda{tag}"
        self.ua_arch = self._user(self.A, f"RecOld{tag}", archived=True)
        # memberships: home + external both ways
        self._member(self.ua, self.sa, "pm", False, "A")
        self._member(self.ub, self.sb, "pm", False, "B")
        self._member(self.ub, self.sa, "worker", True, "A")    # B person on A's site -> A's row
        self._member(self.ua, self.sb, "worker", True, "A")    # A person on B's site -> A's row (home user)
        # sessions
        self.dead_sid = sid()
        self._session(self.ua, self.sa, "A")
        self._session(self.ub, self.sa, "A")                    # B's person on A's site: A's
        self._session(self.ua, self.sb, "B")                    # A's person on B's site: B's
        self._session(self.ub, self.sb, "B")
        self._session(self.ua, None, "A")                       # site-less: home company
        self._session(self.ub, None, "B")
        self._session(self.ua, self.sa, "hidden", session_id=self.dead_sid)
        redactions.create_recording_tombstone(db, self.A, f"extractions/{self.fa}/{DAY}/{self.dead_sid}",
                                              "user delete", None, "worker")
        # topics and children
        self._topic(self.sa, self.ua, "A")
        self._topic(self.sa, self.ub, "A")
        self._topic(self.sb, self.ua, "B")
        self._topic(self.sb, self.ub, "B")
        t_del = self._topic(self.sa, self.ua, "hidden")
        db.execute("INSERT INTO redactions (company_id, target_type, target_id, reason, scope) "
                   "VALUES (%s,'topic',%s,'gone','deleted')", (self.A, t_del))
        self._topic(self.sa, self.ua, "hidden", source=f"extractions/{self.fa}/{DAY}/{self.dead_sid}.json")
        self._topic(self.sa, self.ua, "hidden", superseded=True)
        # observations (manual): company_id is the site's company
        self._row("observations", "INSERT INTO observations (company_id, kind, site_slug, report_date, "
                  "author_sub, author_name, observation) VALUES (%s,'safety','a',%s,'x','X','o') RETURNING id::text",
                  (self.A, DAY), "A")
        self._row("observations", "INSERT INTO observations (company_id, kind, site_slug, report_date, "
                  "author_sub, author_name, observation) VALUES (%s,'safety','b',%s,'x','X','o') RETURNING id::text",
                  (self.B, DAY), "B")
        self._row("observations", "INSERT INTO observations (company_id, kind, site_slug, report_date, "
                  "author_sub, author_name, observation, archived_at) VALUES (%s,'quality','a',%s,'x','X','o',now()) "
                  "RETURNING id::text", (self.A, DAY), "hidden")
        # compliance resolutions
        for site, folder, who in ((self.sa, "Someone", "A"), (self.sb, "Someone", "B"),
                                  (self.sa, self.fa, "hidden")):      # folder/day with a tombstone
            self._row("compliance_resolutions",
                      "INSERT INTO compliance_resolutions (company_id, site_id, report_date, user_folder, "
                      "domain, content_hash, content_sample) VALUES ((SELECT company_id FROM sites WHERE id=%s),"
                      "%s,%s,%s,'safety',%s,'s') RETURNING id::text",
                      (site, site, DAY, folder, uuid.uuid4().hex), who)
        # programmes
        self._programme(self.sa, "A")
        self._programme(self.sb, "B")
        # project documents
        for site, who, deleted in ((self.sa, "A", False), (self.sb, "B", False), (self.sa, "hidden", True)):
            self._row("project_documents",
                      "INSERT INTO project_documents (company_id, site_id, series_id, title, filename, "
                      "source_key, content_type, uploaded_by, deleted_at) VALUES "
                      "((SELECT company_id FROM sites WHERE id=%s),%s,gen_random_uuid(),'t','f.pdf','k',"
                      "'application/pdf',%s,%s) RETURNING id::text",
                      (site, site, self.ua, NOW if deleted else None), who)

    # -- helpers -------------------------------------------------------------------------
    def _bucket(self, f, who, value):
        {"A": self.want, "B": self.foreign, "hidden": self.hidden}[who][f].add(value)

    def _row(self, f, sql, params, who):
        rid = one(self.db, sql, params)
        self._bucket(f, who, rid)
        return rid

    def _site(self, company, name, archived=False):
        rid = one(self.db, "INSERT INTO sites (company_id, name, archived_at) VALUES (%s,%s,%s) RETURNING id::text",
                  (company, name, NOW if archived else None))
        self._bucket("sites", "A" if company == self.A else "B", rid)
        return rid

    def _user(self, company, folder, archived=False):
        rid = one(self.db,
                  "INSERT INTO users (cognito_sub, company_id, email, first_name, last_name, global_role, "
                  "folder_name, prefs, avatar_s3_key, archived_at) VALUES (%s,%s,%s,'F','L','worker',%s,"
                  "'{\"api_token\": \"SECRET-PREF\"}'::jsonb,'avatars/secret.png',%s) RETURNING id::text",
                  (f"sub-{uuid.uuid4()}", company, f"{folder}@example.test", folder,
                   NOW if archived else None))
        self._bucket("users", "A" if company == self.A else "B", rid)
        return rid

    def _member(self, user, site, role, external, who):
        self._row("memberships", "INSERT INTO memberships (user_id, site_id, role, external) "
                  "VALUES (%s,%s,%s,%s) RETURNING id::text", (user, site, role, external), who)

    def _session(self, user, site, who, session_id=None):
        session_id = session_id or sid()
        company = one(self.db, "SELECT COALESCE((SELECT company_id FROM sites WHERE id=%s), "
                      "(SELECT company_id FROM users WHERE id=%s))::text", (site, user))
        self.db.execute("INSERT INTO meeting_session (session_id, company_id, user_id, site_id) "
                        "VALUES (%s,%s,%s,%s)", (session_id, company, user, site))
        self._bucket("sessions", who, session_id)

    def _topic(self, site, user, who, source=None, superseded=False):
        tid = one(self.db, "INSERT INTO topics (site_id, user_id, source_s3_key, report_date, title, "
                  "superseded_at) VALUES (%s,%s,%s,%s,'T',%s) RETURNING id::text",
                  (site, user, source, DAY, NOW if superseded else None))
        self._bucket("topics", who, tid)
        ai = self._row("action_items", "INSERT INTO action_items (topic_id, site_id, text) VALUES (%s,%s,'do') "
                       "RETURNING id::text", (tid, site), who)
        self._row("findings", "INSERT INTO findings (topic_id, site_id, observation) VALUES (%s,%s,'f') "
                  "RETURNING id::text", (tid, site), who)
        self._row("decisions", "INSERT INTO topic_decisions (topic_id, site_id, decision) VALUES (%s,%s,'d') "
                  "RETURNING id::text", (tid, site), who)
        self._row("safety_observations", "INSERT INTO safety_observations (topic_id, site_id, observation) "
                  "VALUES (%s,%s,'o') RETURNING id::text", (tid, site), who)
        # status history of the action item; a superseded item's history is still history
        hist_who = "A" if (who == "hidden" and superseded) else who
        self._row("action_item_history",
                  "INSERT INTO content_edits (company_id, table_name, row_id, field, before_text, after_text) "
                  "VALUES ((SELECT company_id FROM sites WHERE id=%s),'action_items',%s,'status','open','done') "
                  "RETURNING id::text", (site, ai), hist_who)
        return tid

    def _programme(self, site, who):
        p = self._row("programmes", "INSERT INTO programmes (site_id, name) VALUES (%s,'P') RETURNING id::text",
                      (site,), who)
        self._row("programme_versions", "INSERT INTO programme_versions (programme_id, version_no, mode) "
                  "VALUES (%s,1,'initial') RETURNING id::text", (p,), who)
        t1 = self._row("programme_tasks", "INSERT INTO programme_tasks (programme_id, origin, name) "
                       "VALUES (%s,'local','one') RETURNING id::text", (p,), who)
        t2 = self._row("programme_tasks", "INSERT INTO programme_tasks (programme_id, origin, name) "
                       "VALUES (%s,'local','two') RETURNING id::text", (p,), who)
        self.db.execute("INSERT INTO programme_task_deps (predecessor_id, successor_id) VALUES (%s,%s)", (t1, t2))
        self._bucket("programme_task_deps", who, f"{t1}|{t2}")
        self.db.execute("INSERT INTO programme_task_assignees (task_id, assignee) VALUES (%s,'Someone')", (t1,))
        self._bucket("programme_task_assignees", who, f"{t1}|Someone")
        self._row("programme_delay_flags", "INSERT INTO programme_delay_flags (task_id, raised_by, reason) "
                  "VALUES (%s,%s,'late') RETURNING id::text", (t1, self.ua), who)


def row_id(f, row):
    if f == "sessions":
        return row["session_id"]
    if f == "programme_task_deps":
        return f"{row['predecessor_id']}|{row['successor_id']}"
    if f == "programme_task_assignees":
        return f"{row['task_id']}|{row['assignee']}"
    return row["id"]


def run(db, s3, company, **kw):
    kw.setdefault("export_id", str(uuid.uuid4()))
    kw.setdefault("now", NOW)
    return cre.run(db, s3, company, dest_bucket=BKT, **kw)


def files(s3, out):
    prefix = f"exports/{out['company_id']}/{out['export_id']}/records/"
    got = {}
    for (b, k), data in s3.objects.items():
        if b == BKT and k.startswith(prefix) and k.endswith(".jsonl"):
            got[k[len(prefix):-len(".jsonl")]] = [json.loads(line) for line in data.decode().splitlines()]
    return got


def raw(s3, out):
    prefix = f"exports/{out['company_id']}/{out['export_id']}/records/"
    return {k[len(prefix):]: v for (b, k), v in s3.objects.items() if b == BKT and k.startswith(prefix)}


# -- tests --------------------------------------------------------------------------------
def test_every_listed_table_is_a_file_and_a_has_exactly_its_own_rows(db):
    w = World(db)
    s3 = FakeS3({}, min_part=1)
    out = run(db, s3, w.A)
    assert out["done"] is True
    got = files(s3, out)
    assert set(got) == ALL_FILES
    assert (BKT, f"exports/{w.A}/{out['export_id']}/records/README.txt") in s3.objects
    for f in ALL_FILES:
        ids = {row_id(f, r) for r in got[f]}
        assert w.foreign[f].isdisjoint(ids), (f, w.foreign[f] & ids)          # zero B rows
        assert w.hidden[f].isdisjoint(ids), (f, w.hidden[f] & ids)            # tombstones held
        assert w.want[f] <= ids, (f, w.want[f] - ids)                         # A complete
    # counts match exactly for the tables this world fully controls
    for f in ALL_FILES - {"company"}:
        ids = [row_id(f, r) for r in got[f]]
        assert len(ids) == len(set(ids)), f                                   # no duplicates
        assert set(ids) == w.want[f], (f, set(ids) ^ w.want[f])
    assert out["tables"]["topics"]["rows"] == len(w.want["topics"])


def test_project_owned_work_and_external_memberships(db):
    w = World(db)
    s3 = FakeS3({}, min_part=1)
    got = files(s3, run(db, s3, w.A))
    # B's person's session on A's site is A's; A's person's session on B's site is not
    by_user_site = {(r["user_id"], r["site_id"]) for r in got["sessions"]}
    assert (w.ub, w.sa) in by_user_site and (w.ua, w.sb) not in by_user_site
    assert (w.ua, None) in by_user_site and (w.ub, None) not in by_user_site
    assert {(r["user_id"], r["site_id"]) for r in got["topics"]} == {(w.ua, w.sa), (w.ub, w.sa)}
    # nothing but memberships may name B's site, and there only A's person's own row
    for f, rows in got.items():
        for r in rows:
            if r.get("site_id") == w.sb:
                assert f == "memberships" and r["user_id"] == w.ua, (f, r)
    assert {r["id"] for r in got["sites"]} == {w.sa, w.sa_arch}               # no B site row
    # users: A's home users only (B's external person is B's directory row)
    assert {r["id"] for r in got["users"]} == {w.ua, w.ua_arch}


def test_users_file_carries_no_secret_or_internal_columns(db):
    w = World(db)
    s3 = FakeS3({}, min_part=1)
    out = run(db, s3, w.A)
    users = files(s3, out)["users"]
    assert users
    for u in users:
        assert set(u) <= set(cre.USER_COLUMNS), set(u) - set(cre.USER_COLUMNS)
        assert "prefs" not in u and "avatar_s3_key" not in u
    blob = raw(s3, out)["users.jsonl"].decode()
    assert "SECRET-PREF" not in blob and "avatars/secret.png" not in blob


def test_tombstones_are_excluded_and_counted(db):
    w = World(db)
    s3 = FakeS3({}, min_part=1)
    out = run(db, s3, w.A)
    ex = out["tables"]
    assert ex["sessions"]["excluded"].get("deleted") == 1
    assert ex["topics"]["excluded"] == {"deleted": 2, "superseded": 1}
    assert ex["action_items"]["excluded"] == {"deleted": 2, "superseded": 1}
    assert ex["observations"]["excluded"] == {"archived": 1}
    assert ex["compliance_resolutions"]["excluded"] == {"deleted_day": 1}
    assert ex["project_documents"]["excluded"] == {"deleted": 1}
    readme = s3.objects[(BKT, f"exports/{w.A}/{out['export_id']}/records/README.txt")].decode()
    assert "secret" in readme.lower() and w.A in readme
    assert "topics.jsonl" in readme and "project-owned" in readme.lower()


def test_the_python_recheck_drops_a_foreign_row_the_sql_let_through(db, monkeypatch):
    """Defence in depth: break a table's SQL so it selects every row; the per-row ownership
    re-check must still drop B's rows and count them, never write them."""
    w = World(db)
    spec = next(s for s in cre.TABLES if s.name == "topics")
    monkeypatch.setattr(spec, "where", "TRUE")
    s3 = FakeS3({}, min_part=1)
    out = run(db, s3, w.A)
    ids = {r["id"] for r in files(s3, out)["topics"]}
    assert w.foreign["topics"].isdisjoint(ids) and ids == w.want["topics"]
    assert out["tables"]["topics"]["excluded"]["foreign_company"] >= len(w.foreign["topics"])


def test_resume_mid_table_yields_identical_output(db):
    w = World(db)
    whole = FakeS3({}, min_part=1)
    full = run(db, whole, w.A, page_size=1, part_bytes=150)
    s3 = FakeS3({}, min_part=1)
    eid = str(uuid.uuid4())
    t = iter(range(100000))
    first = run(db, s3, w.A, export_id=eid, page_size=1, part_bytes=150, time_budget_s=7,
                clock=lambda: next(t))
    assert first["done"] is False
    state_key = (BKT, f"exports/{w.A}/{eid}/records_state.json")
    mid = set()                         # tables some call stopped INSIDE (durable cursor)

    def note_mid():
        st = json.loads(s3.objects[state_key])
        mid.update(n for n, tb in st["tables"].items() if not tb["done"] and tb.get("cursor"))
    note_mid()
    calls = 1
    out = first
    while not out["done"]:
        t2 = iter(range(100000))
        out = run(db, s3, w.A, export_id=eid, page_size=1, part_bytes=150, time_budget_s=7,
                  clock=lambda: next(t2))
        calls += 1
        if not out["done"]:
            note_mid()
        assert calls < 500
    assert calls > 2
    assert mid, "no call ever stopped inside a table"
    assert raw(s3, out).keys() == raw(whole, full).keys()
    for name, data in raw(whole, full).items():
        if name != "README.txt":
            assert raw(s3, out)[name] == data, name
    assert out["tables"] == full["tables"]
    again = run(db, s3, w.A, export_id=eid)                    # idempotent once done
    assert again["done"] and again["tables"] == full["tables"]


def test_a_missing_table_is_an_empty_file_and_a_readme_note(db):
    w = World(db)
    db.execute("DROP TABLE project_documents CASCADE")         # rolled back with the test
    s3 = FakeS3({}, min_part=1)
    out = run(db, s3, w.A)
    assert out["done"]
    assert raw(s3, out)["project_documents.jsonl"] == b""
    assert out["tables"]["project_documents"]["note"].startswith("absent")
    assert "project_documents" in raw(s3, out)["README.txt"].decode()
    assert files(s3, out)["topics"]                            # the rest still exported


def test_export_b_is_the_mirror(db):
    w = World(db)
    s3 = FakeS3({}, min_part=1)
    got = files(s3, run(db, s3, w.B))
    assert {r["session_id"] for r in got["sessions"]} == w.foreign["sessions"]
    assert {r["id"] for r in got["topics"]} == w.foreign["topics"]
    for f in ALL_FILES:
        ids = {row_id(f, r) for r in got[f]}
        assert (w.want[f] | w.hidden[f]).isdisjoint(ids - w.foreign[f]) or f == "memberships", f
    # B's membership file: rows on B's site (incl. A's person) + B's person's row on A's site
    assert {(r["user_id"], r["site_id"]) for r in got["memberships"]} == {
        (w.ub, w.sb), (w.ua, w.sb), (w.ub, w.sa)}


class GoneS3(FakeS3):
    """S3's answer for an upload id that no longer exists (completed, aborted, expired)."""

    def upload_part(self, Bucket, Key, UploadId, PartNumber, Body, **kw):
        if UploadId not in self.uploads:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "NoSuchUpload", "Message": "x"}}, "UploadPart")
        return super().upload_part(Bucket, Key, UploadId, PartNumber, Body, **kw)

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
        if UploadId not in self.uploads:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "NoSuchUpload", "Message": "x"}},
                              "CompleteMultipartUpload")
        return super().complete_multipart_upload(Bucket, Key, UploadId, MultipartUpload)


def test_a_vanished_multipart_upload_restarts_that_table_with_identical_output(db):
    w = World(db)
    whole = FakeS3({}, min_part=1)
    full = run(db, whole, w.A, page_size=1, part_bytes=150)
    s3 = GoneS3({}, min_part=1)
    eid = str(uuid.uuid4())
    state_key = (BKT, f"exports/{w.A}/{eid}/records_state.json")
    for _ in range(200):                     # stop inside a table that has a live upload
        t = iter(range(100000))
        out = run(db, s3, w.A, export_id=eid, page_size=1, part_bytes=150, time_budget_s=5,
                  clock=lambda: next(t))
        st = json.loads(s3.objects[state_key])
        if any(not tb["done"] and tb.get("upload_id") for tb in st["tables"].values()):
            break
    else:
        pytest.fail("never stopped inside a multipart table")
    s3.uploads.clear()                       # the upload is gone (e.g. a crash after complete)
    while not out["done"]:
        out = run(db, s3, w.A, export_id=eid, page_size=1, part_bytes=150)
    for name, data in raw(whole, full).items():
        if name != "README.txt":
            assert raw(s3, out)[name] == data, name
    assert out["tables"] == full["tables"]
