// Custom LLM endpoint for the ElevenLabs voice agent -- SHELL PHASE.
//
// Spec: "Voice Q&A Agent on ElevenLabs: Integration Spec" (owner, 2026-10-13),
// test plan step 1. ElevenLabs hosts the voice session (ASR, turn-taking, TTS)
// and calls THIS endpoint as an OpenAI-compatible Chat Completions server. It
// streams a restatement first, then the answer, in one SSE stream.
//
// This shell returns FIXED text. It exists to measure three things nobody has
// published: how long ElevenLabs waits for our first token, the end-to-end
// latency from end-of-turn to hearing the restatement, and the round trip
// between ElevenLabs and ap-southeast-2. Retrieval comes in the next phase and
// reuses the Python Ask path (rewrite, ACL, rag-search) rather than this file.
//
// Why Node and not Python: Lambda response streaming is native only on the
// Node.js runtimes. Python needs a custom runtime or the Lambda Web Adapter
// layer, and the deploy role is not allowed to read that public layer
// (simulate-principal-policy, 2026-10-13). This file stays a thin streaming
// facade; the logic it will front stays in Python.
//
// Probes, read from the last user message so they can be typed into a test
// conversation:
//   "delay N"  -> restatement immediately, answer after N seconds
//   "hold N"   -> NOTHING for N seconds, then everything. Finds ElevenLabs'
//                 first-token wait limit, which its docs do not state.

const BEARER = process.env.VOICE_LLM_BEARER || "";
const DEFAULT_ANSWER_DELAY_MS = Number(process.env.DEFAULT_ANSWER_DELAY_MS || "1500");
const MAX_PROBE_SECONDS = 60;

// ElevenLabs' documented buffer-words form: an ellipsis AND a trailing space.
// Without the space the next text is glued onto the ellipsis and the audio
// distorts (their docs call the space "crucial").
export const RESTATEMENT = "Got it, let me check that... ";
export const ANSWER =
  "This is the test endpoint. The real answer will come from your site records once retrieval is connected.";

export function lastUserText(body) {
  const msgs = Array.isArray(body?.messages) ? body.messages : [];
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i]?.role === "user") {
      const c = msgs[i].content;
      if (typeof c === "string") return c;
      if (Array.isArray(c)) return c.map((p) => (typeof p === "string" ? p : p?.text || "")).join(" ");
    }
  }
  return "";
}

// Numbers may arrive as digits ("delay 20", typed) or words ("delay twenty",
// spoken and transcribed); only digits are honoured -- probes are typed.
export function parseProbe(text) {
  const m = /\b(delay|hold)\s+(\d{1,3})\b/i.exec(text || "");
  if (!m) return { kind: "normal", seconds: 0 };
  return { kind: m[1].toLowerCase(), seconds: Math.min(Number(m[2]), MAX_PROBE_SECONDS) };
}

export function chunk(id, created, content, { first = false, finish = null } = {}) {
  const delta = {};
  if (first) delta.role = "assistant";
  if (content) delta.content = content;
  return `data: ${JSON.stringify({
    id,
    object: "chat.completion.chunk",
    created,
    model: "fieldsight-voice-shell",
    choices: [{ index: 0, delta, finish_reason: finish }],
  })}\n\n`;
}

// Two ways in, one secret. ElevenLabs reserves Authorization for its own
// custom_llm.api_key field: an Authorization set through request_headers did
// NOT arrive (2026-10-14, three calls from ElevenLabs refused at this check).
// X-Voice-Llm-Key is a header ElevenLabs has no reason to touch. Lambda
// Function URLs lower-case header names, so both are read lower-cased.
export const KEY_HEADER = "x-voice-llm-key";

export function authorised(headers) {
  if (!BEARER) return false;  // an unconfigured shell refuses everyone
  const h = lowerKeys(headers);
  if ((h.authorization || "") === `Bearer ${BEARER}`) return true;
  return (h[KEY_HEADER] || "") === BEARER;
}

function lowerKeys(headers) {
  const out = {};
  for (const [k, v] of Object.entries(headers || {})) out[k.toLowerCase()] = v;
  return out;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function decodeBody(event) {
  if (!event?.body) return {};
  const raw = event.isBase64Encoded ? Buffer.from(event.body, "base64").toString("utf8") : event.body;
  try { return JSON.parse(raw); } catch { return null; }
}

function plainResponse(stream, statusCode, obj) {
  const out = globalThis.awslambda.HttpResponseStream.from(stream, {
    statusCode,
    headers: { "Content-Type": "application/json" },
  });
  out.write(JSON.stringify(obj));
  out.end();
}

async function handle(event, responseStream) {
  const tArrive = Date.now();
  const method = event?.requestContext?.http?.method || "";
  const path = event?.rawPath || "";

  if (method === "GET" && path.endsWith("/health")) {
    return plainResponse(responseStream, 200, { ok: true });
  }
  if (method !== "POST" || !path.endsWith("/chat/completions")) {
    return plainResponse(responseStream, 404, { error: "not found" });
  }
  if (!authorised(event.headers)) {
    // Header NAMES only, never values: enough to see what a caller actually
    // sent (which is how the ElevenLabs Authorization problem was found) without
    // writing a credential into a log group.
    console.log(JSON.stringify({ evt: "voice_llm_refused", path,
      header_names: Object.keys(event.headers || {}).map((k) => k.toLowerCase()).sort() }));
    return plainResponse(responseStream, 401, { error: "unauthorised" });
  }
  const body = decodeBody(event);
  if (body === null) return plainResponse(responseStream, 400, { error: "malformed JSON" });

  const text = lastUserText(body);
  const probe = parseProbe(text);
  const id = `chatcmpl-shell-${tArrive}`;
  const created = Math.floor(tArrive / 1000);

  const out = globalThis.awslambda.HttpResponseStream.from(responseStream, {
    statusCode: 200,
    headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" },
  });

  const marks = {};
  if (probe.kind === "hold") await sleep(probe.seconds * 1000);
  out.write(chunk(id, created, RESTATEMENT, { first: true }));
  marks.restatement_ms = Date.now() - tArrive;

  if (probe.kind === "delay") await sleep(probe.seconds * 1000);
  else if (probe.kind === "normal") await sleep(DEFAULT_ANSWER_DELAY_MS);

  for (const part of ANSWER.split(/(?<=\. )/)) {
    out.write(chunk(id, created, part));
    await sleep(40);
  }
  marks.answer_ms = Date.now() - tArrive;
  out.write(chunk(id, created, "", { finish: "stop" }));
  out.write("data: [DONE]\n\n");
  out.end();

  // One line per turn: everything the measurement needs, nothing it does not.
  console.log(JSON.stringify({
    evt: "voice_llm_turn",
    probe,
    user_text: text,
    turns: Array.isArray(body.messages) ? body.messages.length : 0,
    stream: body.stream === true,
    model_requested: body.model || null,
    extra_body_keys: Object.keys(body.elevenlabs_extra_body || {}),
    ...marks,
  }));
}

// `awslambda` exists only inside the Lambda runtime; under a local test the
// handler is exported unwrapped so it can be driven with a fake stream.
export const handler = globalThis.awslambda?.streamifyResponse
  ? globalThis.awslambda.streamifyResponse(handle)
  : handle;
