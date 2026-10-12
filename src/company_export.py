"""Export one company's lake objects as streamed multipart ZIP parts plus a manifest.

This is the one path where data LEAVES the system, so the selection is the whole point:

* The ONLY source of keys is `lake_objects` rows with `company_id = X AND deleted_at IS NULL`
  (`lake_objects.owners_for_company`, keyset-paged by s3_key). No code path here lists the
  bucket, so another company's object cannot enter by prefix accident. A row with a NULL
  company (unresolved, split_day, external_member_uncertain) is never selected, and the rows
  are re-checked defensively (foreign company, lake_wide/global scope are dropped and counted).
* Content a user deleted is not exported. The product's delete is a reversible mask: the S3
  object stays and a tombstone marks it. `Tombstones` applies every source the product itself
  consults (see the class docstring). It fails CLOSED: if a tombstone table cannot be read the
  export errors out rather than shipping possibly-deleted content.
* Writes happen only under `exports/{company_id}/{export_id}/`. Nothing is ever deleted.

Memory safety on a 1 GiB Lambda: each ZIP part is written (ZIP_STORED, zip64) onto a
non-seekable multipart sink that holds at most one S3 part (8 MiB) in memory; objects are
copied in 1 MiB chunks. No object and no zip is ever held whole in memory.

Resumable: after each closed part `state.json` records the cursor, parts and manifest entries
so far. A call stops when `time_budget_s` is spent and returns `done: False, next_cursor`;
calling again with the same `export_id` continues from the DURABLE state (the passed cursor is
only a hint, so a retried or stale invocation cannot duplicate or skip an entry).
"""
import json
import logging
import re
import time
import uuid
import zipfile

import lake_keys
from repositories import keyframes, lake_objects, redactions

logger = logging.getLogger(__name__)

DEFAULT_PART_BYTES = 512 * 1024 * 1024
DEFAULT_TIME_BUDGET_S = 780
SINK_PART_BYTES = 8 * 1024 * 1024
COPY_CHUNK = 1 << 20
PAGE_SIZE = 1000

_EXTRACTION_PREFIX_RE = re.compile(r"^extractions/([^/]+)/(\d{4}-\d{2}-\d{2})/(.+)$")
_MISSING_CODES = {"NoSuchKey", "404", "NotFound"}


def export_prefix(company_id, export_id) -> str:
    return f"exports/{company_id}/{export_id}/"


def _canon_uuid(value, what):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"{what} must be a UUID") from None


class Tombstones:
    """Every deletion marker the product consults, applied to S3 keys.

    Sources (all read from Aurora, the authority; the S3 deletion mirror
    `redactions/{folder}/{date}/deleted_sessions.json` is only a cache of the first one):

    1. `redactions` rows with target_type='recording', scope='deleted', reverted_at IS NULL.
       `target_key` is the prefix `extractions/{folder}/{date}/{sessionBase}`. A key is
       excluded when it starts with a prefix, when it carries a tombstoned `sid{32hex}` token
       (the presigner's identity, applied to every company), when it sits under the same
       folder/day and contains a tombstoned (legacy, non-sid) session base, or when a
       session-keyed object's session_base equals/starts with one.
    2. The same recording tombstones hide the day's rolled-up report: any
       `reports/{date}/{folder}/...` document of a (folder, date) that has an active
       tombstone is excluded (the presigner and the ask agent do the same: a day report is a
       synthesis with no session id to filter on).
    3. `redactions` rows with target_type='photo' (exact object key, same scope/reverted rule).
    4. `keyframe_tombstones` (exact object key, a reviewer-deleted keyframe).
    5. The mirror object itself (kind `redaction`) is internal bookkeeping and never exported;
       that is handled by the caller's key-shape check, not here.

    Topic redactions with scope 'analysis'/'all' (life-conversation separation) are not
    deletions and name no S3 key; they are not applied.
    """

    def __init__(self, prefixes=()):
        self.prefixes = tuple(p for p in prefixes if isinstance(p, str) and p)
        self.sids = {"sid" + s for p in self.prefixes for s in lake_keys.SID_RE.findall(p)}
        self.by_day = {}          # (folder, day) -> {session bases}
        for p in self.prefixes:
            m = _EXTRACTION_PREFIX_RE.match(p)
            if m:
                self.by_day.setdefault((m.group(1), m.group(2)), set()).add(m.group(3))
        self.bases = {b for bs in self.by_day.values() for b in bs}

    @classmethod
    def load(cls, conn):
        return cls(redactions.deleted_source_prefixes(conn) or [])

    def _key_reason(self, key):
        if self.prefixes and key.startswith(self.prefixes):
            return "recording_prefix"
        if self.sids and any("sid" + s in self.sids for s in lake_keys.SID_RE.findall(key)):
            return "recording_sid"
        if self.by_day:
            parsed = lake_keys.parse(key) or {}
            folder, day = parsed.get("folder"), parsed.get("day")
            if folder and day and (folder, day) in self.by_day:
                name = key.rsplit("/", 1)[-1]
                if any(b in name for b in self.by_day[(folder, day)]):
                    return "recording_base"
                if parsed.get("kind") == "report":
                    return "day_report"
            sb = parsed.get("session_base")
            if sb and not folder and any(sb == b or sb.startswith(b) for b in self.bases):
                return "recording_base"
        return None

    def excluded(self, conn, keys):
        """{key: reason} for the keys that must not be exported."""
        keys = list(keys)
        out = {}
        for k in keys:
            r = self._key_reason(k)
            if r:
                out[k] = r
        rest = [k for k in keys if k not in out]
        if rest:
            for k in redactions.deleted_photo_keys(conn, keys=rest):
                out[k] = "photo_tombstone"
            rest = [k for k in rest if k not in out]
            for k in keyframes.tombstoned_subset(conn, rest):
                out[k] = "keyframe_tombstone"
        return out


class _MultipartSink:
    """A non-seekable file-like that turns a stream of writes into an S3 multipart upload.

    Holds at most `part_size` bytes (a write larger than the room left is sliced), so a ZIP of
    any size streams through constant memory. `close()` uploads the tail and completes the
    upload; `abort()` abandons it so no orphan multipart is left behind.
    """

    def __init__(self, s3, bucket, key, part_size=SINK_PART_BYTES):
        self._s3, self._bucket, self._key = s3, bucket, key
        self._part_size = part_size
        self._buf = bytearray()
        self._pos = 0
        self._parts = []
        self.peak = 0
        self._done = False
        self._aborted = False
        resp = s3.create_multipart_upload(Bucket=bucket, Key=key, ContentType="application/zip")
        self._upload_id = resp["UploadId"]

    # zipfile only needs these on an unseekable stream
    def writable(self):
        return True

    def seekable(self):
        return False

    def tell(self):
        return self._pos

    def flush(self):
        pass

    def write(self, data):
        mv = memoryview(data)
        total = len(mv)
        if self._aborted:       # a zipfile finalised by the GC after an error must not upload
            return total
        off = 0
        while off < total:
            room = self._part_size - len(self._buf)
            take = mv[off:off + room]
            self._buf += take
            off += len(take)
            self.peak = max(self.peak, len(self._buf))
            if len(self._buf) >= self._part_size:
                self._upload(bytes(self._buf))
                self._buf.clear()
        self._pos += total
        return total

    def _upload(self, body):
        n = len(self._parts) + 1
        resp = self._s3.upload_part(Bucket=self._bucket, Key=self._key, UploadId=self._upload_id,
                                    PartNumber=n, Body=body)
        self._parts.append({"ETag": resp["ETag"], "PartNumber": n})

    def close(self):
        if self._done:
            return
        if self._buf or not self._parts:
            self._upload(bytes(self._buf))
            self._buf.clear()
        self._s3.complete_multipart_upload(
            Bucket=self._bucket, Key=self._key, UploadId=self._upload_id,
            MultipartUpload={"Parts": self._parts})
        self._done = True

    def abort(self):
        if self._done:
            return
        self._done = True
        self._aborted = True
        try:
            self._s3.abort_multipart_upload(Bucket=self._bucket, Key=self._key,
                                            UploadId=self._upload_id)
        except Exception:  # noqa: BLE001 - the original error is the one that matters
            logger.exception("export: abort_multipart_upload failed for %s", self._key)


def _is_missing(exc) -> bool:
    code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
    return code in _MISSING_CODES


def _read_state(s3, bucket, key):
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception as exc:  # noqa: BLE001
        if _is_missing(exc):
            return None
        raise
    return json.loads(body)


def _put_json(s3, bucket, key, doc):
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(doc, default=str).encode("utf-8"),
                  ContentType="application/json")


def _unsafe_name(name) -> bool:
    return name.startswith("/") or any(seg == ".." for seg in name.split("/"))


def run(conn, s3, company_id, *, bucket_for, export_id=None, cursor=None,
        time_budget_s=DEFAULT_TIME_BUDGET_S, part_bytes=DEFAULT_PART_BYTES, now=None,
        dest_bucket=None, tombstones=None, clock=time.monotonic, chunk_size=COPY_CHUNK,
        sink_part_bytes=SINK_PART_BYTES, page_size=PAGE_SIZE) -> dict:
    """Export (part of) one company. See the module docstring.

    Returns {company_id, export_id, done, next_cursor, parts: [keys], objects, bytes, ...}.
    """
    from datetime import datetime, timezone
    company_id = _canon_uuid(company_id, "company_id")
    if export_id is None:
        if cursor is not None:
            raise ValueError("a cursor needs the export_id it belongs to")
        export_id = str(uuid.uuid4())
    else:
        export_id = _canon_uuid(export_id, "export_id")
    now = now or datetime.now(timezone.utc)
    prefix = export_prefix(company_id, export_id)
    dest = dest_bucket or bucket_for("exports")
    state_key = prefix + "state.json"

    state = _read_state(s3, dest, state_key)
    if state is None and cursor is not None:
        raise ValueError("cursor given but this export has no saved state")
    if state is None:
        state = {"export_id": export_id, "company_id": company_id, "cursor": None, "parts": [],
                 "entries": [], "bytes": 0, "excluded": {}, "done": False}
    elif state.get("export_id") != export_id or state.get("company_id") != company_id:
        raise ValueError("saved state belongs to a different export")
    if cursor is not None and cursor != state.get("cursor"):
        logger.warning("export %s: passed cursor %r != saved %r; using the saved one",
                       export_id, cursor, state.get("cursor"))

    def result(done):
        return {"company_id": company_id, "export_id": export_id, "done": done,
                "next_cursor": None if done else state["cursor"],
                "parts": [p["key"] for p in state["parts"]], "objects": len(state["entries"]),
                "bytes": state["bytes"], "excluded": dict(state["excluded"])}

    if state.get("done"):
        return result(True)

    tombs = tombstones if tombstones is not None else Tombstones.load(conn)
    started = clock()
    processed = 0
    pos = state["cursor"]
    sink = zf = None
    new_entries = []            # entries since the last durable state
    cur_objects = cur_bytes = 0
    excluded = dict(state["excluded"])
    part_no = len(state["parts"]) + 1
    new_parts = []

    def bump(reason):
        excluded[reason] = excluded.get(reason, 0) + 1

    def close_part():
        nonlocal sink, zf, cur_objects, cur_bytes, part_no
        if zf is None:
            return
        zf.close()
        sink.close()
        new_parts.append({"key": prefix + f"part-{part_no:04d}.zip", "objects": cur_objects,
                          "bytes": cur_bytes})
        sink = zf = None
        part_no += 1
        cur_objects = cur_bytes = 0

    def save(done=False):
        state["parts"] = state["parts"] + new_parts
        new_parts.clear()
        state["entries"] = state["entries"] + new_entries
        new_entries.clear()
        state["cursor"] = None if done else pos
        state["bytes"] += added_box[0]
        added_box[0] = 0
        state["excluded"] = dict(excluded)
        state["done"] = done
        _put_json(s3, dest, state_key, state)

    added_box = [0]
    try:
        finished = False
        while not finished:
            page = lake_objects.owners_for_company(conn, company_id, after_key=pos,
                                                   limit=page_size)
            if not page:
                finished = True
                break
            drop = {}
            keys = []
            for row in page:
                k = row["s3_key"]
                parsed = lake_keys.parse(k)
                if str(row.get("company_id")) != company_id:
                    drop[k] = "foreign_company"
                elif parsed is not None and parsed["scope"] in ("global", "lake_wide"):
                    drop[k] = "not_tenant"
                elif parsed is not None and parsed["kind"] == "redaction":
                    drop[k] = "internal"
                else:
                    keys.append(k)
            drop.update(tombs.excluded(conn, keys))
            for row in page:
                if processed and clock() - started >= time_budget_s:
                    close_part()
                    save()
                    return result(False)
                k = row["s3_key"]
                processed += 1
                reason = drop.get(k)
                if reason:
                    bump(reason)
                    pos = k
                    continue
                kind = row.get("kind") or "other"
                name = f"{kind}/{k}"
                site = str(row["site_id"]) if row.get("site_id") else None
                entry = {"s3_key": k, "kind": kind, "size": row.get("size_bytes"),
                         "part": None, "site_id": site}
                if _unsafe_name(name):
                    entry["status"] = "unsafe_name"
                    new_entries.append(entry)
                    pos = k
                    continue
                try:
                    body = s3.get_object(Bucket=bucket_for(kind), Key=k)["Body"]
                except Exception as exc:  # noqa: BLE001
                    if not _is_missing(exc):
                        raise            # a transient error must not become a silent gap
                    entry["status"] = "missing"
                    new_entries.append(entry)
                    pos = k
                    continue
                if zf is None:
                    sink = _MultipartSink(s3, dest, prefix + f"part-{part_no:04d}.zip",
                                          sink_part_bytes)
                    zf = zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True)
                zi = zipfile.ZipInfo(name, date_time=now.timetuple()[:6])
                zi.compress_type = zipfile.ZIP_STORED
                n = 0
                with zf.open(zi, "w", force_zip64=True) as member:
                    for chunk in body.iter_chunks(chunk_size):
                        member.write(chunk)
                        n += len(chunk)
                if hasattr(body, "close"):
                    body.close()
                entry["part"] = part_no
                entry["size"] = n
                new_entries.append(entry)
                cur_objects += 1
                cur_bytes += n
                added_box[0] += n
                pos = k
                if cur_bytes >= part_bytes:
                    close_part()
                    save()
            if len(page) < page_size:
                finished = True
        close_part()
        manifest = {"company_id": company_id, "export_id": export_id,
                    "generated_at": now.isoformat(),
                    "parts": state["parts"] + new_parts,
                    "objects": state["entries"] + new_entries,
                    "excluded": dict(excluded)}
        _put_json(s3, dest, prefix + "manifest.json", manifest)
        save(done=True)
        return result(True)
    except BaseException:
        if sink is not None:
            sink.abort()
        raise
