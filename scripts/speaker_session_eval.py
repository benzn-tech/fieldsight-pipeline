"""Score a recorded session's turns against enrolled voiceprints — the Phase 0 reproducer.

Produced the numbers in `docs/superpowers/specs/2026-08-11-speaker-phase0-results.md`.

Turns come from the prod transcripts (chunk-relative times, Transcribe-shaped items carrying
`speaker_label`), and each turn's audio is cut from that chunk's own wav — no concatenation,
so no overlap arithmetic to get wrong.

Ground truth comes from the SCRIPT, not from hand labelling: a read dialogue is performed in
order and every line is textually distinctive, so matching a turn's words to a line names the
speaker. That is cheaper and less error-prone than a person filling in a column, and it fails
loudly (an unmatched turn is reported as unlabelled rather than guessed).

Scored on the RAW chunk audio under `users/…/audio/` — the copy normalisation never touches,
which is what the design requires.

    python scripts/speaker_session_eval.py
        --work runs/blockv                      (expects audio/, tx/, enrol/ inside)
        --scripts scripts/fixtures/2026-08-11-blockv-scripts.json

`--scripts` maps each 32-hex session id to a list of {"speaker", "line"}.
Run it as `uv run --with numpy --with torch --with speechbrain python scripts/...`.
"""
import argparse
import glob
import json
import os
import re
import sys
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import speaker_phase0 as sp  # noqa: E402

STOP = set("the a an and or but is are was were be been to of in on at it its that this "
           "i you we they he she him her them my your our their with for from as so if "
           "not no yes do does did done have has had will would can could should".split())


def toks(s):
    return [w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in STOP and len(w) > 1]


def read_wav(path):
    with wave.open(path, "rb") as w:
        sr, n = w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0, sr


def turns_from_transcript(path):
    """Consecutive same-speaker items collapsed into one turn (chunk-relative seconds)."""
    items = json.load(open(path, encoding="utf-8"))["results"]["items"]
    out = []
    for it in items:
        if it.get("type") != "pronunciation":
            continue
        spk = it.get("speaker_label") or "?"
        st, en = float(it["start_time"]), float(it["end_time"])
        word = it["alternatives"][0]["content"]
        if out and out[-1]["spk"] == spk and st - out[-1]["end"] < 1.2:
            out[-1]["end"] = en
            out[-1]["text"] += " " + word
        else:
            out.append({"spk": spk, "start": st, "end": en, "text": word})
    return out


def match_script(text, script):
    """Best script line for this turn, by token overlap. Returns (speaker, score, line_i)."""
    t = set(toks(text))
    if not t:
        return None, 0.0, -1
    best, bi, bs = None, -1, 0.0
    for i, (who, line) in enumerate(script):
        l = set(toks(line))
        if not l:
            continue
        score = len(t & l) / max(1, min(len(t), len(l)))
        if score > bs:
            best, bi, bs = who, i, score
    return best, bs, bi


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--work", required=True, help="dir holding audio/, tx/, enrol/")
    ap.add_argument("--scripts", required=True, help="json: session_id -> [{speaker, line}]")
    ap.add_argument("--min-turn-s", type=float, default=1.0)
    ap.add_argument("--match-floor", type=float, default=0.30,
                    help="token overlap below which a turn is left UNLABELLED rather than "
                         "assigned — a wrong label is worse than a missing one")
    args = ap.parse_args()
    W = args.work
    scripts = json.load(open(args.scripts, encoding="utf-8"))

    emb = sp.Embedder(cache_dir=os.path.join(W, "ecapa"))
    profiles = {}
    for p in sorted(glob.glob(os.path.join(W, "enrol", "*.wav"))):
        name = os.path.basename(p)[:-4]
        a, sr = read_wav(p)
        profiles[name] = emb.embed(a, sr)
    print(f"profiles: {', '.join(profiles)}\n")

    all_sims = {}
    for sid, script_rows in scripts.items():
        script = [(r["speaker"], r["line"]) for r in script_rows]
        print("=" * 78)
        print("session", sid[:8])
        chunks = sorted(glob.glob(os.path.join(W, "audio", f"*{sid}*.wav")))
        rows = []
        for cpath in chunks:
            m = re.search(r"_c(\d{4})\.wav$", cpath)
            ci = int(m.group(1))
            tx = glob.glob(os.path.join(W, "tx", f"*{sid}_c{ci:04d}_*.json"))
            if not tx:
                print(f"  chunk {ci}: no transcript")
                continue
            audio, sr = read_wav(cpath)
            for t in turns_from_transcript(tx[0]):
                if ci > 0 and t["start"] < 2.0:
                    continue                       # device overlap: already seen in chunk-1
                dur = t["end"] - t["start"]
                if dur < args.min_turn_s:
                    continue
                clip = audio[int(t["start"] * sr):int(t["end"] * sr)]
                if clip.size < sr:
                    continue
                v = emb.embed(clip, sr)
                scores = {n: sp.cosine(v, pv) for n, pv in profiles.items()}
                truth, conf, li = match_script(t["text"], script)
                rows.append({"chunk": ci, "spk": t["spk"], "dur": dur, "text": t["text"],
                             "truth": truth, "conf": conf, "line": li, "scores": scores})

        labelled = [r for r in rows if r["conf"] >= args.match_floor and r["truth"]]
        print(f"  {len(rows)} turns scored, {len(labelled)} confidently matched to a script line")
        print(f"  diarizer labels seen: {sorted({r['spk'] for r in rows})}")
        print()
        print(f"  {'#':>2} {'spk':5} {'s':>5} {'truth':6} " +
              " ".join(f"{n[:5]:>6}" for n in profiles) + "  pred")
        for i, r in enumerate(rows):
            pred = max(r["scores"], key=r["scores"].get)
            mark = "" if not r["truth"] else ("  OK" if pred.lower().startswith(r["truth"].lower()[:3]) else "  X")
            print(f"  {i:>2} {r['spk']:5} {r['dur']:5.1f} {str(r['truth'])[:6]:6} " +
                  " ".join(f"{r['scores'][n]:+6.3f}" for n in profiles) + f"  {pred}{mark}")
            print(f"      \"{r['text'][:88]}\"")

        for r in labelled:
            for n, s in r["scores"].items():
                key = (r["truth"], "Ben" if n.lower().startswith("ben") else n)
                all_sims.setdefault(key, []).append(s)

        hits = sum(1 for r in labelled
                   if max(r["scores"], key=r["scores"].get).lower().startswith(r["truth"].lower()[:3]))
        if labelled:
            print(f"\n  nearest-profile accuracy: {hits}/{len(labelled)} = {hits/len(labelled):.0%}")
        print()

    print("=" * 78)
    same = [s for (a, b), v in all_sims.items() if a == b for s in v]
    diff = [s for (a, b), v in all_sims.items() if a != b for s in v]
    v = sp.separability(same, diff)
    print("SEPARABILITY")
    if same:
        print(f"  same-person   n={len(same):3}  median {np.median(same):+.3f}  "
              f"range {min(same):+.3f}…{max(same):+.3f}")
    if diff:
        print(f"  cross-person  n={len(diff):3}  median {np.median(diff):+.3f}  "
              f"range {min(diff):+.3f}…{max(diff):+.3f}")
    print(f"  overlapping pairs: {v.overlap}")
    if v.best_accuracy is not None:
        print(f"  best fitted cut {v.best_threshold:+.3f} -> {v.best_accuracy:.0%}  (UPPER BOUND)")
    print(f"  verdict: {v.separable}  {v.note}")

    print("\nCOLLAPSE (the question Phase 0 exists for)")
    c = sp.collapse_report(all_sims)
    if c.collapsed is None:
        print(f"  cannot tell — {c.note}")
    elif c.collapsed:
        print("  YES — distant speakers merged:")
        for d in c.detail:
            print("   ", d)
    else:
        print("  no — every speaker matches their own profile best")

    print("\nPER-SPEAKER own-profile vs best other")
    people = sorted({a for a, _ in all_sims})
    for p in people:
        own = all_sims.get((p, p))
        if not own:
            continue
        others = {b: float(np.mean(v2)) for (a, b), v2 in all_sims.items() if a == p and b != p}
        top = max(others, key=others.get) if others else None
        print(f"  {p:6} own {np.mean(own):+.3f} (n={len(own)})" +
              (f"   closest other: {top} {others[top]:+.3f}   margin {np.mean(own)-others[top]:+.3f}"
               if top else ""))


if __name__ == "__main__":
    main()
