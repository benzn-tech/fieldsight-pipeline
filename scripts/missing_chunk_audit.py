"""Which recordings have holes in them?

Chunk indices are allocated in sequence by the device, so a hole in the sequence
is a chunk that existed and never reached S3. Nothing else reports this: the
chunk left no audio, no VAD sidecar and no transcript, so there is no artifact
anywhere saying the record is incomplete — the only thing that would have
created one is the chunk itself.

    python scripts/missing_chunk_audit.py --bucket fieldsight-data-509194952652
    python scripts/missing_chunk_audit.py --bucket ... --from-listing chunks.txt

**Judge absence by the index sequence, never by timestamp arithmetic.** Summing
`next_start - (start + duration)` over the prod bucket reports 64% of all audio
as lost; the two largest entries are 5,758 s and 5,470 s, which are someone
pausing and resuming an hour and a half later. A pause keeps the same session id
and continues the index sequence, so duration arithmetic cannot tell it from a
hole.

**And do not judge it from the transcript list.** A chunk VAD found silent is
dropped before transcription (`DROP_SILENT_CHUNKS`), so it has no transcript
either — inferring absence from missing transcripts reports every silent chunk
as lost. Only the audio object proves arrival.
"""
import argparse
import collections
import re
import subprocess
import sys

BYTES_PER_SEC = 32000          # PCM s16le mono @ 16 kHz
WAV_HEADER = 44
TIME_RE = re.compile(r"[0-9]{2}-[0-9]{2}-[0-9]{2}")
FULL_CHUNK_S = 30.0


def parse_line(line):
    """(folder, date, sid, index, start_seconds, duration_seconds) or None.

    Accepts an `aws s3 ls --recursive` line, or any "<size> <key>" pair.
    """
    parts = line.split()
    if len(parts) < 2:
        return None
    key = parts[-1]
    try:
        size = int(parts[-2])
    except ValueError:
        return None
    if "/audio/" not in key or not key.endswith(".wav") or "_sid" not in key:
        return None
    name = key.rsplit("/", 1)[-1][:-4]
    try:
        sid = name.split("_sid")[1].split("_c")[0]
        idx = int(name.split("_c")[-1])
    except (IndexError, ValueError):
        return None
    # Find the HH-MM-SS token by shape, not by position. Device prefixes differ
    # in how many underscore-separated tokens they carry — `ben_ucpk_...` has two
    # before the date, `AUD_...` has one — and indexing a fixed slot silently
    # dropped an entire real session (sidc30fb98d, 4 chunks) from the census.
    stamp = next((t for t in name.split("_") if TIME_RE.fullmatch(t)), None)
    if stamp is None:
        return None
    h, m, s = (int(x) for x in stamp.split("-"))
    segs = key.split("/")
    if len(segs) < 4:
        return None
    return (segs[1], segs[3], sid, idx, h * 3600 + m * 60 + s,
            (size - WAV_HEADER) / BYTES_PER_SEC)


def group_sessions(rows):
    out = collections.defaultdict(dict)
    for folder, date, sid, idx, start, dur in rows:
        out[(folder, date, sid)][idx] = (start, dur)
    return out


def holes(chunks):
    """Indices between min and max with no object — chunks that never arrived."""
    if not chunks:
        return []
    present = sorted(chunks)
    return [i for i in range(present[0], present[-1] + 1) if i not in chunks]


def short_mid_session(chunks, full=FULL_CHUNK_S, tolerance=1.0):
    """Chunks materially shorter than a full one that are NOT the last.

    A short final chunk is normal — recording stopped mid-chunk — and warning
    about it would bury the case that matters. A short chunk in the middle is a
    recorder restart, and the audio between it and the next chunk is gone.
    """
    if not chunks:
        return []
    last = max(chunks)
    return [(i, round(d, 1)) for i, (_s, d) in sorted(chunks.items())
            if i != last and d < full - tolerance]


def looks_like_pause(chunks, index, min_gap_s=120.0):
    """Is the time jump after `index` a person pausing rather than a fault?

    Indices stay contiguous across a pause — that is exactly why absence has to
    be judged on the sequence. A jump of minutes with no missing index is
    someone stopping and resuming, and must never be counted as loss.
    """
    if index not in chunks or (index + 1) not in chunks:
        return False
    start, dur = chunks[index]
    return (chunks[index + 1][0] - (start + dur)) >= min_gap_s


def audit(sessions):
    report = []
    for key, chunks in sorted(sessions.items()):
        missing = holes(chunks)
        shorts = short_mid_session(chunks)
        pauses = [i for i in sorted(chunks) if looks_like_pause(chunks, i)]
        if missing or shorts:
            report.append({"session": key, "present": len(chunks),
                           "missing": missing, "short_mid": shorts,
                           "pauses": pauses})
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket")
    ap.add_argument("--region", default="ap-southeast-2")
    ap.add_argument("--from-listing", help="a saved `aws s3 ls --recursive` file")
    args = ap.parse_args()

    if args.from_listing:
        lines = open(args.from_listing, encoding="utf-8").read().splitlines()
    elif args.bucket:
        lines = subprocess.run(
            ["aws", "s3", "ls", f"s3://{args.bucket}/users/", "--recursive",
             "--region", args.region],
            capture_output=True, text=True, check=True).stdout.splitlines()
    else:
        ap.error("need --bucket or --from-listing")

    rows = [r for r in (parse_line(l) for l in lines) if r]
    sessions = group_sessions(rows)
    total = sum(len(c) for c in sessions.values())
    hours = sum(d for c in sessions.values() for _s, d in c.values()) / 3600
    print(f"{len(sessions)} sessions, {total} chunks, {hours:.2f} h of audio\n")

    findings = audit(sessions)
    lost_chunks = sum(len(f["missing"]) for f in findings)
    if not findings:
        print("no holes and no mid-session short chunks — every record is complete")
        return 0

    for f in findings:
        folder, date, sid = f["session"]
        print(f"{folder}  {date}  sid{sid[:8]}  ({f['present']} chunks present)")
        if f["missing"]:
            mins = len(f["missing"]) * FULL_CHUNK_S / 60
            print(f"   NEVER ARRIVED: {len(f['missing'])} chunks (~{mins:.1f} min) "
                  f"{f['missing'][:12]}")
        for i, d in f["short_mid"]:
            print(f"   short mid-session: c{i:04d} holds {d}s — recorder restart")
        if f["pauses"]:
            print(f"   (pauses, not loss: after c{', c'.join(f'{i:04d}' for i in f['pauses'])})")
        print()

    print(f"=== {lost_chunks} chunks never arrived, ~{lost_chunks*FULL_CHUNK_S/60:.1f} min, "
          f"{lost_chunks/total*100:.2f}% of all chunks")
    print("    Every one is silent: no audio, no sidecar, no transcript, and no field")
    print("    anywhere records that the report is missing what was said in them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
