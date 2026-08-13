"""Turn a run of consecutive chunks into one batch object, wherever the run is noticed.

Two functions notice: `lambda_transcribe`, when the arrival of a chunk completes a run, and
`lambda_finalize_claim`, when a session is about to be finalized and its last 1–3 chunks
will never see a fourth. Both do exactly the same work, so it lives here once — with the S3
client injected rather than imported, because the sweep runs every minute and must not pay
for a module-level `boto3.client('transcribe')` it has no use for.

Spec:  docs/superpowers/specs/2026-08-11-batched-transcription.md
Plan:  docs/superpowers/plans/2026-08-11-batched-transcription.md (phases 3–4)
"""
from __future__ import annotations

import io
import json
import logging
import wave

import batch_ledger
import batch_stitch
import transcript_utils

logger = logging.getLogger()


def _wav_pcm(data):
    """(pcm_bytes, sample_rate) from a 16-bit mono WAV payload."""
    with wave.open(io.BytesIO(data), 'rb') as w:
        return w.readframes(w.getnframes()), w.getframerate()


def _wav_bytes(pcm, rate):
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def raw_key_for(unit_key):
    """The device's own upload for a VAD unit — `users/{user}/audio/{date}/{stem}.wav`.

    The overlap must be measured on THIS, not on the unit: normalisation applies
    time-varying gain per chunk, so identical source samples come out as different bytes
    and the measurement would find nothing — which, with the "measured zero means do not
    trim" rule, silently switches all trimming off.
    """
    parts = unit_key.split('/')
    if len(parts) < 4:
        return None
    stem = parts[-1].split('_off')[0]
    return f"users/{parts[1]}/audio/{parts[2]}/{stem}.wav"


def measure_trim(s3, bucket, prev_unit_key, unit_key):
    """(seconds, measured) for the head of `unit_key`. Never raises.

    A raw upload that is missing or unreadable — and 0.9% of them have been — means the
    seam is unmeasured, not that there is no overlap. Unmeasured keeps the audio: the
    duplicate survives into the transcript where `_dedup_turn_boundaries` still removes it,
    whereas trimming a guessed length deletes speech with no trace.
    """
    try:
        prev_raw, nxt_raw = raw_key_for(prev_unit_key), raw_key_for(unit_key)
        if not prev_raw or not nxt_raw:
            return 0.0, False
        prev_pcm, rate = _wav_pcm(s3.get_object(Bucket=bucket, Key=prev_raw)['Body'].read())
        nxt_pcm, _ = _wav_pcm(s3.get_object(Bucket=bucket, Key=nxt_raw)['Body'].read())
    except Exception:
        logger.warning("batch: could not read the raw uploads for the %s seam — "
                       "keeping the audio, seam recorded as unmeasured", unit_key)
        return 0.0, False
    secs = batch_stitch.measure_overlap(prev_pcm, nxt_pcm, rate)
    return secs, secs > 0.0


def bypass_singleton(s3, bucket, session_id, index, unit_key, now, table):
    """A window with one member is not a batch — hand the chunk back to the ordinary path.

    A one-member batch saves no request and shares no speaker namespace, which is the only
    benefit batching has ever been measured to deliver. What it costs is the whole grace
    wait, a map, a seal record and a `_bn1` object. 10% of the batches in the lake are this.

    The re-drive is a copy of the object onto its own key: the fresh S3 event re-enters
    `lambda_transcribe` through the path it would have taken with batching off. Calling the
    transcriber directly would pull its client into the sweep's import graph, and the sweep
    runs every minute.

    Consumed is marked FIRST. If the copy then fails the chunk is skipped once; the other
    order would let every later sweep re-plan it, re-copy it, and pay again.
    """
    batch_ledger.mark_members_consumed(table, session_id, [index], index)
    # `MetadataDirective=REPLACE` is not optional: S3 refuses a copy of an object onto its
    # own key when nothing about it changes ("this copy request is illegal because it is
    # trying to copy an object to itself"). REPLACE also drops the existing metadata, so
    # the content type is restated rather than inherited.
    s3.copy_object(Bucket=bucket, Key=unit_key,
                   CopySource={'Bucket': bucket, 'Key': unit_key},
                   MetadataDirective='REPLACE', ContentType='audio/wav')
    batch_ledger.mark_bypassed(table, session_id, index, now)
    logger.info("batch: window of one — chunk %s re-driven per-chunk, no batch written",
                index)
    return None


def seal_batch(s3, bucket, session_id, run, by_index, now, table, sealed_by='arrival'):
    """Write one batch's map and audio, or do nothing if another worker owns it.

    The WAV is written LAST: its S3 event is what starts the transcription, so a crash
    between the two must never leave a batch whose map is missing. Returns the batch key,
    or None if the claim was lost or the window held a single member.
    """
    if batch_ledger.claim_seal(table, session_id, run[0], run, now) is None:
        return None

    if len(run) == 1:
        return bypass_singleton(s3, bucket, session_id, run[0],
                                by_index[run[0]]['chunk_key'], now, table)

    payloads, members, trims = [], [], []
    for pos, idx in enumerate(run):
        unit_key = by_index[idx]['chunk_key']
        pcm, rate = _wav_pcm(s3.get_object(Bucket=bucket, Key=unit_key)['Body'].read())
        if pos == 0:
            trim, measured, seam = 0.0, True, 'first'   # nothing before it to repeat
        elif idx != run[pos - 1] + 1:
            # A window may now bridge a chunk that VAD dropped. Those two were never
            # adjacent in the ring buffer, so they share no samples and the overlap is
            # genuinely zero. Measuring anyway would spend two S3 reads to find nothing and
            # then record `trim_measured: false` — and a wall of `false` is the alarm for
            # the byte comparison being broken, which an expected zero must not set off.
            trim, measured, seam = 0.0, True, 'gap'
        else:
            trim, measured = measure_trim(s3, bucket,
                                          by_index[run[pos - 1]]['chunk_key'], unit_key)
            seam = 'adjacent'
        base = transcript_utils.extract_base_time_from_filename(unit_key)
        kept = (len(pcm) - int(round(trim * rate)) * 2) / (rate * 2)
        members.append(batch_stitch.member(
            idx, unit_key, base.isoformat() if base else '',
            trimmed_head_sec=trim, kept_duration_sec=kept, trim_measured=measured,
            seam=seam))
        payloads.append((pcm, rate))
        trims.append(trim)

    audio, rate = batch_stitch.concat_wavs(payloads, trims)
    prefix, filename = by_index[run[0]]['chunk_key'].rsplit('/', 1)
    stem = filename.split('_off')[0]
    batch_name = batch_stitch.build_batch_name(
        stem, len(run), members[0]['trimmed_head_sec'], len(audio) / (rate * 2))
    batch_key = f"{prefix}/{batch_name}"
    doc = batch_stitch.build_map(session_id, members, sealed_by=sealed_by)

    s3.put_object(Bucket=bucket,
                  Key=batch_stitch.map_key_for_audio(f"{prefix}/{batch_name}"),
                  Body=json.dumps(doc), ContentType='application/json')
    s3.put_object(Bucket=bucket, Key=batch_key, Body=_wav_bytes(audio, rate),
                  ContentType='audio/wav')
    batch_ledger.mark_sealed(table, session_id, run[0], now)
    logger.info("batch: sealed %s from chunks %s (%s)", batch_key, run, sealed_by)
    return batch_key


def seal_ready_runs(s3, bucket, session_id, table, now, deadline_sec, max_chunks,
                    sealed_by='arrival'):
    """Seal every run of this session that is ready. Returns the batch keys written."""
    rows = batch_ledger.list_members(table, session_id)
    if not rows:
        return []
    by_index = {int(r['chunk_index']): r for r in rows}
    out = []
    for run in batch_ledger.pending_runs(rows, now, deadline_sec, max_chunks):
        key = seal_batch(s3, bucket, session_id, run, by_index, now, table,
                         sealed_by=sealed_by)
        if key:
            out.append(key)
    return out
