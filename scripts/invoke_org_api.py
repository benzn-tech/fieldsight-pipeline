"""Invoke the deployed org-api Lambda with a synthetic API Gateway event.

A smoke-test tool for endpoints that are already live. Authorization is the
AWS invoke permission itself: the Cognito authorizer never runs, so the caller
identity is whatever `sub` you pass, and lambda_org_api resolves it against
the users table exactly as it would in production.

  python scripts/invoke_org_api.py <cognito_sub> <METHOD> <path> [qs-json] [body-json]

  python scripts/invoke_org_api.py b9ce...c0 GET /api/org/programme '{"site":"<uuid>"}'

WINDOWS/GIT-BASH TRAP: MSYS rewrites arguments that look like Unix paths, so
`/api/org/me` arrives as `C:/Program Files/Git/api/org/me` and every call 404s
with the router silently falling through. Export MSYS_NO_PATHCONV=1 first.
That cost me a round of debugging the wrong layer.
"""
import json, subprocess, sys
SUB = sys.argv[1]; METHOD = sys.argv[2]; PATH = sys.argv[3]
qs = json.loads(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] else None
body = sys.argv[5] if len(sys.argv) > 5 else None
ev = {
  "requestContext": {"authorizer": {"claims": {"sub": SUB}}, "http": {"method": METHOD}},
  "httpMethod": METHOD,
  "rawPath": PATH, "path": PATH, "resource": PATH,
  "queryStringParameters": qs,
  "body": body,
}
open('ev.json','w').write(json.dumps(ev))
r = subprocess.run(["aws","lambda","invoke","--function-name","fieldsight-test-org-api",
    "--cli-binary-format","raw-in-base64-out","--payload","file://ev.json",
    "out.json","--region","ap-southeast-2"], capture_output=True, text=True)
if r.returncode: print("INVOKE ERR", r.stderr[:500]); sys.exit(1)
resp = json.load(open('out.json'))
print("status:", resp.get("statusCode"))
b = resp.get("body")
print("body:", (b[:600] if isinstance(b,str) else json.dumps(b)[:600]))
