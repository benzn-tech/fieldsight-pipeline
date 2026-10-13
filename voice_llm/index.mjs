// Custom LLM endpoint for the ElevenLabs voice agent -- SHELL PHASE.
//
// Spec: "Voice Q&A Agent on ElevenLabs: Integration Spec" (owner, 2026-10-13),
// test plan step 1. ElevenLabs hosts the voice session (ASR, turn-taking, TTS)
// and calls THIS endpoint as an OpenAI-compatible Chat Completions server. It
// streams a restatement first, then the answer, in one SSE stream.
//
// UPDATE (phase 2): normal turns now run the real flow in handle() below --
// verify the voice-session token, speak the opener at once, ask AskAgentFunction
// for the prompt, stream the answer, fall back to a fixed line on any failure.
// The fixed-text shell survives only as the hold/delay probes (VOICE_LLM_PROBES).
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
// Probes (VOICE_LLM_PROBES=true only), read from the last user message so they
// can be typed into a test conversation:
//   "delay N"  -> restatement immediately, answer after N seconds
//   "hold N"   -> NOTHING for N seconds, then everything. Finds ElevenLabs'
//                 first-token wait limit, which its docs do not state.

import { createHash, createHmac, timingSafeEqual } from "node:crypto";
import { verifyVoiceSession } from "./token.mjs";
import { createTurnStream, OPENER, FALLBACK } from "./stream.mjs";
import { toHistory } from "./history.mjs";
import { streamAnswer } from "./answer.mjs";
import { DEFAULT_MODEL as RESTATE_DEFAULT_MODEL, DEADLINE_MS as RESTATE_DEADLINE_MS, isCorrection, modelRejected,
  assessRestatement, restateViaModel } from "./restate.mjs";

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
// Flags after the number, any order (2026-10-14, after "delay 8" made
// ElevenLabs repeat the restatement three times and never speak the answer):
//   ka -> an SSE comment line every second of the gap (bytes, no content)
//   sp -> a single-space content delta every second of the gap
//   p  -> the restatement as a full sentence ending ". " instead of "... "
export const PERIOD_RESTATEMENT = "Got it. Let me check that. ";
//   l  -> a ~140-character restatement, the length a real one reaches.
//         Measured: a 27-character restatement was NOT spoken until the answer
//         arrived, even with the turn kept alive; ElevenLabs' TTS appears to
//         hold text below a length threshold. This finds out.
//   n  -> a newline appended to the restatement, in case a line break flushes.
export const LONG_RESTATEMENT =
  "Got it, you're asking when the Unit 11 backfill has to be finished and who is doing the trench work for the Hanlon plumbing, let me check... ";

export function parseProbe(text) {
  const m = /^\s*(delay|hold)\s+(\d{1,3})((?:\s+(?:ka|sp|p|l|n|w))*)\s*$/i.exec(text || "");
  if (!m) return { kind: "normal", seconds: 0, flags: [] };
  const flags = (m[3] || "").trim().toLowerCase().split(/\s+/).filter(Boolean);
  return { kind: m[1].toLowerCase(), seconds: Math.min(Number(m[2]), MAX_PROBE_SECONDS), flags };
}

// Sleep for the gap, emitting the chosen keep-alive once a second.
async function gap(out, id, created, ms, flags) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    await sleep(Math.min(1000, end - Date.now()));
    if (Date.now() >= end) break;
    if (flags.includes("ka")) out.write(": keep-alive\n\n");
    if (flags.includes("sp")) out.write(chunk(id, created, " "));
  }
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

// Length-checked timingSafeEqual (same approach as token.mjs); it throws on
// unequal lengths, so those are rejected first.
function safeEqual(a, b) {
  const x = Buffer.from(String(a)), y = Buffer.from(String(b));
  return x.length === y.length && timingSafeEqual(x, y);
}

export function authorised(headers) {
  const bearer = process.env.VOICE_LLM_BEARER || "";
  if (!bearer) return false;  // an unconfigured shell refuses everyone
  const h = lowerKeys(headers);
  const viaAuth = safeEqual(h.authorization || "", `Bearer ${bearer}`);
  const viaKey = safeEqual(h[KEY_HEADER] || "", bearer);
  return viaAuth || viaKey;
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

// Out wrapper: a destroyed client stream must never turn a keep-alive timer
// tick into an uncaught exception.
function safeOut(out) {
  return {
    write(s) { try { out.write(s); } catch { /* client gone */ } },
    end() { try { out.end(); } catch { /* client gone */ } },
  };
}

// AskAgentFunction answers with an API-Gateway-style {statusCode, body}. Only a
// 200 whose body carries `kind` is usable; everything else is invoke_failed.
export function parseAskResult(payload) {
  if (!payload || typeof payload !== "object" || payload.statusCode !== 200) return null;
  let b = payload.body;
  if (typeof b === "string") { try { b = JSON.parse(b); } catch { return null; } }
  return b && typeof b === "object" && typeof b.kind === "string" ? b : null;
}

// Real Ask invoke. The SDK ships with the Node runtime but not locally or in
// CI, so it is loaded here, never at the top of the module.
let lambdaClient = null;
async function defaultInvokeAsk(payload, { signal } = {}) {
  const { LambdaClient, InvokeCommand } = await import("@aws-sdk/client-lambda");
  lambdaClient ||= new LambdaClient({});
  const res = await lambdaClient.send(new InvokeCommand({
    FunctionName: process.env.ASK_AGENT_FUNCTION,
    InvocationType: "RequestResponse",
    Payload: Buffer.from(JSON.stringify(payload)),
  }), { abortSignal: signal });
  if (res.FunctionError) throw new Error("function_error");
  return JSON.parse(Buffer.from(res.Payload).toString("utf8"));
}

class InvokeTimeout extends Error {}

async function invokeWithTimeout(invokeAsk, payload, ms) {
  const ac = new AbortController();
  let t;
  const timeout = new Promise((_, rej) => { t = setTimeout(() => { ac.abort(); rej(new InvokeTimeout("invoke_timeout")); }, ms); });
  const call = Promise.resolve().then(() => invokeAsk(payload, { signal: ac.signal }));
  call.catch(() => {});  // a late rejection after the race is lost must not be unhandled
  try { return await Promise.race([call, timeout]); } finally { clearTimeout(t); }
}

// retry_suspected: ElevenLabs silently re-sends a dropped turn. Same last user
// text and same message count within 10 s = the same turn again.
const recentTurns = new Map();
function retrySuspected(text, count, nowMs) {
  const k = createHash("sha256").update(`${text}\u0000${count}`).digest("hex");
  const prev = recentTurns.get(k);
  for (const [key, at] of recentTurns) if (nowMs - at > 60000) recentTurns.delete(key);
  recentTurns.set(k, nowMs);
  return prev !== undefined && nowMs - prev < 10000;
}

const INVOKE_TIMEOUT_MS = 9000;
const TURN_GUARD_MS = 30000;
// First-token waits. OpenRouter's normal first token is ~2 s but it has tail
// spikes of 10-15 s; the keep-alive holds the ElevenLabs turn open, so one
// longer retry beats a spoken fallback.
const FIRST_TOKEN_MS = 5000;
const RETRY_FIRST_TOKEN_MS = 8000;
const retryable = (e) => e === "first_token_timeout" || /^http_5\d\d$/.test(e || "");
const DEFAULT_BASE_URL = "https://openrouter.ai/api/v1";
let reasoningModelLogged = false;

export async function handle(event, responseStream, deps = {}) {
  const invokeAsk = deps.invokeAsk || defaultInvokeAsk;
  const fetchImpl = deps.fetchImpl || fetch;
  const answerStream = deps.streamAnswer || streamAnswer;
  const restateCall = deps.restate || ((a) => restateViaModel({ ...a, fetchImpl }));
  const nowSeconds = deps.now || (() => Math.floor(Date.now() / 1000));
  const nowMs = deps.nowMs || Date.now;
  const tArrive = nowMs();
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
  const probe = process.env.VOICE_LLM_PROBES === "true" ? parseProbe(text) : { kind: "normal", seconds: 0, flags: [] };
  if (probe.kind !== "normal") return probeTurn(responseStream, body, probe, tArrive);

  const key = process.env.VOICE_SESSION_SIGNING_KEY || "";
  const apiKey = process.env.OPENROUTER_API_KEY || "";
  const missing = [["VOICE_SESSION_SIGNING_KEY", key], ["ASK_AGENT_FUNCTION", process.env.ASK_AGENT_FUNCTION],
    ["OPENROUTER_API_KEY", apiKey]].filter(([, v]) => !v).map(([n]) => n);
  if (missing.length) {
    console.log(JSON.stringify({ evt: "voice_llm_misconfigured", missing }));
    return plainResponse(responseStream, 503, { error: "voice endpoint not configured" });
  }

  const out = safeOut(globalThis.awslambda.HttpResponseStream.from(responseStream, {
    statusCode: 200,
    headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" },
  }));
  const turn = createTurnStream(out, { id: `chatcmpl-${tArrive}`, created: Math.floor(tArrive / 1000) });
  const extra = body.elevenlabs_extra_body || {};

  // Restatement (VOICE_RESTATE_ENABLED=true): only for a turn whose token already
  // verifies (synchronous), so an unauthenticated caller never costs a model call.
  // Everything else, and the flag off, speaks OPENER before any await.
  const restateOn = process.env.VOICE_RESTATE_ENABLED === "true";
  const restateModel = process.env.VOICE_RESTATE_MODEL || RESTATE_DEFAULT_MODEL;
  let restateReject = restateOn ? null : "disabled";
  let restatePlan = null;
  if (restateOn) {
    if (modelRejected(restateModel)) {
      restateReject = "reasoning_model";
      if (!reasoningModelLogged) {
        reasoningModelLogged = true;
        console.log(JSON.stringify({ evt: "voice_llm_restate_model_rejected", model: restateModel }));
      }
    } else {
      try {
        const pre = toHistory(body.messages);
        if (verifyVoiceSession(extra.voice_session, key, nowSeconds())) {
          if (isCorrection(pre.question)) restateReject = "correction";
          else if (!pre.question) restateReject = "error";
          else restatePlan = { question: pre.question, prevQuestion: pre.history.at(-1)?.question };
        }
      } catch { restateReject = "error"; }
    }
  }
  const log = { evt: "voice_llm_turn", sub_hash: null, outcome: "error", invoke_ms: null,
    first_answer_token_ms: null, answer_chars: 0, chunk_ids: [], rewritten: null,
    history_turns: null, kind: null, retry_suspected: null,
    restate_used: false, restate_ms: null, restate_reject: restateReject };
  // Resolves once the first real text (restatement or OPENER) is written; never rejects.
  let opened = Promise.resolve();
  if (restatePlan) {
    turn.text(" ");  // starts the keep-alive while the model works; before any await
    opened = (async () => {
      const t0 = nowMs();
      const ac = new AbortController();
      let timer;
      const deadline = new Promise((res) => {
        timer = setTimeout(() => { ac.abort(); res({ error: "deadline" }); },
          Math.max(0, RESTATE_DEADLINE_MS - (t0 - tArrive)));
      });
      const call = Promise.resolve().then(() => restateCall({ ...restatePlan, model: restateModel, apiKey,
        baseUrl: process.env.OPENROUTER_BASE_URL || DEFAULT_BASE_URL, signal: ac.signal })).catch(() => ({ error: "error" }));
      const r = await Promise.race([call, deadline]);
      clearTimeout(timer);
      log.restate_ms = nowMs() - t0;
      const a = r && typeof r.middle === "string" ? assessRestatement(restatePlan.question, r.middle)
        : { text: null, reason: r?.error || "error" };
      log.restate_used = a.text !== null;
      log.restate_reject = a.reason;
      turn.text(a.text || OPENER);
    })();
  } else {
    turn.text(OPENER);  // before any await: ElevenLabs drops a turn after ~4 s of silence
  }

  const turnAbort = new AbortController();
  const guard = setTimeout(() => { log.outcome = "turn_guard"; turnAbort.abort(); turn.finish(); }, TURN_GUARD_MS);
  try {
    // Inside the try: a throw here must still give FALLBACK and [DONE].
    const { question, history } = toHistory(body.messages);
    log.history_turns = history.length;
    log.retry_suspected = retrySuspected(text, Array.isArray(body.messages) ? body.messages.length : 0, tArrive);
    const sub = verifyVoiceSession(extra.voice_session, key, nowSeconds());
    if (!sub) { log.outcome = "bad_token"; return turn.fail(FALLBACK); }
    log.sub_hash = createHmac("sha256", key).update(sub).digest("hex").slice(0, 12);

    const tInvoke = nowMs();
    let result;
    try {
      result = parseAskResult(await invokeWithTimeout(invokeAsk,
        { question, history, caller_sub: sub, mode: "voice", prompt_only: true, deadline_s: 8, tz: extra.tz },
        INVOKE_TIMEOUT_MS));
    } catch (e) {
      log.invoke_ms = nowMs() - tInvoke;
      log.outcome = "invoke_failed";
      if (e instanceof InvokeTimeout) log.invoke_abandoned = true;
      await opened;  // the fallback must follow the restatement, not precede it
      return turn.fail(FALLBACK);
    }
    log.invoke_ms = nowMs() - tInvoke;
    await opened;  // answer text only after the opener or restatement is out
    if (!result) { log.outcome = "invoke_failed"; return turn.fail(FALLBACK); }
    log.kind = result.kind;
    log.rewritten = result.rewritten ?? null;
    log.chunk_ids = Array.isArray(result.chunk_ids) ? result.chunk_ids : [];

    if (result.kind === "answer") {
      const answer = String(result.answer ?? "").trim();
      log.outcome = answer ? "answered" : "empty_answer";
      log.answer_chars = answer.length;
      turn.text(answer || FALLBACK);
      return turn.finish();
    }
    if (result.kind === "prompt" && typeof result.prompt === "string" && result.prompt && result.llm) {
      const attempt = (firstTokenMs) => answerStream({ prompt: result.prompt, llm: result.llm, apiKey, fetchImpl,
        baseUrl: process.env.OPENROUTER_BASE_URL || DEFAULT_BASE_URL, firstTokenMs,
        signal: turnAbort.signal, onText: (t) => turn.text(t) });
      let r = await attempt(FIRST_TOKEN_MS);
      log.answer_attempts = 1;
      // Retry only when nothing was spoken, the failure is a stall or a 5xx
      // (never 4xx or an abort), and the turn guard has not fired.
      if (r.chars === 0 && retryable(r.error) && !turnAbort.signal.aborted) {
        log.first_attempt_error = r.error;
        r = await attempt(RETRY_FIRST_TOKEN_MS);
        log.answer_attempts = 2;
      }
      log.answer_chars = r.chars;
      log.first_answer_token_ms = r.firstTokenMs;
      if (r.chars > 0) {
        if (log.outcome !== "turn_guard") log.outcome = r.truncated ? "answered_truncated" : "answered";
        return turn.finish();
      }
      if (log.outcome !== "turn_guard") log.outcome = "answer_failed";
      log.error = r.error || null;
      return turn.fail(FALLBACK);
    }
    log.outcome = "invoke_failed";
    return turn.fail(FALLBACK);
  } catch {
    log.outcome = "error";
    await opened;
    turn.fail(FALLBACK);
  } finally {
    clearTimeout(guard);
    turn.finish();
    log.total_ms = nowMs() - tArrive;
    console.log(JSON.stringify(log));  // no question or answer text, ever
  }
}

// Shell-phase timing probes (hold/delay), reachable only with VOICE_LLM_PROBES=true.
async function probeTurn(responseStream, body, probe, tArrive) {
  const id = `chatcmpl-shell-${tArrive}`;
  const created = Math.floor(tArrive / 1000);
  const out = globalThis.awslambda.HttpResponseStream.from(responseStream, {
    statusCode: 200,
    headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" },
  });

  const marks = {};
  if (probe.kind === "hold") await sleep(probe.seconds * 1000);
  let restatement = probe.flags.includes("l") ? LONG_RESTATEMENT
    : probe.flags.includes("p") ? PERIOD_RESTATEMENT : RESTATEMENT;
  if (probe.flags.includes("n")) restatement += "\n";
  // w: "Got it, " padded with spaces past the ~120-character threshold the `l`
  // probe found (141 chars spoken at 1.1 s; 27 chars held until the answer).
  if (probe.flags.includes("w")) restatement = "Got it, " + " ".repeat(130);
  out.write(chunk(id, created, restatement, { first: true }));
  marks.restatement_ms = Date.now() - tArrive;

  if (probe.kind === "delay") await gap(out, id, created, probe.seconds * 1000, probe.flags);

  for (const part of ANSWER.split(/(?<=\. )/)) {
    out.write(chunk(id, created, part));
    await sleep(40);
  }
  marks.answer_ms = Date.now() - tArrive;
  out.write(chunk(id, created, "", { finish: "stop" }));
  out.write("data: [DONE]\n\n");
  out.end();

  console.log(JSON.stringify({
    evt: "voice_llm_turn",
    probe,
    turns: Array.isArray(body.messages) ? body.messages.length : 0,
    stream: body.stream === true,
    model_requested: body.model || null,
    extra_body_keys: Object.keys(body.elevenlabs_extra_body || {}),
    ...marks,
  }));
}

// `awslambda` exists only inside the Lambda runtime; under a local test the
// handler is exported unwrapped so it can be driven with a fake stream.
// Lambda passes its context as a third argument, so the wrapper drops it:
// `deps` is for tests only.
export const handler = globalThis.awslambda?.streamifyResponse
  ? globalThis.awslambda.streamifyResponse((event, stream) => handle(event, stream))
  : handle;
