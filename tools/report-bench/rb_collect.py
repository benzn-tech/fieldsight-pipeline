"""Read-only collector: owner-test folders only (spec §4).

    python rb_collect.py --sessions sessions.json --out DIR [--profile NAME]
    python rb_collect.py --debug-cmd --bucket B --date D --folder F --out DIR

`sessions.json` (owner-written): [{folder, date, session_base, bucket,
role: "set"|"heldout"}, ...].

Writes only under `<out>/inputs/`:
  X/<session_base>.turns.json   the turns prod's final pass would see
                                (lambda_extract_session.gather_session_segments
                                + assemble_session_turns, the same real turn
                                assembly scripts/extraction_ab.py uses), in its
                                --turns-file shape plus source_filename / abs_*
  R/<folder>_<date>.json        build_daily_prompt's inputs for that day
                                (process_user_data -> correlate_photos_...)
  manifest.json                 {session_base: {folder, date, bucket, role,
                                n_segments, n_turns, sha256}, "R": {...}}

Guards, all before any S3 call: --out outside the repo / Dropbox; every folder
in owner_folders.json with `confirmed: true`; `_summary` always refused. The
S3 client is wrapped in ReadOnlyS3 (get / list / head / paginator only), and
the never-write prefixes are refused for any destination key. stdout carries
ids, counts and hashes only.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import rb_common  # noqa: E402

OWNER_FOLDERS = os.path.join(_HERE, "owner_folders.json")
NEVER_WRITE = ("transcripts/", "extraction_requests/", "extractions/", "reports/")
SUMMARY = "_summary"
_READ_METHODS = frozenset({"get_object", "list_objects_v2", "head_object", "get_paginator"})


def assert_never_write(key: str) -> None:
    k = (key or "").lstrip("/")
    for p in NEVER_WRITE:
        if k.startswith(p):
            raise PermissionError(f"refusing a destination under {p}")


def _refuse_summary(value):
    if isinstance(value, str) and f"/{SUMMARY}/" in "/" + value:
        raise PermissionError("the _summary report folder is never read")


class ReadOnlyS3:
    """Exposes only the read methods of a boto3 S3 client. Anything else --
    put_object, delete_object, copy_object, upload_file, ... -- raises
    PermissionError before reaching the client."""

    def __init__(self, client):
        object.__setattr__(self, "_client", client)

    def __getattr__(self, name):
        if name not in _READ_METHODS:
            raise PermissionError(f"ReadOnlyS3: {name} is not a read method")
        fn = getattr(self._client, name)

        def guarded(*args, **kwargs):
            for k in ("Key", "Prefix"):
                _refuse_summary(kwargs.get(k))
            for k in ("CopySource", "Destination", "DestinationKey"):
                if k in kwargs:
                    assert_never_write(str(kwargs[k]))
            return fn(*args, **kwargs)
        return guarded

    def __setattr__(self, name, value):
        raise PermissionError("ReadOnlyS3 is immutable")


def load_owner_folders(path=OWNER_FOLDERS):
    return rb_common.read_json(path)


def check_folder(folder: str, cfg: dict | None = None) -> None:
    if folder == SUMMARY or not folder:
        raise PermissionError("the _summary folder is never a bench input")
    cfg = load_owner_folders() if cfg is None else cfg
    if not cfg.get("confirmed"):
        raise PermissionError("owner_folders.json is not confirmed by the owner")
    if folder not in cfg.get("folders", []):
        raise PermissionError(f"{folder!r} is not an owner-test folder")


def debug_fetch_command(bucket, date, folder, out):
    """The owner's `aws s3 cp` line for one owner-folder daily_report_debug.json."""
    check_folder(folder)
    out = rb_common.check_out_dir(out)
    return (f"aws s3 cp s3://{bucket}/reports/{date}/{folder}/daily_report_debug.json "
            f"\"{os.path.join(out, 'inputs', 'R', f'{folder}_{date}.debug.json')}\"")


@contextlib.contextmanager
def _patched(module, attr, value):
    old = getattr(module, attr)
    setattr(module, attr, value)
    try:
        yield
    finally:
        setattr(module, attr, old)


def load_x(s3, bucket, folder, date, session_base):
    """(turns, n_segments) via the real turn assembly, on the read-only client."""
    rb_common.ensure_src_on_path()
    import lambda_extract_session as ex
    with _patched(ex, "_s3_client", s3):
        keys = ex.gather_session_segments(bucket, folder, date, session_base)
        if not keys:
            raise LookupError(f"no transcript segments for {folder}/{date}/{session_base}")
        turns, files, _stats = ex.assemble_session_turns(bucket, keys)
    keep = ("abs_start_str", "speaker", "text", "source_filename", "abs_start", "abs_end")
    return [{k: t[k] for k in keep if k in t} for t in turns], len(files)


def load_r(s3, bucket, folder, date):
    """build_daily_prompt's kwargs for one owner folder and day, as
    generate_daily_report assembles them (lambda_report_generator.py:1834+)."""
    rb_common.ensure_src_on_path()
    import lambda_report_generator as rg
    with _patched(rg, "s3_client", s3):
        # Meeting-consumed keys are excluded as the nightly run does; the bench
        # reads only this folder's manifest (the nightly unions every folder's).
        consumed = rg.read_meeting_manifest(s3, bucket, rg.REPORT_PREFIX, date, folder)
        data = rg.process_user_data(bucket, folder, date, exclude_keys=consumed or set())
        if not data or not data.get("transcripts"):
            raise LookupError(f"no transcripts for {folder}/{date}")
        correlated = rg.correlate_photos_with_transcripts(data["transcripts"], data["photos"])
        role, site = "", os.environ.get("SITE_NAME", "Construction Site")
        try:
            primary, _all, roles, sites = rg.get_user_site_mapping(bucket)
            role = roles.get(folder, "")
            site = rg.site_name_for(sites.get(primary.get(folder, ""), {}))
        except Exception:                                 # noqa: BLE001 - recorded
            pass
    return {"kwargs": {"transcripts_with_photos": correlated, "user_name": folder,
                       "site_name": site, "target_date": date, "role": role,
                       "total_duration": data.get("total_duration", 0.0),
                       "num_photos": len(data.get("photos") or [])},
            "n_transcripts": len(data["transcripts"])}


def collect(sessions_file, out, s3, x_loader=load_x, r_loader=load_r, with_r=True):
    out = rb_common.check_out_dir(out)
    sessions = rb_common.read_json(sessions_file)
    cfg = load_owner_folders()
    for s in sessions:
        check_folder(s["folder"], cfg)
        if s.get("role", "set") not in ("set", "heldout"):
            raise ValueError(f"role must be set|heldout: {s.get('role')!r}")
    ro = s3 if isinstance(s3, ReadOnlyS3) else ReadOnlyS3(s3)
    inputs = os.path.join(out, "inputs")
    manifest = {"R": {}}
    for s in sessions:
        turns, n_seg = x_loader(ro, s["bucket"], s["folder"], s["date"], s["session_base"])
        path = os.path.join(inputs, "X", s["session_base"] + ".turns.json")
        rb_common.write_json(path, turns)
        manifest[s["session_base"]] = {"folder": s["folder"], "date": s["date"],
                                       "bucket": s["bucket"], "role": s.get("role", "set"),
                                       "n_segments": n_seg, "n_turns": len(turns),
                                       "sha256": rb_common.sha256_file(path)}
    if with_r:
        days = sorted({(s["bucket"], s["folder"], s["date"]) for s in sessions
                       if s.get("role", "set") == "set"})
        for bucket, folder, date in days:
            r = r_loader(ro, bucket, folder, date)
            path = os.path.join(inputs, "R", f"{folder}_{date}.json")
            rb_common.write_json(path, r)
            manifest["R"][f"{folder}_{date}"] = {"folder": folder, "date": date,
                                                 "bucket": bucket,
                                                 "n_transcripts": r["n_transcripts"],
                                                 "sha256": rb_common.sha256_file(path)}
    rb_common.write_json(os.path.join(inputs, "manifest.json"), manifest)
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions")
    ap.add_argument("--out", required=True)
    ap.add_argument("--profile", help="read-only AWS profile (owner)")
    ap.add_argument("--no-r", action="store_true")
    ap.add_argument("--debug-cmd", action="store_true")
    ap.add_argument("--bucket")
    ap.add_argument("--date")
    ap.add_argument("--folder")
    a = ap.parse_args(argv)
    if a.debug_cmd:
        print(debug_fetch_command(a.bucket, a.date, a.folder, a.out))
        return 0
    rb_common.check_out_dir(a.out)
    import boto3
    client = boto3.Session(profile_name=a.profile).client("s3")
    m = collect(a.sessions, a.out, client, with_r=not a.no_r)
    for sid, e in sorted((k, v) for k, v in m.items() if k != "R"):
        print(sid, e["folder"], e["date"], e["role"], "segments=%d" % e["n_segments"],
              "turns=%d" % e["n_turns"], e["sha256"][:12])
    for k, e in sorted(m["R"].items()):
        print("R", k, "transcripts=%d" % e["n_transcripts"], e["sha256"][:12])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
