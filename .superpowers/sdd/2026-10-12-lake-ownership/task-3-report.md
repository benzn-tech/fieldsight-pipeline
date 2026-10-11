# Task 3 report

Status: DONE

## Public signatures (src/lake_put.py)
- lake_put(bucket, key, body, *, owner_hint=None, client=None, manifest=True, **put_kwargs) -> put_object response (unchanged)
- lake_upload_file(bucket, key, filename, *, owner_hint=None, client=None, manifest=True, ExtraArgs=None) -> upload_file's return (None)
- manifest_key(now=None, uid=None) -> "lake_manifest/{yyyy}/{mm}/{dd}/{uuid}.jsonl" (UTC date)
- MANIFEST_PREFIX = "lake_manifest/"

owner_hint: dict, any of company_id, site_id, user_id, folder, session_base (falsy values dropped). It lands only in the manifest line.
Manifest line: {"v":1,"bucket","key","size","t":<utc iso>,"hint":{non-null, str()}}; size = len for bytes/bytearray/str, os.path.getsize for upload_file, else null. ContentType application/x-ndjson.

## Contract (per C4/C6)
- No Tagging anywhere, no put_object_tagging; LAKE_TAGS dropped. All other kwargs and ExtraArgs pass through unchanged.
- Object put first; raises as before with no manifest. Manifest failure -> WARNING "lake_put: manifest write failed for <key>", never raises.
- Manifest only if lake_keys.parse(key) is None (unknown shape, drain reports it) or scope != "global".
- Kill switch: env LAKE_MANIFEST=off, read per call. manifest=False also skips.
- Manifest key never matches the 14+ prefix/suffix filters parsed from scripts/wire-s3-events.sh nor the transcripts/ EventBridge prefix (test parses the script).

## Red-proof (each mutation reverted)
manifest-before-put: 3 fail; add Tagging: 1; raise on manifest error: 1; kill switch ignored: 1; prefix under transcripts/: 8; manifest global keys: 3; drop kwargs: 1.
