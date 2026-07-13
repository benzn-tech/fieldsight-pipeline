# SP4a 录制上传后端 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 FieldSight Org API 加一张 `recordings` 媒体表和两个端点,让移动端(GrandTime)能请求预签名 S3 PUT 上传录制、并落地每条录制的元数据 + 工地归属。

**Architecture:** 新迁移建 `recordings` 表 → `repositories/recordings.py` 数据层 → `lambda_org_api.py` 的 `dispatch()` 加两条路由:`POST /api/recordings/upload-url`(建 pending 行 + 预签名 PUT)与 `POST /api/recordings/{id}/complete`(标 uploaded)→ `template.yaml` 给 OrgApiFunction 加 `users/*` 的 S3 PutObject 权限。S3 键沿用现有 `users/{name}/{kind}/{date}/` 约定,与下游转写/报告管道无缝。

**Tech Stack:** Aurora PostgreSQL 16.4 + psycopg3(无 ORM)、AWS SAM、boto3 预签名、pytest。

设计依据:`docs/superpowers/specs/`(GrandTime 仓 `2026-07-13-sp4-upload-project-selection-design.md` §4;本仓待同步一份)。

## Global Constraints

- **数据层**:psycopg3,`dict_row` row factory,`_COLS` 模块常量 + f-string 插值;**repository 函数绝不 commit/rollback**(连接生命周期由 `lambda_handler` 的 `with get_connection() as conn:` 管);写函数用位置 `%s` 参数化。
- **迁移**:文件名 4 位递增,当前最高 `0008_programme_suggestions.sql` → **新文件 `src/migrations/0009_recordings.sql`**;丢进 `src/migrations/` 即被 `db/migrate.py` 和测试 `migrated_db_url` fixture 自动应用,无需注册。
- **路由**:Org API 是 `/api/org/{proxy+}` ANY 代理,**`template.yaml` 的 `Events:` 不用改**;新路由加在 `lambda_org_api.py` 的 `dispatch()` 里、`return error("not found", 404)` 哨兵之前,紧跟现有 `/upload-url` 块。
- **handler 签名**:`dispatch()` 已把 `caller` 预校验好(company_id 存在、非 archived);新 handler 形如 `def create_recording_upload_url(conn, caller, body)`,**不要**再查 `caller is None`;只做 recordings 相关的 company/ACL 守卫。
- **响应**:一律走 `ok(body, status)` / `error(message, status)`;POST body 用 `parse_body(event)`(返回 dict 或 None)。
- **认证**:idToken 的 `sub` 由 `dispatch()` 经 `users.get_user_by_sub` 解析成 `caller`;recordings 的 `user_id` = `caller["id"]`,`company_id` = `caller["company_id"]`。
- **预签名**:`s3().generate_presigned_url("put_object", Params={"Bucket": S3_BUCKET, "Key": key, "ContentType": content_type}, ExpiresIn=PRESIGNED_URL_EXPIRY)`;`S3_BUCKET = os.environ["S3_BUCKET"]`(env,来自 `template.yaml` 的 `DataBucketName`),`PRESIGNED_URL_EXPIRY=900`。
- **S3 键**:**不 import** `lambda_orchestrator.generate_s3_key`(不可移植,依赖 S3 查表);在本 Lambda 内**重实现**约定 `users/{safe(display_name)}/{content_folder}/{date}/{safe(file_name)}`,`display_name = caller["folder_name"] or f'{first_name}_{last_name}'`,`content_folder`: video→`video`、audio→`audio`、photo→`pictures`,`date` 取 `started_at` 的 `YYYY-MM-DD`。
- **幂等**:`recordings.client_uuid`(= 设备端 capture_records.id)唯一约束;upload-url 若 `client_uuid` 已存在,返回同一行 + 新预签名 URL,**不重复建行**。
- **测试**:pytest。端点单测(`tests/unit/test_recordings_api.py`)全 monkeypatch——`org.get_connection`→`FakeConn`、`org.users.get_user_by_sub`→假 caller、`org.recordings.*`→桩、`org._s3_client`→`FakeS3`;**不引 moto**,用手写 `FakeS3`。仓库集成测(`tests/integration/test_recordings_repo.py`,`@pytest.mark.integration`)用真 PG 的 `db` fixture(每测 rollback 隔离)。命令:单测 `python -m pytest -v`;含集成 `TEST_DATABASE_URL=postgresql://fieldsight:fieldsight@localhost:5432/fieldsight_test python -m pytest -v`。
- **IAM**:给 `OrgApiFunction` 的 `Policies` 追加一条 `users/*` 的 `s3:PutObject`+`s3:GetObject` Statement(现仅 `org-assets/*` + `programmes/*`)。
- **桶待确认(风险)**:`OrgApiFunction` 的 `S3_BUCKET` env = `DataBucketName`,当前与 `IngestBucketName`(注释称"真正数据湖")指向同一物理桶 `fieldsight-data-509194952652`。本计划用 `S3_BUCKET`。**实建时向用户确认**若 prod 将两桶分离,预签名 + IAM 是否要改指 `IngestBucketName`(下游管道读的那个)。
- **site_id ACL**:upload-url 若带 `siteId`,须校验其属 caller 公司(`sites.get_site(conn, siteId)` 存在且 `company_id == caller["company_id"]`),否则 403;不给则存 NULL(允许未选工地上传)。

---

### Task 1: 迁移 `0009_recordings.sql` + `repositories/recordings.py`(TDD 集成)

**Files:**
- Create: `src/migrations/0009_recordings.sql`
- Create: `src/repositories/recordings.py`
- Test: `tests/integration/test_recordings_repo.py`

**Interfaces:**
- Produces:
  - 表 `recordings`(见下 SQL)
  - `recordings.insert_pending(conn, company_id, user_id, site_id, kind, s3_key, client_uuid, started_at, ended_at, duration_s, resolution, codec, size_bytes) -> dict`
  - `recordings.get_by_client_uuid(conn, user_id, client_uuid) -> dict | None`
  - `recordings.get_by_id(conn, rec_id) -> dict | None`
  - `recordings.mark_uploaded(conn, rec_id, company_id, size_bytes) -> dict | None`(company 守卫)

- [ ] **Step 1: 写失败测试 `tests/integration/test_recordings_repo.py`**

```python
import pytest
from repositories import companies, users, sites, recordings

pytestmark = pytest.mark.integration


def _seed(db):
    co = companies.create_company(db, "Acme", industry="construction")
    u = users.upsert_user(db, "sub-rec", "r@acme.com", company_id=co["id"], global_role="pm")
    s = sites.create_site(db, co["id"], "North Wharf", location="Auckland")
    return co, u, s


def test_insert_get_and_idempotency(db):
    co, u, s = _seed(db)
    row = recordings.insert_pending(
        db, company_id=co["id"], user_id=u["id"], site_id=s["id"], kind="video",
        s3_key="users/r/video/2026-07-13/x.mp4", client_uuid="cap-1",
        started_at="2026-07-13T16:01:58Z", ended_at="2026-07-13T16:04:00Z",
        duration_s=122, resolution="1920x1080", codec="h264", size_bytes=None,
    )
    assert row["id"] and row["uploaded_at"] is None and row["site_id"] == s["id"]
    assert recordings.get_by_id(db, row["id"])["s3_key"].endswith("x.mp4")
    assert recordings.get_by_client_uuid(db, u["id"], "cap-1")["id"] == row["id"]
    assert recordings.get_by_client_uuid(db, u["id"], "nope") is None


def test_null_site_allowed(db):
    co, u, s = _seed(db)
    row = recordings.insert_pending(
        db, company_id=co["id"], user_id=u["id"], site_id=None, kind="photo",
        s3_key="users/r/pictures/2026-07-13/y.jpg", client_uuid="cap-2",
        started_at="2026-07-13T16:05:00Z", ended_at=None, duration_s=None,
        resolution=None, codec=None, size_bytes=None,
    )
    assert row["site_id"] is None and row["kind"] == "photo"


def test_mark_uploaded_company_guarded(db):
    co, u, s = _seed(db)
    other = companies.create_company(db, "Other")
    row = recordings.insert_pending(
        db, company_id=co["id"], user_id=u["id"], site_id=None, kind="audio",
        s3_key="users/r/audio/2026-07-13/z.wav", client_uuid="cap-3",
        started_at="2026-07-13T16:06:00Z", ended_at=None, duration_s=None,
        resolution=None, codec=None, size_bytes=None,
    )
    assert recordings.mark_uploaded(db, row["id"], other["id"], 999) is None, "wrong company must not update"
    done = recordings.mark_uploaded(db, row["id"], co["id"], 12345)
    assert done["uploaded_at"] is not None and done["size_bytes"] == 12345
```

- [ ] **Step 2: 跑测试确认失败** — `TEST_DATABASE_URL=postgresql://fieldsight:fieldsight@localhost:5432/fieldsight_test python -m pytest tests/integration/test_recordings_repo.py -v` → FAIL(表/模块不存在)。

- [ ] **Step 3: 写迁移 `src/migrations/0009_recordings.sql`**

```sql
-- 0009: recordings — per-recording media metadata pushed by the mobile app
-- (GrandTime), with app-tagged site attribution. spec:
-- docs/superpowers/specs/2026-07-13-sp4-upload-project-selection-design.md §4
CREATE TABLE recordings (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id   uuid NOT NULL REFERENCES companies(id),
  user_id      uuid NOT NULL REFERENCES users(id),
  site_id      uuid REFERENCES sites(id),
  kind         text NOT NULL CHECK (kind IN ('video','audio','photo')),
  s3_key       text NOT NULL,
  client_uuid  text NOT NULL,
  started_at   timestamptz NOT NULL,
  ended_at     timestamptz,
  duration_s   numeric,
  resolution   text,
  codec        text,
  size_bytes   bigint,
  gps_track    jsonb,
  uploaded_at  timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_recordings_s3_key ON recordings (s3_key);
CREATE UNIQUE INDEX uq_recordings_client_uuid ON recordings (user_id, client_uuid);
CREATE INDEX idx_recordings_site ON recordings (site_id);
CREATE INDEX idx_recordings_user_started ON recordings (user_id, started_at);
```

- [ ] **Step 4: 写 `src/repositories/recordings.py`**

```python
from psycopg.rows import dict_row

_COLS = ("id, company_id, user_id, site_id, kind, s3_key, client_uuid, started_at, "
         "ended_at, duration_s, resolution, codec, size_bytes, gps_track, uploaded_at, created_at")


def insert_pending(conn, company_id, user_id, site_id, kind, s3_key, client_uuid,
                   started_at, ended_at=None, duration_s=None, resolution=None,
                   codec=None, size_bytes=None) -> dict:
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO recordings (company_id, user_id, site_id, kind, s3_key, client_uuid, "
        f"started_at, ended_at, duration_s, resolution, codec, size_bytes) "
        f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_COLS}",
        (company_id, user_id, site_id, kind, s3_key, client_uuid,
         started_at, ended_at, duration_s, resolution, codec, size_bytes),
    ).fetchone()


def get_by_client_uuid(conn, user_id, client_uuid) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM recordings WHERE user_id=%s AND client_uuid=%s",
        (user_id, client_uuid),
    ).fetchone()


def get_by_id(conn, rec_id) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM recordings WHERE id=%s", (rec_id,)
    ).fetchone()


def mark_uploaded(conn, rec_id, company_id, size_bytes=None) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE recordings SET uploaded_at=now(), size_bytes=COALESCE(%s, size_bytes) "
        f"WHERE id=%s AND company_id=%s RETURNING {_COLS}",
        (size_bytes, rec_id, company_id),
    ).fetchone()
```

- [ ] **Step 5: 跑测试确认通过** — 同 Step 2 命令 → PASS(3 测试)。

- [ ] **Step 6: Commit** — `git add src/migrations/0009_recordings.sql src/repositories/recordings.py tests/integration/test_recordings_repo.py && git commit -m "feat(db): recordings table + repository (SP4a)"`

---

### Task 2: `POST /api/recordings/upload-url` 端点 + S3 键构造(TDD 单测)

**Files:**
- Modify: `src/lambda_org_api.py`(加 S3 键 helper + `create_recording_upload_url` + dispatch 路由)
- Test: `tests/unit/test_recordings_api.py`

**Interfaces:**
- Consumes:Task 1 的 `recordings.*`;`sites.get_site`;`org._s3_client`/`s3()`;`S3_BUCKET`/`PRESIGNED_URL_EXPIRY`。
- Produces:路由 `POST /api/org/recordings/upload-url`;handler `create_recording_upload_url(conn, caller, body)`;helper `_recording_s3_key(display_name, kind, started_at, file_name)`。

**注意**:代理路径去掉 `/api/org` 前缀后,route 为 `/recordings/upload-url`。

- [ ] **Step 1: 写失败测试 `tests/unit/test_recordings_api.py`**

```python
import json, uuid
import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")


class FakeConn:
    def __enter__(self): return self
    def __exit__(self, *a): return False


class FakeS3:
    def generate_presigned_url(self, op, Params=None, ExpiresIn=0):
        self.last = {"op": op, "params": Params, "expires": ExpiresIn}
        return "https://s3.example/" + Params["Key"]


CALLER = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-1", "email": "a@x.nz",
          "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
          "global_role": "pm", "created_at": "2026-07-04", "archived_at": None}


def make_event(method, path, sub="sub-1", body=None):
    return {"httpMethod": method, "path": path, "queryStringParameters": None,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"claims": {"sub": sub} if sub else {}}}}


def body_of(res): return json.loads(res["body"])


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER) if sub == "sub-1" else None)
    fake = FakeS3()
    monkeypatch.setattr(org, "_s3_client", fake)
    return monkeypatch, fake


def test_upload_url_creates_row_and_presigns(wired):
    mp, fake = wired
    created = {}
    def fake_insert(conn, **kw):
        created.update(kw); return {"id": "rec-1", **kw, "uploaded_at": None}
    mp.setattr(org.recordings, "get_by_client_uuid", lambda c, u, cu: None)
    mp.setattr(org.recordings, "insert_pending", fake_insert)
    mp.setattr(org.sites, "get_site", lambda c, sid: {"id": sid, "company_id": "c-1"})

    res = org.lambda_handler(make_event("POST", "/api/org/recordings/upload-url", body={
        "kind": "video", "clientUuid": "cap-1", "siteId": "s-1",
        "fileName": "Ada_L_20260713_160158.mp4", "contentType": "video/mp4",
        "startedAt": "2026-07-13T16:01:58Z", "durationS": 122,
        "resolution": "1920x1080", "codec": "h264"}), None)

    assert res["statusCode"] == 200
    b = body_of(res)
    assert b["recordingId"] == "rec-1"
    assert b["s3Key"] == "users/Ada_L/video/2026-07-13/Ada_L_20260713_160158.mp4"
    assert b["uploadUrl"].endswith(b["s3Key"])
    assert fake.last["op"] == "put_object" and fake.last["params"]["ContentType"] == "video/mp4"
    assert created["kind"] == "video" and created["site_id"] == "s-1" and created["user_id"] == "u-1"


def test_photo_maps_to_pictures_folder(wired):
    mp, fake = wired
    mp.setattr(org.recordings, "get_by_client_uuid", lambda c, u, cu: None)
    mp.setattr(org.recordings, "insert_pending", lambda conn, **kw: {"id": "rec-2", **kw})
    res = org.lambda_handler(make_event("POST", "/api/org/recordings/upload-url", body={
        "kind": "photo", "clientUuid": "cap-2", "siteId": None,
        "fileName": "Ada_L_20260713_160314.jpg", "contentType": "image/jpeg",
        "startedAt": "2026-07-13T16:03:14Z"}), None)
    assert body_of(res)["s3Key"] == "users/Ada_L/pictures/2026-07-13/Ada_L_20260713_160314.jpg"


def test_upload_url_idempotent_on_client_uuid(wired):
    mp, fake = wired
    mp.setattr(org.recordings, "get_by_client_uuid",
               lambda c, u, cu: {"id": "rec-existing", "s3_key": "users/Ada_L/video/2026-07-13/old.mp4"})
    called = {"insert": False}
    mp.setattr(org.recordings, "insert_pending",
               lambda conn, **kw: called.__setitem__("insert", True) or {"id": "x"})
    res = org.lambda_handler(make_event("POST", "/api/org/recordings/upload-url", body={
        "kind": "video", "clientUuid": "cap-1", "siteId": None,
        "fileName": "new.mp4", "contentType": "video/mp4",
        "startedAt": "2026-07-13T16:01:58Z"}), None)
    b = body_of(res)
    assert b["recordingId"] == "rec-existing" and called["insert"] is False
    assert b["s3Key"] == "users/Ada_L/video/2026-07-13/old.mp4"


def test_site_from_other_company_rejected(wired):
    mp, fake = wired
    mp.setattr(org.recordings, "get_by_client_uuid", lambda c, u, cu: None)
    mp.setattr(org.sites, "get_site", lambda c, sid: {"id": sid, "company_id": "OTHER"})
    res = org.lambda_handler(make_event("POST", "/api/org/recordings/upload-url", body={
        "kind": "video", "clientUuid": "cap-9", "siteId": "s-x",
        "fileName": "a.mp4", "contentType": "video/mp4", "startedAt": "2026-07-13T16:01:58Z"}), None)
    assert res["statusCode"] == 403


def test_bad_kind_rejected(wired):
    mp, fake = wired
    res = org.lambda_handler(make_event("POST", "/api/org/recordings/upload-url", body={
        "kind": "movie", "clientUuid": "c", "fileName": "a.x", "contentType": "video/mp4",
        "startedAt": "2026-07-13T16:01:58Z"}), None)
    assert res["statusCode"] == 400
```

- [ ] **Step 2: 跑测试确认失败** — `python -m pytest tests/unit/test_recordings_api.py -v` → FAIL(路由 404 / helper 不存在)。

- [ ] **Step 3: 实现**

在 `src/lambda_org_api.py` 顶部 helper 区加(靠近其它模块函数;`re` 已在文件顶部 import,勿重复 import):

```python
_KIND_FOLDER = {"video": "video", "audio": "audio", "photo": "pictures"}
_CONTENT_TYPES = {"video", "audio", "photo"}


def _safe_seg(s):
    # 保守清洗:非字母数字/点/下划线/连字符 → 下划线(re 已在文件顶部 import)
    return re.sub(r"[^A-Za-z0-9._-]", "_", (s or "").strip()) or "unknown"


def _recording_s3_key(display_name, kind, started_at, file_name):
    date_str = str(started_at)[:10]  # ISO 'YYYY-MM-DD...' → 'YYYY-MM-DD'
    folder = _KIND_FOLDER[kind]
    return f"users/{_safe_seg(display_name)}/{folder}/{date_str}/{_safe_seg(file_name)}"


def create_recording_upload_url(conn, caller, body):
    if body is None:
        return error("malformed JSON body", 400)
    kind = body.get("kind")
    if kind not in _CONTENT_TYPES:
        return error(f"kind must be one of {sorted(_CONTENT_TYPES)}", 400)
    client_uuid = body.get("clientUuid")
    file_name = body.get("fileName")
    content_type = body.get("contentType")
    started_at = body.get("startedAt")
    if not (client_uuid and file_name and content_type and started_at):
        return error("clientUuid, fileName, contentType, startedAt are required", 400)

    site_id = body.get("siteId")
    if site_id:
        site = sites.get_site(conn, site_id)
        if site is None or site["company_id"] != caller["company_id"]:
            return error("site not accessible", 403)

    # 幂等:同一设备录制(clientUuid)重发 → 复用已建行,只重新签 URL
    existing = recordings.get_by_client_uuid(conn, caller["id"], client_uuid)
    if existing is not None:
        rec_id, key = existing["id"], existing["s3_key"]
    else:
        display_name = caller.get("folder_name") or f"{caller.get('first_name','')}_{caller.get('last_name','')}"
        key = _recording_s3_key(display_name, kind, started_at, file_name)
        row = recordings.insert_pending(
            conn, company_id=caller["company_id"], user_id=caller["id"], site_id=site_id,
            kind=kind, s3_key=key, client_uuid=client_uuid, started_at=started_at,
            ended_at=body.get("endedAt"), duration_s=body.get("durationS"),
            resolution=body.get("resolution"), codec=body.get("codec"),
            size_bytes=body.get("sizeBytes"),
        )
        rec_id = row["id"]

    url = s3().generate_presigned_url(
        "put_object",
        Params={"Bucket": S3_BUCKET, "Key": key, "ContentType": content_type},
        ExpiresIn=PRESIGNED_URL_EXPIRY,
    )
    return ok({"recordingId": rec_id, "uploadUrl": url, "s3Key": key})
```

在 `dispatch()` 里、`/upload-url` 块之后、`return error("not found", 404)` 之前加路由:

```python
    if route == "/recordings/upload-url" and method == "POST":
        return create_recording_upload_url(conn, caller, parse_body(event))
```

顶部确保已 import:`from repositories import recordings`(与现有 `sites`/`users` 同一 import 行或新增)。

- [ ] **Step 4: 跑测试确认通过** — `python -m pytest tests/unit/test_recordings_api.py -v` → PASS(5)。

- [ ] **Step 5: Commit** — `git add src/lambda_org_api.py tests/unit/test_recordings_api.py && git commit -m "feat(api): POST /recordings/upload-url — presigned PUT + pending row (SP4a)"`

---

### Task 3: `POST /api/recordings/{id}/complete` 端点(TDD 单测)

**Files:**
- Modify: `src/lambda_org_api.py`(`complete_recording` + dispatch 路由)
- Test: 追加到 `tests/unit/test_recordings_api.py`

**Interfaces:**
- Produces:路由 `POST /api/org/recordings/{id}/complete`;handler `complete_recording(conn, caller, rec_id, body)`。

- [ ] **Step 1: 追加失败测试**

```python
def test_complete_marks_uploaded(wired):
    mp, fake = wired
    seen = {}
    mp.setattr(org.recordings, "mark_uploaded",
               lambda c, rid, cid, sz: seen.update(rid=rid, cid=cid, sz=sz) or
               {"id": rid, "uploaded_at": "2026-07-13T16:10:00Z", "size_bytes": sz})
    res = org.lambda_handler(make_event("POST", "/api/org/recordings/rec-1/complete",
                                        body={"sizeBytes": 12345}), None)
    assert res["statusCode"] == 200 and body_of(res)["ok"] is True
    assert seen == {"rid": "rec-1", "cid": "c-1", "sz": 12345}


def test_complete_unknown_or_wrong_company_404(wired):
    mp, fake = wired
    mp.setattr(org.recordings, "mark_uploaded", lambda c, rid, cid, sz: None)
    res = org.lambda_handler(make_event("POST", "/api/org/recordings/rec-x/complete",
                                        body={}), None)
    assert res["statusCode"] == 404
```

- [ ] **Step 2: 跑测试确认失败** — `python -m pytest tests/unit/test_recordings_api.py -k complete -v` → FAIL(路由 404)。

- [ ] **Step 3: 实现**

handler(加在 `create_recording_upload_url` 附近):

```python
def complete_recording(conn, caller, rec_id, body):
    size_bytes = (body or {}).get("sizeBytes")
    row = recordings.mark_uploaded(conn, rec_id, caller["company_id"], size_bytes)
    if row is None:
        return error("recording not found", 404)
    return ok({"ok": True})
```

dispatch 路由(紧跟 `/recordings/upload-url` 之后,哨兵之前):

```python
    m_rc = re.match(r"^/recordings/([^/]+)/complete$", route)
    if m_rc and method == "POST":
        return complete_recording(conn, caller, m_rc.group(1), parse_body(event))
```

- [ ] **Step 4: 跑测试确认通过** — `python -m pytest tests/unit/test_recordings_api.py -v` → PASS(7 全绿)。

- [ ] **Step 5: Commit** — `git add src/lambda_org_api.py tests/unit/test_recordings_api.py && git commit -m "feat(api): POST /recordings/{id}/complete — mark uploaded (SP4a)"`

---

### Task 4: IAM 授权 `users/*` PutObject + 模板校验

**Files:**
- Modify: `src/template.yaml`(`OrgApiFunction` 的 `Policies`)

**Interfaces:**
- 无代码接口;交付 = 部署后 OrgApiFunction 能对 `users/*` 预签名 PUT 并写入。

- [ ] **Step 1: 加 IAM Statement**

在 `OrgApiFunction` 的 `Policies:` 内,现有 `programmes/*` 那条 `s3:PutObject`/`s3:GetObject` Statement 之后,追加:

```yaml
            - Effect: Allow
              Action:
                - s3:PutObject
                - s3:GetObject
              Resource: !Sub arn:aws:s3:::${DataBucketName}/users/*
```

- [ ] **Step 2: 模板校验** — `sam validate --lint`(或仓库既有校验命令)→ 通过,无语法/资源错误。

- [ ] **Step 3: Commit** — `git add src/template.yaml && git commit -m "chore(iam): OrgApiFunction s3:PutObject on users/* for recordings upload (SP4a)"`

---

## 部署与验收(SDD 全部任务后)

1. 部署栈(SAM),手动 invoke MigrateFunction 应用 `0009`(或按仓库既有部署流程)。
2. 端到端脚本冒烟(真 idToken):`POST /api/org/recordings/upload-url` → 拿 `uploadUrl` → `curl -X PUT -H "Content-Type: video/mp4" --data-binary @clip.mp4 "<uploadUrl>"` → 文件落 `users/{name}/video/{date}/` → `POST /recordings/{id}/complete` → 查 recordings 行 `uploaded_at` 非空、`site_id` 正确。
3. 负例:过期/非法 token 401;跨公司 siteId 403;非法 kind 400;重复 clientUuid 不重复建行。
4. 确认桶(见 Global Constraints「桶待确认」)与下游管道读的一致。

完成后进 SP4b(GrandTime 移动端)独立 plan。
