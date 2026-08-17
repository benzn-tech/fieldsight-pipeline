#!/usr/bin/env python3
"""Check the speaker-naming chain end to end, on one real session, against the live stack.

    uv run --with boto3 python scripts/verify_speaker_chain.py \
        --sub <cognito-sub> --user Ben_UCPK2 --date 2026-08-12 [--env test]

Written as a script because the manual version is what produced this project's worst defect
of the week. `invoke_writer` returned boto3's response envelope instead of the callee's
decoded payload, so `.get("profiles")` was always None, the matcher's "no consented profiles
for this company" branch fired on every single run, and **the whole speaker-match path did
nothing in production** — while the log line read exactly like an empty database. Twenty unit
tests monkeypatched that function. None exercised it. Nothing about the manual checks I was
doing could have caught it, because every one of them read a surface that was working.

So this asserts the things that were indistinguishable from each other, and reports the
distinction rather than a tick:

* **what the deployed function actually carries** — `SPEAKER_IDENTITY_MODE` and
  `VOICEPRINT_MAX_FRAME_SPREAD`, read from the function configuration, not from the repo
  variable and not from the template. A test run against a stack that never got the change
  is the second-most common way this project has verified the wrong thing.
* **profiles, and why each is as it is** — `samples`, `humanSamples`, and how the last
  enrolment attempt ended. A named profile with zero samples names nobody in any future
  meeting, and until the last-attempt fields existed that was indistinguishable from an
  embedder that died halfway.
* **whether "nothing was named" means "no profiles" or "profiles were not delivered"** —
  the #539 distinction. If profiles exist and the match still names nothing, that is the
  interesting failure and this says so in those words.
* **the asynchronous part, waited out** — match is queued through an S3 artifact and the
  embedder has taken 138 seconds on a real session. Reading the transcript once, straight
  after the POST, and concluding "it did not happen" is a mistake I have actually made here.
  This polls until the count stops moving and prints how long it took.

What it deliberately does NOT claim: that a name is CORRECT. It counts names and reports
their states. Whether `spk_1` is really Ivy is a question for a person with ears, and a
script that implied otherwise would be worse than no script.
"""
import argparse
import json
import subprocess
import sys
import time

REGION = "ap-southeast-2"
PREFIX = {"test": "fieldsight-test", "prod": "fieldsight"}


def _aws(args):
    r = subprocess.run(["aws", *args, "--region", REGION], capture_output=True, text=True)
    if r.returncode:
        return None, (r.stderr or "").strip()[:300]
    return r.stdout, None


def _invoke(function, payload, scratch="_vsc.json"):
    with open(scratch, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    out, err = _aws(["lambda", "invoke", "--function-name", function,
                     "--cli-binary-format", "raw-in-base64-out",
                     "--payload", f"file://{scratch}", scratch + ".out"])
    if err:
        return None, err
    with open(scratch + ".out", encoding="utf-8") as fh:
        return json.load(fh), None


def _api(env, sub, method, path, params=None, body=None):
    event = {"httpMethod": method, "path": "/api/org" + path,
             "requestContext": {"authorizer": {"claims": {"sub": sub}}}}
    if params:
        event["queryStringParameters"] = params
    if body is not None:
        event["body"] = json.dumps(body)
    got, err = _invoke(f"{PREFIX[env]}-org-api", event)
    if err:
        return None, err
    code = (got or {}).get("statusCode")
    if code != 200 and code != 202:
        return None, f"HTTP {code}: {str((got or {}).get('body'))[:200]}"
    return json.loads(got["body"], strict=False), None


def deployed_settings(env):
    """What the function is RUNNING with. Not the repo variable, not the template default.

    A repo variable is what somebody intended; the function configuration is what arrived.
    They differ whenever a deploy failed, was skipped, or has not run yet — and a skipped
    deploy in this repository reports `skipped`, not `failed`, so nothing alerts.
    """
    out, err = _aws(["lambda", "get-function-configuration",
                     "--function-name", f"{PREFIX[env]}-speaker-embed",
                     "--query", "Environment.Variables", "--output", "json"])
    if err:
        return None, err
    embed = json.loads(out or "{}") or {}
    out, err = _aws(["lambda", "get-function-configuration",
                     "--function-name", f"{PREFIX[env]}-org-api",
                     "--query", "Environment.Variables.SPEAKER_IDENTITY_MODE",
                     "--output", "text"])
    mode = (out or "").strip()
    if err or not mode:
        # Fatal, not a blank field. An unreadable mode falling through as "" would be
        # compared against "off", not match, and every check below would run as though the
        # feature were enabled — a script reporting on a stack it could not read.
        return None, err or "SPEAKER_IDENTITY_MODE came back empty"
    return {"mode": mode,
            "max_frame_spread": embed.get("VOICEPRINT_MAX_FRAME_SPREAD",
                                          "(unset -> 0.35)")}, None


def named_turns(env, sub, user, date):
    """Every named segment in the day, by state. The read the user themselves would do."""
    got, err = _api(env, sub, "GET", "/transcripts",
                    {"date": date, "user": user, "start": "00:00", "end": "23:59"})
    if err:
        return None, err
    segs = got.get("speaker_segments") or []
    named = [s for s in segs if s.get("speaker_name")]
    by_state = {}
    for s in named:
        by_state[s.get("speaker_state") or "?"] = by_state.get(s.get("speaker_state") or "?", 0) + 1
    return {"segments": len(segs), "named": len(named), "by_state": by_state,
            "names": sorted({s["speaker_name"] for s in named}),
            "sessions": sorted({s.get("source_filename", "")[:60] for s in segs})}, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sub", required=True, help="cognito sub of a manager-role caller")
    ap.add_argument("--user", required=True, help="user folder, e.g. Ben_UCPK2")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--env", default="test", choices=["test", "prod"])
    ap.add_argument("--session", help="session base to match; default: skip the match step")
    ap.add_argument("--wait", type=int, default=240, help="seconds to wait for the async match")
    args = ap.parse_args()

    if args.env == "prod" and args.session:
        print("refusing to queue a match against prod from a verification script.")
        return 2

    print("== what the deployed stack is running ==")
    cfg, err = deployed_settings(args.env)
    if err and not cfg:
        print(f"  could not read it: {err}")
        return 2
    print(f"  SPEAKER_IDENTITY_MODE        {cfg['mode']}")
    print(f"  VOICEPRINT_MAX_FRAME_SPREAD  {cfg['max_frame_spread']}")
    if cfg["mode"] == "off":
        print("\n  The feature is OFF on this stack. Every endpoint below returns 404 and no")
        print("  code path runs. That is not a failure — it is what prod is meant to look")
        print("  like — but nothing further can be verified here.")
        return 0
    if cfg["max_frame_spread"] != "(unset -> 0.35)":
        print("\n  NOTE: the homogeneity guard is LOOSENED on this stack. Samples admitted")
        print("  under it are not evidence that the window held one voice; each carries the")
        print("  limit it came in under in speaker_voiceprint_samples.admitted_max_spread.")

    print("\n== profiles ==")
    got, err = _api(args.env, args.sub, "GET", "/voiceprints")
    if err:
        print(f"  could not read them: {err}")
        return 2
    profiles = got.get("voiceprints") or []
    if not profiles:
        print("  none. Nothing can be matched until a correction creates one.")
    for p in profiles:
        print(f"  {(p.get('displayName') or '(unnamed)'):<22} samples={p.get('samples'):<3} "
              f"human={p.get('humanSamples'):<3} linkedOn={p.get('linkedOn') or '-'}")
        if not p.get("samples"):
            print(f"     empty — last attempt {p.get('lastAttemptOutcome') or '(never)'}"
                  + (f": {p['lastAttemptDetail']}" if p.get("lastAttemptDetail") else ""))
    usable = [p for p in profiles if (p.get("samples") or 0) > 0]
    print(f"\n  {len(usable)} of {len(profiles)} profile(s) hold a sample and can name anybody.")

    print("\n== names on the transcript, before ==")
    before, err = named_turns(args.env, args.sub, args.user, args.date)
    if err:
        print(f"  could not read it: {err}")
        return 2
    print(f"  {before['named']} of {before['segments']} segments named  {before['by_state']}")
    if before["names"]:
        print(f"  names present: {', '.join(before['names'])}")

    if not args.session:
        print("\n  No --session given, so the matcher was not run. The counts above are the")
        print("  standing state, which is what a correction leaves behind.")
        return 0

    print(f"\n== queueing a match for {args.session} ==")
    queued, err = _api(args.env, args.sub, "POST",
                       f"/sessions/{args.session}/speaker-match", body={})
    if err:
        print(f"  refused: {err}")
        return 2
    print(f"  {queued.get('turnsQueued')} turns in {queued.get('runs')} run(s), "
          f"mode={queued.get('mode')}, willWriteNames={queued.get('willWriteNames')}, "
          f"siteId={queued.get('siteId')}")

    # Waited out, not sampled once. The embedder has taken 138 s on a real session, and
    # reading straight after the POST and calling it "did not happen" is a mistake made here
    # for real. Stop when the count has held still for two consecutive polls.
    print(f"\n== waiting up to {args.wait}s for the asynchronous half ==")
    start, last, stable = time.time(), before["named"], 0
    after = before
    while time.time() - start < args.wait:
        time.sleep(15)
        after, err = named_turns(args.env, args.sub, args.user, args.date)
        if err:
            print(f"  read failed: {err}")
            break
        elapsed = int(time.time() - start)
        print(f"  +{elapsed:>3}s  named={after['named']}  {after['by_state']}")
        stable = stable + 1 if after["named"] == last else 0
        last = after["named"]
        if stable >= 2 and after["named"] != before["named"]:
            break

    gained = after["named"] - before["named"]
    print(f"\n== result ==  {gained:+d} named turns in {int(time.time() - start)}s")
    if gained > 0:
        print("  The matcher named turns from stored profiles. What it does NOT establish is")
        print("  that the names are RIGHT — that needs a person who can recognise the voice.")
        return 0
    if not usable:
        print("  Nothing was named, and no profile holds a sample — so the matcher had")
        print("  nothing to match against. This is the expected outcome, not a defect, and")
        print("  the thing to fix is enrolment (see the profile table above for why each")
        print("  profile is empty).")
        return 0
    print("  Nothing was named although profiles DO hold samples. This is the interesting")
    print("  failure: the candidates exist and did not produce a name. Check the embedder's")
    print("  log for 'no consented profiles' — if it says that while the table above shows")
    print("  samples, the profiles are not reaching the matcher, which is defect #539 again.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
