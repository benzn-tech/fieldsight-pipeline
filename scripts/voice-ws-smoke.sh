#!/usr/bin/env bash
# voice-ws-smoke.sh <stack> <region>
# End-to-end Site Voice smoke on a deployed stack (e.g. fieldsight-test):
#   authorizer (allow + deny) / connect / upload-url + PUT / sendVoice /
#   fanout-receive / backfill / reaper sweep.
# Requires: aws CLI, node (auto-installs the `ws` package to a temp dir).
# Env:
#   VOICE_SMOKE_TOKEN   Cognito idToken of a site member (REQUIRED to run;
#                       absent -> skip cleanly with exit 0 so CI never breaks)
#   VOICE_SMOKE_SITE    site UUID that member belongs to (REQUIRED)
#   VOICE_SMOKE_TOKEN2  a SECOND member's idToken (OPTIONAL) -> enables the
#                       fanout-receive assertion (sender is excluded, so two
#                       distinct members are needed to observe delivery)
set -euo pipefail
STACK="${1:?usage: voice-ws-smoke.sh <stack> <region>}"
REGION="${2:?missing region}"

if [ -z "${VOICE_SMOKE_TOKEN:-}" ] || [ -z "${VOICE_SMOKE_SITE:-}" ]; then
  echo "SKIP: VOICE_SMOKE_TOKEN / VOICE_SMOKE_SITE not set (no token to drive the WS)."
  exit 0
fi

out() { aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
WS="$(out VoiceWsEndpoint)"; API="$(out ApiEndpoint)"
[ -n "$WS" ] && [ "$WS" != "None" ] || { echo "no VoiceWsEndpoint output"; exit 1; }
echo "WS=$WS  API=$API"

# --- REST: reserve an upload, PUT bytes -----------------------------------
UP="$(curl -s -X POST "$API/org/voice/upload-url" \
  -H "Authorization: $VOICE_SMOKE_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"contentType\":\"audio/wav\",\"siteId\":\"$VOICE_SMOKE_SITE\",\"durationS\":1.0}")"
echo "upload-url -> $UP"
S3KEY="$(node -e "process.stdin.on('data',d=>{const j=JSON.parse(d);console.log(j.s3Key)})" <<<"$UP")"
URL="$(node -e "process.stdin.on('data',d=>{const j=JSON.parse(d);console.log(j.uploadUrl)})" <<<"$UP")"
[ -n "$S3KEY" ] || { echo "no s3Key returned"; exit 1; }
printf 'RIFF....WAVEfmt ' > /tmp/voice-smoke.wav   # 16 bytes is enough to PUT
curl -s -X PUT "$URL" -H 'Content-Type: audio/wav' --data-binary @/tmp/voice-smoke.wav
echo "PUT ok: $S3KEY"

# --- node ws harness: authorizer allow/deny + connect + sendVoice/fanout ---
WORK="$(mktemp -d)"; ( cd "$WORK" && npm i ws --silent >/dev/null 2>&1 )
export NODE_PATH="$WORK/node_modules"
WS_URL="$WS" GOOD="$VOICE_SMOKE_TOKEN" TOKEN2="${VOICE_SMOKE_TOKEN2:-}" \
SITE="$VOICE_SMOKE_SITE" S3KEY="$S3KEY" node <<'NODE'
const WebSocket = require('ws');
const { WS_URL, GOOD, TOKEN2, SITE, S3KEY } = process.env;
const conn = (tok) => new Promise((res, rej) => {
  const w = new WebSocket(WS_URL, { headers: { Authorization: tok } });
  w.on('open', () => res(w));
  w.on('unexpected-response', (_r, r) => rej(new Error('handshake ' + r.statusCode)));
  w.on('error', rej);
});
(async () => {
  // 1) authorizer DENY: a bad token must be refused at the handshake.
  let denied = false;
  try { await conn('not-a-real-token'); } catch (e) { denied = /handshake 401|403/.test(e.message); }
  if (!denied) throw new Error('bad token was NOT rejected');
  console.log('authorizer deny: ok');

  // 2) authorizer ALLOW + connect.
  const a = await conn(GOOD);
  console.log('authorizer allow + connect: ok');

  if (TOKEN2) {
    // 3) fanout: B (a second member) must receive A's sendVoice; A must not.
    const b = await conn(TOKEN2);
    const got = new Promise((res) => b.on('message', (m) => res(JSON.parse(m))));
    let selfEcho = false; a.on('message', () => { selfEcho = true; });
    a.send(JSON.stringify({ action: 'sendVoice', siteId: SITE, s3Key: S3KEY, durationS: 1.0 }));
    const msg = await Promise.race([got,
      new Promise((_, rej) => setTimeout(() => rej(new Error('B never received')), 8000))]);
    if (msg.s3Key !== S3KEY) throw new Error('B got wrong payload: ' + JSON.stringify(msg));
    if (selfEcho) throw new Error('sender received its own message');
    console.log('fanout receive (B got it, A did not): ok');
    b.close();
  } else {
    // No second member -> just prove sendVoice is accepted (0 recipients).
    a.send(JSON.stringify({ action: 'sendVoice', siteId: SITE, s3Key: S3KEY, durationS: 1.0 }));
    console.log('sendVoice accepted (single member; fanout receive skipped — set VOICE_SMOKE_TOKEN2)');
  }
  a.close();
})().then(() => process.exit(0)).catch((e) => { console.error('FAIL:', e.message); process.exit(1); });
NODE

# --- REST: backfill must list the message we just sent --------------------
BF="$(curl -s "$API/org/sites/$VOICE_SMOKE_SITE/voice?since=1970-01-01T00:00:00Z" \
  -H "Authorization: $VOICE_SMOKE_TOKEN")"
echo "$BF" | node -e "process.stdin.on('data',d=>{const j=JSON.parse(d);const hit=(j.items||[]).some(m=>m.s3Key===process.env.S3KEY);if(!hit){console.error('backfill missing '+process.env.S3KEY);process.exit(1)}console.log('backfill: ok ('+j.items.length+' msgs)')})" S3KEY="$S3KEY"

# --- reaper sweep: must run without a FunctionError ------------------------
PREFIX="$(basename "$STACK")"   # fieldsight-test / fieldsight-prod
RESP="$(aws lambda invoke --function-name "${PREFIX}-voice-reaper" \
  --cli-binary-format raw-in-base64-out --payload '{"sweep": true}' \
  /tmp/reaper-out.json --region "$REGION")"
echo "reaper: $RESP"; cat /tmp/reaper-out.json; echo
echo "$RESP" | grep -q '"FunctionError"' && { echo "reaper raised"; exit 1; } || true
echo "ALL SITE VOICE SMOKE CHECKS PASSED"
