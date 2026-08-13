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
import speaker_phase0 as sp
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import voiceprint_utils as vp  # noqa: E402  # noqa: E402

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


def _same(a, b) -> bool:
    """Whether two person keys name the same person.

    Case-insensitive exact match, NOT the three-letter prefix this script used until
    2026-08-13. That prefix silently scored a distractor called `benny` as the wearer, and
    a distractor run is exactly when it would have mattered.
    """
    return bool(a) and bool(b) and str(a).strip().lower() == str(b).strip().lower()


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


class OnnxEmbedder:
    """The exported model, run the way the Lambda runs it.

    Measuring with speechbrain and shipping onnxruntime would leave the gap between them
    unmeasured; the parity test bounds it at cosine 0.999, but a threshold frozen from one
    engine and applied by the other is a threshold nobody checked.
    """

    def __init__(self, path):
        import onnxruntime as ort
        self.sess = ort.InferenceSession(path, providers=["CPUExecutionProvider"])

    def embed(self, audio, sr):
        out = self.sess.run(None, {"wav": np.asarray(audio, dtype=np.float32)[None, :],
                                   "wav_lens": np.array([1.0], dtype=np.float32)})
        return np.asarray(out[0]).ravel()


def agglomerate(vectors, tau):
    """Complete-linkage agglomerative clustering on cosine distance. Returns labels.

    Complete linkage, not single: a cluster is only merged when EVERY pair across it is
    within tau, so "all members within tau" holds by construction. Single linkage would let
    a chain of near-neighbours join two people who are nothing alike, which for naming is
    the expensive direction.

    Pure numpy on purpose — voiceprint_utils ships to a Lambda whose layer has no scipy or
    sklearn, and an implementation measured here that cannot run there is not a measurement
    of anything.
    """
    n = len(vectors)
    if n == 0:
        return []
    d = np.zeros((n, n))
    for i in range(n):
        for j in range(i + 1, n):
            d[i][j] = d[j][i] = 1.0 - float(vp.cosine(vectors[i], vectors[j]))
    clusters = [[i] for i in range(n)]
    while len(clusters) > 1:
        best, bi, bj = None, -1, -1
        for a in range(len(clusters)):
            for b in range(a + 1, len(clusters)):
                worst = max(d[x][y] for x in clusters[a] for y in clusters[b])
                if best is None or worst < best:
                    best, bi, bj = worst, a, b
        if best is None or best > tau:
            break
        clusters[bi] = clusters[bi] + clusters[bj]
        del clusters[bj]
    labels = [0] * n
    for k, c in enumerate(clusters):
        for i in c:
            labels[i] = k
    return labels


def report_clustering(rows, taus=(0.30, 0.50, 0.70, 0.80, 0.82, 0.84, 0.85, 0.86,
                                  0.88, 0.90, 0.95)):
    """Gate A. Does any threshold separate the people we know were in the room?

    Purity alone is not the test: one cluster per turn is perfectly pure and useless, so
    cluster count is reported beside it. What we need is k == the number of real speakers
    AND high purity at the same tau.
    """
    labelled = [r for r in rows if r.get("truth") and r.get("vec") is not None]
    if len(labelled) < 2:
        print("  clustering: too few labelled turns to say anything")
        return
    vecs = [r["vec"] for r in labelled]
    truth = [r["truth"] for r in labelled]
    people = sorted(set(truth))
    print(f"\n  --- Gate A: {len(labelled)} labelled turns, {len(people)} real speakers "
          f"{people} ---")

    # The distribution nobody has measured: turn-vs-turn, not turn-vs-profile.
    same, diff = [], []
    for i in range(len(vecs)):
        for j in range(i + 1, len(vecs)):
            dist = 1.0 - float(vp.cosine(vecs[i], vecs[j]))
            (same if truth[i] == truth[j] else diff).append(dist)
    if same and diff:
        print(f"  turn-vs-turn cosine DISTANCE  same speaker: "
              f"min {min(same):.3f} med {sorted(same)[len(same)//2]:.3f} max {max(same):.3f}")
        print(f"                                 different:    "
              f"min {min(diff):.3f} med {sorted(diff)[len(diff)//2]:.3f} max {max(diff):.3f}")
        # NOT the test. max(same) < min(diff) is sufficient for a threshold to exist, not
        # necessary: complete linkage does not need every same-speaker pair closer than
        # every different-speaker pair, only a merge ORDER that never crosses people. The
        # tau sweep below is the real answer; this line is context for how hard it is.
        print(f"  pairwise bands {'do not overlap' if max(same) < min(diff) else 'OVERLAP'} "
              f"(by {max(0.0, max(same) - min(diff)):.3f}) — sufficient, not necessary")

    print(f"  {'tau':>5} {'k':>3} {'purity':>7}  {'singletons':>10}")
    for tau in taus:
        labels = agglomerate(vecs, tau)
        k = len(set(labels))
        hits = 0
        for c in set(labels):
            members = [truth[i] for i, l in enumerate(labels) if l == c]
            hits += max(members.count(x) for x in set(members))
        singles = sum(1 for c in set(labels) if list(labels).count(c) < 2)
        flag = "  <-- k == real speakers" if k == len(people) else ""
        print(f"  {tau:5.2f} {k:>3} {hits/len(labels):>6.0%}  {singles:>10}{flag}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--work", required=True, help="dir holding audio/, tx/, enrol/")
    ap.add_argument("--scripts", required=True, help="json: session_id -> [{speaker, line}]")
    ap.add_argument("--min-turn-s", type=float, default=1.0)
    ap.add_argument("--match-floor", type=float, default=0.30,
                    help="token overlap below which a turn is left UNLABELLED rather than "
                         "assigned — a wrong label is worse than a missing one")
    ap.add_argument("--person-map", default=None,
                    help="json: profile stem -> person key. Two enrolments of one person "
                         "(Ben has English and Chinese) MUST share a key, or they become "
                         "each other's runner-up and the margin never clears. Absent, "
                         "every profile is its own person.")
    ap.add_argument("--distractors", default=None,
                    help="dir of extra enrolment wavs, treated as people who are NOT in "
                         "the room. Grows the pool without touching the ground truth.")
    ap.add_argument("--onnx", default=None,
                    help="path to the exported ECAPA ONNX. Uses onnxruntime instead of "
                         "speechbrain — the SAME engine the Lambda runs, so a measurement "
                         "here is a measurement of production rather than of its ancestor. "
                         "Also avoids a torch install.")
    ap.add_argument("--cluster", action="store_true",
                    help="Gate A: cluster each session's turns by voice and report whether "
                         "any merge threshold separates the known speakers. Needs no "
                         "enrolments — the whole point is that it uses no stored profiles.")
    args = ap.parse_args()
    W = args.work
    scripts = json.load(open(args.scripts, encoding="utf-8"))

    emb = OnnxEmbedder(args.onnx) if args.onnx else sp.Embedder(cache_dir=os.path.join(W, "ecapa"))
    profiles = {}
    for p in sorted(glob.glob(os.path.join(W, "enrol", "*.wav"))):
        name = os.path.basename(p)[:-4]
        a, sr = read_wav(p)
        profiles[name] = emb.embed(a, sr)
    n_real = len(profiles)
    if args.distractors:
        for p in sorted(glob.glob(os.path.join(args.distractors, "*.wav"))):
            name = "distractor__" + os.path.basename(p)[:-4]
            a, sr = read_wav(p)
            profiles[name] = emb.embed(a, sr)

    # profile stem -> person. Ben's two enrolments are one person; without this they are
    # each other's runner-up at ~0.08 and `decide_name` can never confirm him.
    person_of = json.load(open(args.person_map, encoding="utf-8")) if args.person_map else {}
    def person(profile_name):
        return person_of.get(profile_name, profile_name)

    print(f"profiles: {n_real} real + {len(profiles) - n_real} distractors "
          f"= {len(profiles)} in the pool, {len({person(n) for n in profiles})} people\n")

    all_sims = {}
    three = {"confirmed": 0, "tentative": 0, "unknown": 0}
    wrong_confident = []
    floor_eligible_same = []
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
                             "truth": truth, "conf": conf, "line": li, "scores": scores,
                             "vec": v})

        labelled = [r for r in rows if r["conf"] >= args.match_floor and r["truth"]]
        print(f"  {len(rows)} turns scored, {len(labelled)} confidently matched to a script line")
        print(f"  diarizer labels seen: {sorted({r['spk'] for r in rows})}")
        if args.cluster:
            report_clustering(labelled)
        if not profiles:
            # Gate A uses no stored profiles at all — that is the whole claim it tests —
            # so everything below, which is about matching against them, has nothing to say.
            continue
        print()
        print(f"  {'#':>2} {'spk':5} {'s':>5} {'truth':6} " +
              " ".join(f"{n[:5]:>6}" for n in profiles) + "  pred")
        for i, r in enumerate(rows):
            pred = person(max(r["scores"], key=r["scores"].get))
            mark = "" if not r["truth"] else ("  OK" if _same(pred, r["truth"]) else "  X")
            print(f"  {i:>2} {r['spk']:5} {r['dur']:5.1f} {str(r['truth'])[:6]:6} " +
                  " ".join(f"{r['scores'][n]:+6.3f}" for n in profiles) + f"  {pred}{mark}")
            print(f"      \"{r['text'][:88]}\"")

        for r in labelled:
            for n, s in r["scores"].items():
                all_sims.setdefault((r["truth"], person(n)), []).append(s)

        hits = sum(1 for r in labelled
                   if _same(person(max(r["scores"], key=r["scores"].get)), r["truth"]))
        if labelled:
            print(f"\n  nearest-profile accuracy: {hits}/{len(labelled)} = {hits/len(labelled):.0%}")

        # What the shipped rule would actually answer. Nearest-profile and `confirmed` are
        # different questions: aggregation first (two enrolments of one person are one
        # candidate), then the margin, which refuses rather than guesses.
        for r in labelled:
            agg = vp.aggregate_scores(
                [{"person_key": person(n), "score": s} for n, s in r["scores"].items()])
            d = vp.decide_name(agg, duration_s=r["dur"])
            r["decision"] = d
            three[d.status] += 1
            if d.status == "confirmed" and not _same(d.name, r["truth"]):
                wrong_confident.append((sid[:8], r["truth"], d.name, round(d.margin or 0, 3)))
            if _same(person(max(r["scores"], key=r["scores"].get)), r["truth"]) \
                    and r["dur"] >= vp.DEFAULT_MIN_TURN_S:
                # the number the margin actually has to clear, floor-eligible only
                floor_eligible_same.append(max(
                    s for n, s in r["scores"].items() if _same(person(n), r["truth"])))
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

    # ---- what the shipped rule would answer, which is not nearest-profile ----
    total = sum(three.values())
    print("\nDECISION (decide_name, after per-person aggregation)")
    if total:
        for k in ("confirmed", "tentative", "unknown"):
            print(f"  {k:10} {three[k]:3}  {three[k]/total:5.0%}")
    print(f"  pool: {len(profiles)} profiles / "
          f"{len({person(n) for n in profiles})} people")
    print(f"  WRONG-CONFIDENT: {len(wrong_confident)}"
          + ("  <-- the number that decides whether the margin is safe"
             if wrong_confident else "  (none)"))
    for sid8, truth, got, m in wrong_confident:
        print(f"      {sid8}  truth={truth}  named={got}  margin={m}")
    if floor_eligible_same:
        print(f"  weakest floor-eligible same-person score: {min(floor_eligible_same):+.3f}"
              f"  (n={len(floor_eligible_same)})")
        print("      This is the number the margin actually has to clear. The often-quoted"
              " +0.104 is the 2.1s turn the duration floor now excludes.")

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
