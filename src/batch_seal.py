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


def raw_window_for_member(unit_key, start_in_unit, end_in_unit):
    """A window inside a batch member, expressed in the device's own upload.

    A batch map's `chunk_key` is the VAD unit under `audio_segments/`, not the raw chunk —
    found by invoking the deployed function, after unit tests with fabricated maps had
    agreed with the assumption instead of with what the seal writes.

    Two conversions, and the second is the one that disappears quietly: the unit begins
    `_off{T}` into its chunk, so a position inside the unit is not a position inside the
    chunk. Every member in test currently carries `off0.0` because whole-chunk transcription
    is on, which is exactly what would let the missing term go unnoticed until it was turned
    off.

    Raises rather than passing the key through: reading the normalised copy gives plausible
    numbers, not an error, and none of the voiceprint thresholds hold on it.
    """
    raw = raw_key_for(unit_key)
    if not raw:
        raise ValueError(f"{unit_key!r} is not a VAD unit key; cannot reach the raw upload")
    offsets = transcript_utils.extract_vad_offsets_from_filename(unit_key.split('/')[-1])
    unit_start = float((offsets or [None])[0] or 0.0)
    return raw, start_in_unit + unit_start, end_in_unit + unit_start


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

    Three writes, and the order of all three is load-bearing:

    1. `bypassed` is recorded BEFORE the copy, because the copy's event can be delivered
       before the next line runs. An event arriving while the record still said `sealing`
       would fall through to batching, find the member unplannable, and report
       `batched_pending` for audio nothing would ever transcribe.
    2. the copy.
    3. consumed LAST. A failed copy then leaves the member unconsumed, so the planner
       proposes it again and `claim_seal` retakes the stale `bypassed` record after its
       retry window. Marking consumed first — which this did until the review — made a
       failed copy permanent: never re-planned, never re-copied, never transcribed, and the
       comment here claimed it was "skipped once".
    """
    batch_ledger.mark_bypassed(table, session_id, index, now)
    # `MetadataDirective=REPLACE` is not optional: S3 refuses a copy of an object onto its
    # own key when nothing about it changes ("this copy request is illegal because it is
    # trying to copy an object to itself"). REPLACE also drops the existing metadata, so
    # the content type is restated rather than inherited.
    s3.copy_object(Bucket=bucket, Key=unit_key,
                   CopySource={'Bucket': bucket, 'Key': unit_key},
                   MetadataDirective='REPLACE', ContentType='audio/wav')
    batch_ledger.mark_members_consumed(table, session_id, [index], index)
    logger.info("batch: window of one — chunk %s re-driven per-chunk, no batch written",
                index)
    return None


def seal_batch(s3, bucket, session_id, run, by_index, now, table, sealed_by='arrival',
               claim_key=None):
    """Write one batch's map and audio, or do nothing if another worker owns it.

    The WAV is written LAST: its S3 event is what starts the transcription, so a crash
    between the two must never leave a batch whose map is missing. Returns the batch key,
    or None if the claim was lost or the window held a single member.
    """
    # The claim is keyed on the BUCKET, not on the run's first index. A first-index key can
    # only exclude a worker that computed the same anchor, and under concurrent arrival two
    # workers compute different anchors over the same chunks — 123 batches for 153 chunks on
    # a real replay. `claim_key=None` keeps the old shape for direct callers and tests.
    key = run[0] if claim_key is None else claim_key
    if batch_ledger.claim_seal(table, session_id, key, run, now) is None:
        return None
    try:
        return _seal_claimed(s3, bucket, session_id, run, by_index, now, table,
                             sealed_by, key)
    except Exception:
        # Hand the claim back. Holding it after a failure does not protect anything -- no
        # artifact was written -- and it silences every retry for SEAL_RETRY_SECONDS, which
        # for a session's tail means forever: the close sweep visits a session once.
        batch_ledger.release_claim(table, session_id, key, now)
        raise


def _seal_claimed(s3, bucket, session_id, run, by_index, now, table, sealed_by,
                  claim_key):
    """The work, once this worker owns the claim."""
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
    # AFTER both artifacts, BEFORE mark_sealed. This call was missing until the review, and
    # its absence made the whole consumed-member mechanism decorative on the multi-member
    # path -- the planner kept re-proposing sealed windows, with two live consequences: a
    # late EARLIER chunk shifted the first index, claimed a key nobody held and billed the
    # window twice; a late INTERIOR chunk re-planned a window whose key IS held, lost the
    # claim, and was then never transcribed by anything, including the close sweep.
    #
    # Placed after the WAV so a crash before the artifacts exist leaves the members
    # unconsumed and the stale `sealing` claim retakeable -- the re-drive window survives.
    batch_ledger.mark_members_consumed(table, session_id, run, run[0])
    batch_ledger.mark_sealed(table, session_id, claim_key if claim_key is not None else run[0], now)
    logger.info("batch: sealed %s from chunks %s (%s), members consumed",
                batch_key, run, sealed_by)
    return batch_key


def seal_ready_runs(s3, bucket, session_id, table, now, deadline_sec, max_chunks,
                    sealed_by='arrival', window_sec=120.0):
    """Seal every window of this session that is ready. Returns the batch keys written.

    `deadline_sec` is the grace a window that could still grow waits for; pass 0 at session
    close, where nothing more can arrive and waiting is the failure this exists to prevent.

    A window that seals with one member returns no key — it was handed back to the
    per-chunk path — so the returned list is batches written, not windows sealed.
    """
    rows = batch_ledger.list_members(table, session_id)
    if not rows:
        return []
    by_index = {int(r['chunk_index']): r for r in rows}
    plan = batch_ledger.pending_buckets(rows, now, deadline_sec, window_sec=window_sec,
                                        cap=max_chunks, table=table, session_id=session_id)
    out = []
    for bucket_id, run in plan['ready']:
        key = seal_batch(s3, bucket, session_id, run, by_index, now, table,
                         sealed_by=sealed_by, claim_key=bucket_id)
        if key:
            out.append(key)
    # A member whose bucket sealed without it would be re-planned into that bucket forever
    # and refused every time, because a sealed claim is never re-driven. It goes down the
    # per-chunk path instead — the same mechanism a window of one uses.
    for idx in plan['stragglers']:
        row = by_index.get(idx)
        if row is None:
            continue
        bypass_singleton(s3, bucket, session_id, idx, row['chunk_key'], now, table)
    return out
