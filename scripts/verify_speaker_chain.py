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
* **whether anything is listening on `voiceprint_requests/`** — that notification is
  hand-wired outside the template (BUG-33). Lose it and `speaker-match` still answers 202,
  artifacts pile up, and nothing runs, with no in-band symptom at all: the transcript simply
  never gains a name, which looks exactly like the matcher receiving no profiles.
* **the mode, before judging by names** — `shadow` computes every score and writes nothing,
  by design, and `on` is prohibited until a margin is calibrated. Judging shadow by "did the
  named count go up" would report the designed behaviour as defect #539 on every permitted
  configuration.
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
import os
import subprocess
import tempfile
import sys
import time

REGION = "ap-southeast-2"
# `fieldsight-prod-*`, not `fieldsight-*`. The stack prefixes BOTH stages, and the
# abbreviated form was a guess that no test could catch and that only shows up as
# ResourceNotFoundException the first time somebody runs this against prod — which is
# exactly what happened, 25 invocations in a row, every one of them "not found" and none
# of them saying which name it had tried.
PREFIX = {"test": "fieldsight-test", "prod": "fieldsight-prod"}


def _aws(args):
    r = subprocess.run(["aws", *args, "--region", REGION], capture_output=True, text=True)
    if r.returncode:
        err = (r.stderr or "").strip()[:300]
        if "ResourceNotFound" in err:
            # Name the thing that was not found. A bare "ResourceNotFoundException" sends the
            # reader to look for a broken deploy when the truth is a misspelt prefix here.
            named = [a for a in args if a.startswith("fieldsight")]
            err += "  <- looked for: %s" % (named or args)
        return None, err
    return r.stdout, None


def _scratch(name):
    """A lambda payload/response path OUTSIDE the repository.

    This script used to write `_vsc.json` and `_vsc.json.out` into the repo root, and its
    response half is the worst of the family: `GET /api/org/voiceprints` returns people's
    display names, and the transcript read returns their words. Two consequences, and
    .gitignore only addresses the first: an untracked file one `git add -A` away from being
    committed, and a file inside a Dropbox-synced directory, which syncs regardless of git.
    """
    return os.path.join(tempfile.gettempdir(), name)


def _invoke(function, payload, scratch="_vsc.json"):
    path = _scratch(scratch)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    out, err = _aws(["lambda", "invoke", "--function-name", function,
                     "--cli-binary-format", "raw-in-base64-out",
                     "--payload", "file://" + path, path + ".out"])
    if err:
        return None, err
    with open(path + ".out", encoding="utf-8") as fh:
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


def trigger_is_wired(env):
    """Is anything actually listening on `voiceprint_requests/`?

    This notification is hand-wired outside the template (BUG-33) — `SpeakerEmbedFunction`
    carries no S3 event of its own. If it is ever lost, `speaker-match` still answers 202,
    artifacts pile up in the bucket, and nothing runs. There is no in-band symptom: the
    transcript simply never gains a name, which is indistinguishable from the matcher
    receiving no profiles, and this script would then send the reader to defect #539 — the
    wrong bug entirely.

    Checked here because a script whose stated purpose is telling apart indistinguishable
    failures has no business leaving one of them out.
    """
    bucket = ("fieldsight-data-test-509194952652" if env == "test"
              else "fieldsight-data-509194952652")
    out, err = _aws(["s3api", "get-bucket-notification-configuration", "--bucket", bucket,
                     "--output", "json"])
    if err:
        return None, err
    cfg = json.loads(out or "{}") or {}
    for entry in cfg.get("LambdaFunctionConfigurations") or []:
        rules = (entry.get("Filter") or {}).get("Key", {}).get("FilterRules") or []
        if any("voiceprint_requests" in str(r.get("Value", "")) for r in rules):
            return entry.get("LambdaFunctionArn", "").split(":")[-1] or True, None
    return False, None


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

    # WHICH credentials, before anything else. These scripts inherit the ambient AWS
    # profile, and the default one on this machine is a root `login_session` that expires
    # daily while two non-expiring IAM profiles sit beside it in ~/.aws/config. A week of
    # "AWS is down" was that, and the only symptom was `Your session has expired` with no
    # hint that another profile would have worked.
    who, err = _aws(["sts", "get-caller-identity", "--query", "Arn", "--output", "text"])
    print("== identity ==")
    if err:
        print(f"  {err.splitlines()[0] if err else 'unknown'}")
        print(f"  profile in use: {os.environ.get('AWS_PROFILE', '(default)')}")
        print("  try: AWS_PROFILE=fieldsight-deployer  (a non-expiring IAM user)")
        return 2
    print(f"  {who.strip()}   profile={os.environ.get('AWS_PROFILE', '(default)')}")

    print()
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

    wired, err = trigger_is_wired(args.env)
    if err:
        print(f"  S3 trigger on voiceprint_requests/: could not check ({err[:80]})")
    elif wired:
        print(f"  S3 trigger on voiceprint_requests/  -> {wired}")
    else:
        print("\n  NO S3 NOTIFICATION on voiceprint_requests/. Every request below will be")
        print("  accepted with a 202, write its artifact, and never be picked up. This")
        print("  notification is hand-wired outside the template (BUG-33), so it can be lost")
        print("  by a bucket-level change nobody connected to this feature. Fix it before")
        print("  reading anything further as evidence about the matcher.")

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
    # `--user`, NOT an empty body. `_resolve_org_media_folder` falls back to the CALLER's own
    # folder when `user` is absent, so an empty body made a manager verify their own
    # transcripts: no turns for the requested session, 409, and the script reported "refused"
    # for the one flow it exists to check. It could never have passed.
    queued, err = _api(args.env, args.sub, "POST",
                       f"/sessions/{args.session}/speaker-match",
                       body={"user": args.user})
    if err:
        print(f"  refused: {err}")
        return 2
    print(f"  {queued.get('turnsQueued')} turns in {queued.get('runs')} run(s), "
          f"mode={queued.get('mode')}, willWriteNames={queued.get('willWriteNames')}, "
          f"siteId={queued.get('siteId')}")

    # `shadow` computes every score and writes NOTHING — that is the mode's purpose, and
    # `on` is prohibited until a margin is calibrated, so shadow is the only mode this can
    # currently run against with the feature enabled. Judging it by "did the named count go
    # up" would report the designed behaviour as defect #539 on every permitted
    # configuration: a manufactured alarm pointing at a bug that was already fixed.
    if not queued.get("willWriteNames"):
        print()
        print("  Mode is not `on`, so the matcher writes no names by design. Nothing about")
        print("  the transcript can change, and waiting for it to would prove nothing. Read")
        print("  the embedder's log for `match(shadow): ... would name N of M` — that count")
        print("  is what this mode exists to produce, and it is the number to calibrate on.")
        return 0

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
