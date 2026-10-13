# Timeline Audio Dedup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The web timeline's audio list shows each stretch of speech once — a 30 s chunk wav is dropped from the listing when a batch wav in the same result already contains it.

**Architecture:** Batching stitches several 30 s chunk wavs into one batch wav written beside them under `audio_segments/{folder}/{date}/`, plus a `*_batch_map.json` sidecar that names every member chunk's S3 key. Both audio-list endpoints (org-api `/api/org/audio-segments` and the legacy gateway `/api/audio-segments`) list that prefix flat and return both, so the same audio plays twice. A new pure module `src/batch_cover.py` reads the sidecars of the batches in a listing (concurrently, fail-open) and returns the member keys they cover; both endpoints drop those keys before presigning.

**Tech Stack:** Python 3.12 Lambdas (SAM, `CodeUri: src/` bundles every module), boto3, pytest with in-repo fakes, `concurrent.futures.ThreadPoolExecutor`.

**Spec:** No spec document. The owner's decisions (2026-09-17/18 session) and the evidence behind them are:
- Owner rule: "按你的推荐去重" = hide a chunk ONLY when a batch in the same listing covers it; never hide by "is 30 s long".
- Research (read it before Task 1): `C:/Users/camil/AppData/Local/Temp/claude/C--Users-camil-Dropbox/ae81ae99-4b24-47e8-a14b-4a8f50179b99/scratchpad/plan-research/timeline-dedup.md`

## Global Constraints

- Discriminator is the batch map's `members[].chunk_key` list. **Never** derive membership from the batch filename's `c{first}_bn{count}`: measured on prod `Ben_UCPK2/2026-09-10`, 2 of 33 maps disagree with the filename because a VAD-rejected chunk is bridged (`c1035_bn4` → members 1035, 1036, 1037, **1039**).
- **Fail open.** A missing, unreadable (403 or 404), or malformed map hides nothing. The worst outcome allowed is a duplicate on screen, never a missing recording.
- A batch key never hides a batch key.
- Days with no batch objects (e.g. prod `Ben_UCPK2/2026-08-07`, `2026-08-13`) must return exactly what they return today.
- Only batches that survive the time-window filter can hide chunks ("covered by a batch **in this result**").
- The response shape is unchanged: `{"segments": [...], "count": N}`, same per-segment fields. Do not add fields.
- Sidecar reads are concurrent (busiest day measured: 215 batches; org-api sits behind API Gateway's 29 s limit). Cap workers at 16.
- `batch_cover.py` imports only the standard library and `batch_stitch` — no boto3, no psycopg. The S3 read is injected as a callable, so the in-VPC org-api and the non-VPC legacy gateway can share it without either one's client leaking into the other.
- Development artefacts (code comments, commit messages, docs) in English. Commit messages end with:
  ```
  Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01N2ZDWwHWVd7mH4NNKX1kjt
  ```
- Test harness (run from the worktree root `C:/Users/camil/fswork/timeline-audio-dedup`, Git Bash):
  ```bash
  export UV_LINK_MODE=copy AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=ap-southeast-2
  uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest <paths> -q
  ```
- Windows repo: never `git add -A`; add files by path.

## File Structure

- Create `src/batch_cover.py` — one responsibility: given batch wav keys and a byte reader, return the set of chunk keys those batches cover.
- Create `tests/unit/test_batch_cover.py` — unit tests for the module.
- Modify `src/lambda_org_api.py` — `_read_org_audio_segments` (≈ line 8589) drops covered chunks; add `import batch_cover` beside `import batch_stitch` (≈ line 161).
- Modify `tests/unit/test_lambda_org_api.py` — dedup tests beside the existing `test_audio_segments_*` block (≈ lines 4027–4276).
- Modify `src/lambda_fieldsight_api.py` — `get_audio_segments` (≈ line 888) drops covered chunks; add `import batch_cover` to the import block.
- Modify `tests/unit/test_lambda_fieldsight_api_media_window.py` — one dedup test using that file's existing fake.

---

### Task 1: `batch_cover.covered_chunk_keys`

**Files:**
- Create: `src/batch_cover.py`
- Test: `tests/unit/test_batch_cover.py`

**Interfaces:**
- Consumes: `batch_stitch.is_batch_key(key: str) -> bool`, `batch_stitch.map_key_for_audio(batch_audio_key: str) -> str` (both exist, `src/batch_stitch.py:267` and `:335`).
- Produces: `batch_cover.covered_chunk_keys(get_bytes, keys, max_workers=16) -> set[str]` where `get_bytes(key: str) -> bytes` may raise; `keys` is any iterable of S3 keys (batch and non-batch mixed). Also `batch_cover.MAX_MAP_READERS = 16`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_batch_cover.py`:

```python
"""A batch wav is the same speech as the chunk wavs it was stitched from. The map beside
it is the only authority on which chunks those are -- the filename is not (a chunk VAD
rejected is bridged, so c1035_bn4 holds 1035, 1036, 1037 and 1039)."""
import json

import batch_cover

SID = "449c1bb3473c405985351aaa189049e6"
P = "audio_segments/Ben_Lin_test2/2026-09-17/"


def chunk(i, base="13-05-18", to="30.0"):
    return f"{P}ben_lin_2026-09-17_{base}_sid{SID}_c{i:04d}_off0.0_to{to}_srcwav.wav"


def batch(first, count, to):
    return f"{P}ben_lin_2026-09-17_13-05-18_sid{SID}_c{first:04d}_bn{count}_off0.0_to{to}_srcwav.wav"


def map_of(batch_key, member_keys):
    stem = batch_key[: -len(".wav")]
    return stem + "_batch_map.json", json.dumps(
        {"schema": 1, "session_id": SID,
         "members": [{"chunk_index": n, "chunk_key": k} for n, k in enumerate(member_keys)]}
    ).encode()


class Store:
    def __init__(self, objects):
        self.objects = dict(objects)
        self.read = []

    def get(self, key):
        self.read.append(key)
        if key not in self.objects:
            raise KeyError(key)          # stands in for S3 NoSuchKey / AccessDenied
        return self.objects[key]


def test_returns_the_member_keys_the_map_names():
    b = batch(0, 2, "58.0")
    store = Store([map_of(b, [chunk(0), chunk(1)])])
    assert batch_cover.covered_chunk_keys(store.get, [b, chunk(0), chunk(1), chunk(2)]) == {
        chunk(0), chunk(1)}


def test_members_come_from_the_map_not_the_filename():
    # c0035_bn4 whose 4th member is c0039 because c0038 was VAD-rejected (prod 2026-09-10).
    b = batch(35, 4, "116.0")
    members = [chunk(35), chunk(36), chunk(37), chunk(39)]
    store = Store([map_of(b, members)])
    covered = batch_cover.covered_chunk_keys(store.get, [b] + members)
    assert covered == set(members)
    assert chunk(38) not in covered


def test_non_batch_keys_are_never_read():
    store = Store([])
    assert batch_cover.covered_chunk_keys(store.get, [chunk(0), chunk(1)]) == set()
    assert store.read == []


def test_a_missing_map_hides_nothing_and_does_not_stop_the_others():
    b_missing, b_ok = batch(0, 2, "58.0"), batch(2, 3, "82.0")
    store = Store([map_of(b_ok, [chunk(2), chunk(3), chunk(4)])])
    assert batch_cover.covered_chunk_keys(store.get, [b_missing, b_ok]) == {
        chunk(2), chunk(3), chunk(4)}


def test_a_malformed_map_hides_nothing():
    b1, b2, b3 = batch(0, 2, "58.0"), batch(2, 1, "30.0"), batch(3, 1, "30.0")
    store = Store([
        (b1[: -len(".wav")] + "_batch_map.json", b"not json"),
        (b2[: -len(".wav")] + "_batch_map.json", json.dumps({"members": "nope"}).encode()),
        (b3[: -len(".wav")] + "_batch_map.json", json.dumps({"members": [{"x": 1}, 7]}).encode()),
    ])
    assert batch_cover.covered_chunk_keys(store.get, [b1, b2, b3]) == set()


def test_a_batch_never_hides_a_batch():
    b1, b2 = batch(0, 2, "58.0"), batch(2, 2, "58.0")
    store = Store([map_of(b1, [chunk(0), b2])])
    assert batch_cover.covered_chunk_keys(store.get, [b1, b2]) == {chunk(0)}


def test_empty_input_reads_nothing():
    store = Store([])
    assert batch_cover.covered_chunk_keys(store.get, []) == set()
    assert store.read == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_batch_cover.py -q`
Expected: collection error / FAIL with `ModuleNotFoundError: No module named 'batch_cover'`.

- [ ] **Step 3: Write the implementation**

Create `src/batch_cover.py`:

```python
"""Which chunk wavs a batch already covers, so a listing can play each stretch of speech once.

Batching stitches several chunk wavs into one batch wav and writes it BESIDE them, with a
`*_batch_map.json` naming every member's key. A flat listing of the prefix therefore holds
the same speech twice. The map is the only authority on membership: the batch filename's
`c{first}_bn{count}` is not, because a chunk VAD rejected is bridged -- on prod 2026-09-10,
`c1035_bn4` holds 1035, 1036, 1037 and 1039.

Fail open, always. A map that is missing, forbidden or malformed hides nothing: the worst
this module may cause is a duplicate on screen, never a recording that vanished.

No boto3 and no psycopg here -- the read is injected, so the in-VPC org-api and the legacy
gateway share this without either one's client leaking into the other.
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor

import batch_stitch

logger = logging.getLogger()

# One day measured at 215 batches, behind API Gateway's 29 s limit: read maps in parallel.
MAX_MAP_READERS = 16


def _members(get_bytes, batch_key):
    map_key = batch_stitch.map_key_for_audio(batch_key)
    try:
        doc = json.loads(get_bytes(map_key))
    except Exception as exc:  # noqa: BLE001 - any failure means "hide nothing"
        logger.warning("batch_cover: map unreadable for %s (%s) -- nothing hidden",
                       batch_key.rsplit("/", 1)[-1], type(exc).__name__)
        return set()
    members = doc.get("members") if isinstance(doc, dict) else None
    if not isinstance(members, list):
        return set()
    return {m["chunk_key"] for m in members
            if isinstance(m, dict) and isinstance(m.get("chunk_key"), str)
            and m["chunk_key"] and not batch_stitch.is_batch_key(m["chunk_key"])}


def covered_chunk_keys(get_bytes, keys, max_workers=MAX_MAP_READERS):
    """Union of the chunk keys named by the maps of the batch wavs among `keys`.

    `get_bytes(key) -> bytes` may raise; a raise contributes nothing. Non-batch keys are
    never read.
    """
    batches = [k for k in keys if batch_stitch.is_batch_key(k)]
    if not batches:
        return set()
    covered = set()
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(batches)))) as pool:
        for members in pool.map(lambda k: _members(get_bytes, k), batches):
            covered |= members
    return covered
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_batch_cover.py -q`
Expected: `7 passed`.

- [ ] **Step 5: Prove the fail-open test can go red**

Temporarily change the `except` branch in `_members` to `raise`. Run the same command.
Expected: `test_a_missing_map_hides_nothing_and_does_not_stop_the_others` and `test_a_malformed_map_hides_nothing` FAIL. Restore the `return set()` branch and re-run: `7 passed`.

- [ ] **Step 6: Commit**

```bash
git add src/batch_cover.py tests/unit/test_batch_cover.py
git commit -m "Name the chunks a batch already covers, from its map

A batch wav is written beside the chunk wavs it was stitched from, so a flat listing
of audio_segments/ holds the same speech twice. The batch map is the only authority on
membership: the filename's c{first}_bn{count} is not, because a VAD-rejected chunk is
bridged (prod 2026-09-10: c1035_bn4 holds 1035, 1036, 1037, 1039). Reads are
concurrent and fail open -- an unreadable map hides nothing.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2ZDWwHWVd7mH4NNKX1kjt"
```

---

### Task 2: org-api `/api/org/audio-segments` drops covered chunks

**Files:**
- Modify: `src/lambda_org_api.py` — import block (≈ line 161) and `_read_org_audio_segments` (≈ lines 8589–8631)
- Test: `tests/unit/test_lambda_org_api.py` — add tests after `test_audio_segments_matches_chunk_session_key` (≈ line 4085)

**Interfaces:**
- Consumes: `batch_cover.covered_chunk_keys(get_bytes, keys) -> set[str]` from Task 1.
- Produces: no new names. `_read_org_audio_segments(date, folder, start_time, end_time, conn=None)` keeps its signature and return shape.

- [ ] **Step 1: Read the fixture before writing tests**

Read the `presign_wired` fixture and `_wire_one_audio_segment` (≈ line 4058) in `tests/unit/test_lambda_org_api.py`. Confirm two things and note them in your report: (a) the fake's `list_objects_response` feeds `_list_media_objects`, (b) the fake serves `get_object(Bucket=, Key=)` from `fake.objects` returning a dict with a `"Body"` whose `.read()` returns the stored bytes. If (b) is not true, extend the fake **in the fixture** (not in the production code) so `get_object` does that and raises for a missing key the way the real client raises `NoSuchKey`.

- [ ] **Step 2: Write the failing tests**

Add after `test_audio_segments_matches_chunk_session_key`:

```python
# ----------------------------------------------------------
# A batch wav is written beside the chunk wavs it was stitched from. Listing both
# plays every stretch of speech twice. A chunk is dropped only when a batch in THIS
# result names it in its map -- never by duration, never by filename arithmetic.
# ----------------------------------------------------------

_DEDUP_SID = "449c1bb3473c405985351aaa189049e6"


def _dedup_chunk(i, clock):
    return (f"ben_lin_2026-09-17_{clock}_sid{_DEDUP_SID}_c{i:04d}"
            f"_off0.0_to30.0_srcwav.wav")


def _dedup_batch(first, count, to):
    return (f"ben_lin_2026-09-17_13-05-18_sid{_DEDUP_SID}_c{first:04d}_bn{count}"
            f"_off0.0_to{to}_srcwav.wav")


def _wire_listing(fake, folder, date, filenames, maps=None):
    prefix = f"audio_segments/{folder}/{date}/"
    fake.list_objects_response = {"Contents": [{"Key": prefix + f} for f in filenames]}
    for f in filenames:
        fake.objects[prefix + f] = b""
    for batch_name, member_names in (maps or {}).items():
        map_key = prefix + batch_name[: -len(".wav")] + "_batch_map.json"
        fake.objects[map_key] = json.dumps({"schema": 1, "members": [
            {"chunk_index": n, "chunk_key": prefix + m}
            for n, m in enumerate(member_names)]}).encode()


def _dedup_get(wired, date="2026-09-17"):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "site_manager",
                                     "folder_name": "Ben_Lin_test2"})
    res = org.lambda_handler(make_event(
        "GET", "/api/org/audio-segments",
        params={"date": date, "start": "13:00:00", "end": "13:30:00"}), None)
    assert res["statusCode"] == 200
    return body_of(res)


def test_audio_segments_hide_chunks_a_listed_batch_covers(presign_wired):
    wired, fake = presign_wired
    b = _dedup_batch(0, 2, "58.0")
    c0, c1, c2 = (_dedup_chunk(0, "13-05-18"), _dedup_chunk(1, "13-05-50"),
                  _dedup_chunk(2, "13-06-18"))
    _wire_listing(fake, "Ben_Lin_test2", "2026-09-17", [b, c0, c1, c2], maps={b: [c0, c1]})
    out = _dedup_get(wired)
    assert sorted(s["filename"] for s in out["segments"]) == sorted([b, c2])
    assert out["count"] == 2


def test_audio_segments_members_come_from_the_map_not_the_filename(presign_wired):
    # prod 2026-09-10 shape: c0035_bn4 holds 35, 36, 37, 39 -- 38 was VAD-rejected (no wav).
    wired, fake = presign_wired
    b = _dedup_batch(35, 4, "116.0")
    members = [_dedup_chunk(35, "13-05-18"), _dedup_chunk(36, "13-05-48"),
               _dedup_chunk(37, "13-06-16"), _dedup_chunk(39, "13-07-12")]
    after = _dedup_chunk(40, "13-07-40")
    _wire_listing(fake, "Ben_Lin_test2", "2026-09-17", [b] + members + [after],
                  maps={b: members})
    out = _dedup_get(wired)
    assert sorted(s["filename"] for s in out["segments"]) == sorted([b, after])


def test_audio_segments_a_day_without_batches_is_unchanged(presign_wired):
    wired, fake = presign_wired
    chunks = [_dedup_chunk(0, "13-05-18"), _dedup_chunk(1, "13-05-50"),
              _dedup_chunk(2, "13-06-18")]
    _wire_listing(fake, "Ben_Lin_test2", "2026-09-17", chunks)
    out = _dedup_get(wired)
    assert out["count"] == 3


def test_audio_segments_a_batch_without_a_readable_map_hides_nothing(presign_wired):
    wired, fake = presign_wired
    b = _dedup_batch(0, 2, "58.0")
    c0, c1 = _dedup_chunk(0, "13-05-18"), _dedup_chunk(1, "13-05-50")
    _wire_listing(fake, "Ben_Lin_test2", "2026-09-17", [b, c0, c1])   # no map written
    out = _dedup_get(wired)
    assert out["count"] == 3
```

If `json` is not already imported at the top of `tests/unit/test_lambda_org_api.py`, add `import json` to its imports.

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_lambda_org_api.py -q -k "audio_segments_hide or audio_segments_members_come or audio_segments_a_day_without or audio_segments_a_batch_without"`
Expected: `test_audio_segments_hide_chunks_a_listed_batch_covers` and `test_audio_segments_members_come_from_the_map_not_the_filename` FAIL (counts too high); the other two PASS already (they pin today's behaviour).

- [ ] **Step 4: Implement**

In `src/lambda_org_api.py`, beside `import batch_stitch`, add:

```python
import batch_cover
```

Replace the body of `_read_org_audio_segments` from `start_sec, end_sec = ...` through the line `segments.sort(key=lambda seg: seg["absolute_start"])` with:

```python
    start_sec, end_sec = _org_media_window(start_time, end_time)
    prefix = f"audio_segments/{folder}/{date}/"
    kept = []
    for obj in _list_media_objects(prefix, "audio-segments"):
        key = obj["Key"]
        if not key.endswith(".wav"):
            continue
        filename = key.split("/")[-1]
        # Base time then offset, matched SEPARATELY — a chunk-session segment keeps
        # the sid/chunk tokens BETWEEN them (…_HH-MM-SS_sid{hex}_c{NNNN}_off…), so
        # anchoring the time on a trailing "_off" (the old whole-file shape) skipped
        # every chunk segment and left the web Audio tab empty. off_match below still
        # pulls the offset; they need not be adjacent.
        base_match = re.search(r"\d{4}-\d{2}-\d{2}_(\d{2})-(\d{2})-(\d{2})", filename)
        off_match = re.search(r"_off([\d.]+)_to([\d.]+)", filename)
        if not base_match or not off_match:
            continue
        h, m, sec = (int(base_match.group(1)), int(base_match.group(2)),
                     int(base_match.group(3)))
        base_sec = h * 3600 + m * 60 + sec
        abs_start = base_sec + float(off_match.group(1))
        abs_end = base_sec + float(off_match.group(2))
        if abs_end < start_sec or abs_start > end_sec:
            continue
        kept.append((key, filename, abs_start, abs_end))
    client = s3()
    # A batch wav is the same speech as the chunk wavs it was stitched from, written
    # beside them -- listing both plays every stretch twice. Drop a chunk only when a
    # batch IN THIS RESULT names it in its map: never by duration (a day before
    # batching is all 30 s chunks) and never by filename arithmetic (a VAD-rejected
    # chunk is bridged, so c1035_bn4 holds 1035, 1036, 1037, 1039). An unreadable map
    # hides nothing.
    covered = batch_cover.covered_chunk_keys(
        lambda k: client.get_object(Bucket=S3_BUCKET, Key=k)["Body"].read(),
        [k for k, _f, _s, _e in kept])
    segments = []
    for key, filename, abs_start, abs_end in kept:
        if key in covered:
            continue
        url = client.generate_presigned_url(
            "get_object", Params={"Bucket": S3_BUCKET, "Key": key},
            ExpiresIn=PRESIGNED_URL_EXPIRY)
        ah, am, asec = (int(abs_start) // 3600, (int(abs_start) % 3600) // 60,
                        int(abs_start) % 60)
        segments.append({
            "url": url, "filename": filename,
            "absolute_start": abs_start, "absolute_end": abs_end,
            "duration": round(abs_end - abs_start, 1),
            "time_label": f"{ah:02d}:{am:02d}:{asec:02d}",
        })
    segments.sort(key=lambda seg: seg["absolute_start"])
```

Leave the two lines after the sort (`_drop_deleted_media` and the `return`) unchanged.

- [ ] **Step 5: Run the new tests and the whole audio-segments block**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_lambda_org_api.py tests/unit/test_org_media_deleted.py tests/unit/test_template_org_api_media_iam.py -q -k "audio"`
Expected: all PASS, including every pre-existing `test_audio_segments_*` and `test_transcripts_and_audio_segments_select_the_same_chunks`.

- [ ] **Step 6: Prove the dedup test can go red**

Temporarily replace `if key in covered:` with `if False:`. Run the Step 3 command. Expected: the two dedup tests FAIL. Restore and re-run: PASS.

- [ ] **Step 7: Confirm the IAM grant covers the map read**

The map lives in the same `audio_segments/` prefix the route already presigns for. Run: `git grep -n "audio_segments" src/template.yaml` and read the OrgApiFunction policy lines. Record in your report the exact line granting `s3:GetObject` on `audio_segments/*` to OrgApiFunction. If no such grant exists, STOP and report BLOCKED — a missing grant would make every map read fail open and the fix would silently do nothing.

- [ ] **Step 8: Commit**

```bash
git add src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "Play each stretch of speech once in the org-api audio list

/api/org/audio-segments listed the batch wav and the chunk wavs it was stitched from
side by side, so the timeline played the same audio twice. A chunk is now dropped when
a batch in the same result names it in its map; days without batches are unchanged
and an unreadable map hides nothing.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2ZDWwHWVd7mH4NNKX1kjt"
```

---

### Task 3: legacy gateway `/api/audio-segments` drops covered chunks

The frontend (`fieldsight-ui` `scripts/api/audio.js`) calls org-api only when `timelineSource === 'aurora' && orgBaseUrl`; otherwise it calls this legacy route. Both must be fixed or the bug survives on whichever configuration a site runs.

**Files:**
- Modify: `src/lambda_fieldsight_api.py` — import block (top) and `get_audio_segments` (≈ lines 888–936)
- Test: `tests/unit/test_lambda_fieldsight_api_media_window.py`

**Interfaces:**
- Consumes: `batch_cover.covered_chunk_keys(get_bytes, keys) -> set[str]` from Task 1.
- Produces: nothing new; `get_audio_segments(params, caller)` keeps signature and response.

- [ ] **Step 1: Know the existing fake**

`tests/unit/test_lambda_fieldsight_api_media_window.py` installs `FakeS3` (≈ line 104) on `fapi.s3_client` via the `s3` fixture; `FakeS3.list_objects_v2` filters `self.keys` by prefix, `get_object` serves `BODIES[Key]` and raises `KeyError` for anything else (so a missing map already behaves like S3's NoSuchKey), and callers use `ADMIN_CALLER`, `DATE = "2026-08-09"`, `FOLDER = "Ben_UCPK2"`, `SID = "sid0e43b52d7d654101aa01ee139c79831e"` (already `sid`-prefixed, 32 hex). The test below subclasses that fake so the file's shared fixture data is untouched.

- [ ] **Step 2: Write the failing test**

Append to `tests/unit/test_lambda_fieldsight_api_media_window.py`:

```python
# ---------------------------------------------------------------
# A batch wav is written beside the chunk wavs it was stitched from; listing both
# plays the same speech twice. Members come from the batch map, never from the
# filename: prod 2026-09-10's c1035_bn4 holds 1035, 1036, 1037 and 1039.
# ---------------------------------------------------------------

class _BatchedS3(FakeS3):
    def __init__(self, keys, bodies):
        self.keys = keys
        self.bodies = bodies

    def get_object(self, Bucket=None, Key=None):
        return {"Body": io.BytesIO(self.bodies[Key])}


def _batched_listing(monkeypatch, with_map=True):
    prefix = f"audio_segments/{FOLDER}/{DATE}/"
    batch = f"ben_ucpk2_{DATE}_12-05-21_{SID}_c0035_bn4_off0.0_to116.0_srcwav.wav"
    members = [f"ben_ucpk2_{DATE}_{clock}_{SID}_c{i:04d}_off0.0_to30.0_srcwav.wav"
               for i, clock in ((35, "12-05-21"), (36, "12-05-49"), (37, "12-06-17"),
                                (39, "12-07-13"))]
    after = f"ben_ucpk2_{DATE}_12-07-41_{SID}_c0040_off0.0_to30.0_srcwav.wav"
    bodies = {}
    if with_map:
        bodies[prefix + batch[: -len(".wav")] + "_batch_map.json"] = json.dumps(
            {"schema": 1, "members": [{"chunk_key": prefix + m} for m in members]}).encode()
    monkeypatch.setattr(fapi, "s3_client",
                        _BatchedS3([prefix + n for n in [batch] + members + [after]], bodies))
    return batch, members, after


def _audio_names():
    res = fapi.get_audio_segments({"date": DATE, "user": FOLDER, "start": "", "end": ""},
                                  ADMIN_CALLER)
    assert res["statusCode"] == 200, res["body"]
    return sorted(s["filename"] for s in json.loads(res["body"])["segments"])


def test_audio_segments_hide_chunks_a_listed_batch_covers(monkeypatch):
    batch, members, after = _batched_listing(monkeypatch)
    assert _audio_names() == sorted([batch, after])


def test_audio_segments_a_batch_without_a_readable_map_hides_nothing(monkeypatch):
    batch, members, after = _batched_listing(monkeypatch, with_map=False)
    assert _audio_names() == sorted([batch] + members + [after])
```

`io` and `json` are already imported at the top of that file.

- [ ] **Step 3: Run it to verify it fails**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_lambda_fieldsight_api_media_window.py -q -k hide_chunks`
Expected: FAIL — all six filenames returned.

- [ ] **Step 4: Implement**

Add `import batch_cover` to the import block of `src/lambda_fieldsight_api.py` (after `import deletion_mirror`).

In `get_audio_segments`, change the `try:` body so it collects then filters. Replace from `segments = []` through the `except` clause with:

```python
    kept = []
    try:
        resp = s3_client.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
        for obj in resp.get('Contents', []):
            key = obj['Key']
            if not key.endswith('.wav'):
                continue
            if deleted and any(b in key for b in deleted):
                continue              # a recording the customer deleted
            filename = key.split('/')[-1]
            # Base time then offset, matched SEPARATELY -- a chunk-session segment
            # keeps sid/chunk tokens BETWEEN them (..._HH-MM-SS_sid{hex}_c{NNNN}_off...),
            # so anchoring the time on a trailing "_off" (the old whole-file shape)
            # skipped every chunk segment and left the Audio tab empty.
            base_match = re.search(r'\d{4}-\d{2}-\d{2}_(\d{2})-(\d{2})-(\d{2})', filename)
            off_match = re.search(r'_off([\d.]+)_to([\d.]+)', filename)
            if not base_match or not off_match:
                continue
            h, m, s = int(base_match.group(1)), int(base_match.group(2)), int(base_match.group(3))
            base_sec = h * 3600 + m * 60 + s
            abs_start = base_sec + float(off_match.group(1))
            abs_end = base_sec + float(off_match.group(2))
            if abs_end < start_sec or abs_start > end_sec:
                continue
            kept.append((key, filename, abs_start, abs_end))
    except Exception as e:
        logger.error(f"Error listing audio segments: {e}")
    # Same rule as org-api's _read_org_audio_segments: a chunk named in the map of a
    # batch in this result is the same speech as that batch -- drop it. Members come
    # from the map, never the filename; an unreadable map hides nothing.
    covered = batch_cover.covered_chunk_keys(
        lambda k: s3_client.get_object(Bucket=S3_BUCKET, Key=k)['Body'].read(),
        [k for k, _f, _s, _e in kept])
    segments = []
    for key, filename, abs_start, abs_end in kept:
        if key in covered:
            continue
        url = s3_client.generate_presigned_url('get_object', Params={'Bucket': S3_BUCKET, 'Key': key}, ExpiresIn=PRESIGNED_URL_EXPIRY)
        ah, am, asec = int(abs_start)//3600, (int(abs_start)%3600)//60, int(abs_start)%60
        segments.append({
            'url': url, 'filename': filename,
            'absolute_start': abs_start, 'absolute_end': abs_end,
            'duration': round(abs_end - abs_start, 1),
            'time_label': f"{ah:02d}:{am:02d}:{asec:02d}",
        })
```

Leave `segments.sort(...)` and the `return ok(...)` unchanged. Do NOT add pagination here (out of scope — see Deferred).

- [ ] **Step 5: Run the legacy media and deletion tests**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_lambda_fieldsight_api_media_window.py tests/unit/test_legacy_api_honours_deletions.py -q`
Expected: all PASS.

- [ ] **Step 6: Prove the test can go red**

Temporarily replace `if key in covered:` with `if False:`; run Step 3's command; expected FAIL. Restore; PASS.

- [ ] **Step 7: Confirm the IAM grant**

Run `git grep -n "audio_segments" src/template.yaml` and read ApiFunction's policy. Record the line granting `s3:GetObject` on `audio_segments/*` (or the bucket) to ApiFunction. If none exists, STOP and report BLOCKED.

- [ ] **Step 8: Commit**

```bash
git add src/lambda_fieldsight_api.py tests/unit/test_lambda_fieldsight_api_media_window.py
git commit -m "Play each stretch of speech once in the legacy audio list

The web app reads audio from the legacy gateway whenever the Aurora timeline source is
off, so the duplicate batch-plus-chunks listing fixed in org-api survived there. Same
rule, same module: a chunk named in a listed batch's map is dropped.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2ZDWwHWVd7mH4NNKX1kjt"
```

---

### Task 4: Full suite, push, PR, TEST verification

**Files:** none modified (unless the suite finds a regression, which is then fixed in the task that caused it).

- [ ] **Step 1: Full unit suite**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit -q`
Expected: all pass except the known pre-existing skips. Record the pass/skip counts in the report.

- [ ] **Step 2: Push and open the PR (controller decision point)**

```bash
git push -u origin fix/timeline-audio-dedup
gh pr create --base develop --title "Play each stretch of speech once in the timeline audio list" --body-file <body file>
```
Body: what was wrong (both endpoints list batch + member chunks flat), the rule (map membership, fail open, same-result only), the measured evidence (2/33 filename disagreements on prod 2026-09-10), the tests added, and the TEST verification below. End the body with:
```
🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01N2ZDWwHWVd7mH4NNKX1kjt
```
Merging into `develop` deploys TEST and is the owner's call.

- [ ] **Step 3: TEST verification after the owner merges**

On the TEST bucket, find one day with batches: `aws s3 ls s3://fieldsight-data-test-509194952652/audio_segments/ --recursive --profile fieldsight-deployer | grep _batch_map.json | head`. For that folder/date, invoke `fieldsight-test-org-api` with a synthetic API-Gateway event for `GET /api/org/audio-segments` (authorizer claims of an account that can read that folder) and assert from the response body: no returned filename is a `chunk_key` in any map of a returned batch; the count dropped by exactly the number of covered chunks versus a raw listing of that window. Record both numbers.
