import json,os,glob,re
W=os.environ.get("ASR_EVAL_WORK", os.path.dirname(os.path.abspath(__file__)))
rows=[]
for p in sorted(glob.glob(os.path.join(W,"vadmeta","*.json"))):
    j=json.load(open(p))
    rows.append(dict(ci=j.get("chunk_index"), start=j.get("chunk_start"),
        total=j.get("total_duration_sec") or 0, speech=j.get("speech_duration_sec") or 0,
        ratio=j.get("speech_ratio"), mode=j.get("emit_mode"),
        nseg=len(j.get("segments") or []),
        emitted=sum(s.get("duration",0) for s in (j.get("segments") or [])),
        db_before=j.get("loudness_dbfs_before"), db_after=j.get("loudness_dbfs_after")))
rows.sort(key=lambda r:(r["ci"] if r["ci"] is not None else -1))
T=sum(r["total"] for r in rows); S=sum(r["speech"] for r in rows); E=sum(r["emitted"] for r in rows)
print(f"chunks with metadata: {len(rows)}   chunk_index range {rows[0]['ci']}..{rows[-1]['ci']}")
print(f"raw recorded audio     : {T:8.1f}s  ({T/60:.1f} min)")
print(f"VAD-detected speech    : {S:8.1f}s  ({S/T*100:.1f}% of raw)")
print(f"actually sent to ASR    : {E:8.1f}s  ({E/T*100:.1f}% of raw)   <- emit_mode whole_chunk")
print(f"discarded by VAD        : {T-E:8.1f}s  ({(T-E)/T*100:.1f}%)")
print()
zero=[r for r in rows if r["nseg"]==0]
print(f"chunks dropped entirely (0 segments): {len(zero)} -> {[ (r['ci'],r['start'],r['ratio']) for r in zero]}")
part=[r for r in rows if 0<r["ratio"]<1.0]
print(f"chunks with speech_ratio < 1.0: {len(part)}")
for r in part[:20]: print(f"   c{r['ci']:04d} {r['start'][-8:]} ratio={r['ratio']:.2f} speech={r['speech']}s emitted={r['emitted']}s segs={r['nseg']}")
db=[r["db_before"] for r in rows if r["db_before"] is not None]
db.sort()
print(f"\nloudness before normalisation: median {db[len(db)//2]:.1f} dBFS, min {db[0]:.1f}, max {db[-1]:.1f}")
