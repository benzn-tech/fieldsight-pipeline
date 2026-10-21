// node --test voice_llm/   (no dependencies)
//
// Drives the real handler with a fake Lambda response stream and parses what
// ElevenLabs would receive. The contract being pinned is ElevenLabs' own
// (custom-llm docs, read 2026-10-13): `data: {json}\n\n` chunks of
// chat.completion.chunk, text in choices[0].delta.content, terminated by
// `data: [DONE]\n\n`, served as text/event-stream.
import { test, mock } from "node:test";
import { readFileSync } from "node:fs";
import { OPENER, FALLBACK } from "./stream.mjs";
import assert from "node:assert/strict";

process.env.VOICE_LLM_BEARER = "test-bearer";
const F = JSON.parse(readFileSync(new URL("../tests/fixtures/voice_session_token.json", import.meta.url), "utf8"));
process.env.VOICE_SESSION_SIGNING_KEY = F.key;
process.env.ASK_AGENT_FUNCTION = "ask-fn";
process.env.OPENROUTER_API_KEY = "or-key";
process.env.OPENROUTER_BASE_URL = "http://openrouter.test/v1";

class FakeStream {
  constructor() { this.parts = []; this.ended = false; this.meta = null; }
  write(s) { this.parts.push(s); }
  end() { this.ended = true; }
  text() { return this.parts.join(""); }
}
globalThis.awslambda = {
  HttpResponseStream: { from(stream, meta) { stream.meta = meta; return stream; } },
};

const mod = await import("./index.mjs");

function event({ auth = "Bearer test-bearer", path = "/v1/chat/completions", method = "POST", body } = {}) {
  return {
    rawPath: path,
    requestContext: { http: { method } },
    headers: auth ? { authorization: auth } : {},
    body: JSON.stringify(body ?? { model: "x", stream: true, messages: [{ role: "user", content: "hello" }] }),
    isBase64Encoded: false,
  };
}

function sseEvents(text) {
  return text.split("\n\n").filter(Boolean).map((block) => {
    assert.ok(block.startsWith("data: "), `every block must be a data: line, got ${JSON.stringify(block)}`);
    return block.slice(6);
  });
}

const Q = "when is the backfill due";
function turnEvent({ token = F.token, messages, extra } = {}) {
  return event({ body: { model: "x", stream: true,
    messages: messages || [{ role: "user", content: Q }],
    elevenlabs_extra_body: { voice_session: token, tz: "Pacific/Auckland", ...(extra || {}) } } });
}
// Default deps: token clock pinned to the fixture, an Ask that must not be reached.
function deps(over = {}) {
  const calls = [];
  const d = { now: () => F.now, nowMs: Date.now, calls,
    invokeAsk: async (payload) => { calls.push(payload); throw new Error("unexpected invoke"); },
    ...over };
  return d;
}
const askReply = (body, statusCode = 200) => ({ statusCode, body: JSON.stringify(body) });
const fakeFetch = (deltas, { status = 200 } = {}) => async () => ({
  ok: status === 200, status,
  body: (async function* () {
    for (const d of deltas) yield new TextEncoder().encode(`data: ${JSON.stringify({ choices: [{ delta: { content: d } }] })}\n\n`);
    yield new TextEncoder().encode("data: [DONE]\n\n");
  })() });
const PROMPT = { kind: "prompt", prompt: "P", llm: { model: "m", max_tokens: 50 }, rewritten: true, chunk_ids: ["c1", "c2"] };

async function run(ev, d) {
  const s = new FakeStream();
  const lines = []; const orig = console.log; console.log = (l) => lines.push(l);
  try { await mod.handler(ev, s, d); } finally { console.log = orig; }
  const events = sseEvents(s.text());
  const content = events.slice(0, -1).map((e) => JSON.parse(e).choices[0].delta.content || "");
  const log = lines.map((l) => { try { return JSON.parse(l); } catch { return {}; } }).find((l) => l.evt === "voice_llm_turn");
  return { s, events, content, spoken: content.join(""), log, lines };
}

test("a bad token gets the opener then the fallback, and Ask is never invoked", async () => {
  const d = deps();
  for (const token of ["garbage", null, F.token.slice(0, -2) + "xx"]) {
    const r = await run(turnEvent({ token }), d);
    assert.equal(r.s.meta.statusCode, 200);
    assert.equal(r.content[0], OPENER);
    assert.equal(r.spoken.trim(), (OPENER + FALLBACK).trim());
    assert.equal(r.events.at(-1), "[DONE]");
    assert.equal(r.log.outcome, "bad_token");
    assert.equal(r.log.sub_hash, null);
  }
  assert.equal(d.calls.length, 0, "no invoke before the token verifies");
});

test("kind:answer is spoken verbatim after the opener", async () => {
  const msg = "No relevant records found for this question.";
  const d = deps({ invokeAsk: async () => askReply({ kind: "answer", answer: msg }) });
  const r = await run(turnEvent(), d);
  assert.deepEqual(r.content.filter(Boolean), [OPENER, msg]);
  assert.equal(r.events.at(-1), "[DONE]");
  assert.equal(r.log.outcome, "answered");
});

test("an empty or non-string answer becomes the fallback", async () => {
  for (const answer of ["", "   ", null, undefined]) {
    const d = deps({ invokeAsk: async () => askReply({ kind: "answer", answer }) });
    const r = await run(turnEvent(), d);
    assert.deepEqual(r.content.filter(Boolean), [OPENER, FALLBACK], String(answer));
  }
});

test("kind:prompt streams the model's deltas after the opener, then [DONE]", async () => {
  const seen = [];
  const d = deps({
    invokeAsk: async (p) => { seen.push(p); return askReply(PROMPT); },
    fetchImpl: fakeFetch(["The backfill ", "is due Friday."]),
  });
  const messages = [
    { role: "user", content: "who is on site" },
    { role: "assistant", content: OPENER + "Bob is on site." },
    { role: "user", content: Q },
  ];
  const r = await run(turnEvent({ messages, extra: { caller_sub: "attacker", history: [{ question: "x", answer: "y" }] } }), d);
  assert.deepEqual(r.content.filter(Boolean), [OPENER, "The backfill ", "is due Friday."]);
  assert.equal(r.events.at(-1), "[DONE]");
  assert.equal(seen.length, 1);
  assert.equal(seen[0].prompt_only, true);
  assert.equal(seen[0].mode, "voice");
  assert.equal(seen[0].caller_sub, F.sub, "identity is the verified sub, never the client's");
  assert.equal(seen[0].question, Q);
  assert.deepEqual(seen[0].history, [{ question: "who is on site", answer: "Bob is on site." }]);
  assert.equal(seen[0].tz, "Pacific/Auckland");
  assert.equal(seen[0].deadline_s, 8);
  assert.equal(r.log.outcome, "answered");
  assert.equal(r.log.kind, "prompt");
  assert.equal(r.log.rewritten, true);
  assert.deepEqual(r.log.chunk_ids, ["c1", "c2"]);
  assert.equal(r.log.history_turns, 1);
  assert.equal(r.log.answer_chars, "The backfill is due Friday.".length);
});

test("an invoke that never resolves ends in the fallback within 9 s", async () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const d = deps({ invokeAsk: () => new Promise(() => {}) });
    const p = run(turnEvent(), d);
    for (let i = 0; i < 20; i++) { await Promise.resolve(); mock.timers.tick(1000); await Promise.resolve(); }
    const r = await p;
    assert.equal(r.spoken.endsWith(FALLBACK), true);
    assert.equal(r.events.at(-1), "[DONE]");
    assert.equal(r.log.outcome, "invoke_failed");
    assert.equal(r.log.invoke_abandoned, true);
    assert.ok(r.content.filter((c) => c === " ").length >= 7, "keep-alive spaces flowed while waiting");
  } finally { mock.timers.reset(); }
});

test("an Ask error status, a body without kind, or a thrown invoke is the fallback", async () => {
  const cases = {
    "statusCode 500": async () => askReply({ error: "boom" }, 500),
    "body without kind": async () => askReply({ answer: "hi" }),
    "unparseable body": async () => ({ statusCode: 200, body: "<html>" }),
    "invoke throws": async () => { throw new Error("FunctionError"); },
  };
  for (const [name, invokeAsk] of Object.entries(cases)) {
    const r = await run(turnEvent(), deps({ invokeAsk }));
    assert.deepEqual(r.content.filter(Boolean), [OPENER, FALLBACK], name);
    assert.equal(r.events.at(-1), "[DONE]", name);
    assert.equal(r.log.outcome, "invoke_failed", name);
  }
});

test("a model that speaks nothing is the fallback; a truncated answer is not followed by one", async () => {
  const empty = await run(turnEvent(), deps({ invokeAsk: async () => askReply(PROMPT), fetchImpl: fakeFetch([]) }));
  assert.deepEqual(empty.content.filter(Boolean), [OPENER, FALLBACK]);
  assert.equal(empty.log.outcome, "answer_failed");

  const cut = async () => ({ ok: true, status: 200, body: (async function* () {
    yield new TextEncoder().encode(`data: ${JSON.stringify({ choices: [{ delta: { content: "Half an" } }] })}\n\n`);
    throw new Error("socket reset");
  })() });
  const r = await run(turnEvent(), deps({ invokeAsk: async () => askReply(PROMPT), fetchImpl: cut }));
  assert.deepEqual(r.content.filter(Boolean), [OPENER, "Half an"]);
  assert.equal(r.events.at(-1), "[DONE]");
  assert.equal(r.log.outcome, "answered_truncated");
});

test("the turn log carries a 12-hex sub_hash and none of the question or answer text", async () => {
  const d = deps({ invokeAsk: async () => askReply({ kind: "answer", answer: "Secret answer text." }) });
  const r = await run(turnEvent(), d);
  assert.match(r.log.sub_hash, /^[0-9a-f]{12}$/);
  for (const line of r.lines) {
    assert.ok(!String(line).includes(Q), "question text reached the log");
    assert.ok(!String(line).includes("Secret answer"), "answer text reached the log");
    assert.ok(!String(line).includes(F.sub), "raw sub reached the log");
  }
  assert.ok(!("user_text" in r.log));
});

test("retry_suspected: the same turn re-sent within 10 s is flagged, a new one is not", async () => {
  const d = deps({ invokeAsk: async () => askReply({ kind: "answer", answer: "ok" }) });
  const q = (t) => turnEvent({ messages: [{ role: "user", content: t }] });
  const a = await run(q("retry probe one"), d);
  const b = await run(q("retry probe one"), d);
  const c = await run(q("retry probe two"), d);
  assert.deepEqual([a.log.retry_suspected, b.log.retry_suspected, c.log.retry_suspected], [false, true, false]);
});

test("missing configuration is a 503 naming what is missing, with no stream opened", async () => {
  const saved = process.env.OPENROUTER_API_KEY;
  delete process.env.OPENROUTER_API_KEY;
  const lines = []; const orig = console.log; console.log = (l) => lines.push(l);
  try {
    const s = new FakeStream();
    await mod.handler(turnEvent(), s, deps());
    assert.equal(s.meta.statusCode, 503);
    assert.ok(!s.text().includes("data:"));
    assert.ok(lines.some((l) => String(l).includes("voice_llm_misconfigured") && String(l).includes("OPENROUTER_API_KEY")));
  } finally { console.log = orig; process.env.OPENROUTER_API_KEY = saved; }
});

test("a stream that throws on write cannot crash a turn", async () => {
  class Broken extends FakeStream { write() { throw new Error("destroyed"); } }
  const s = new Broken();
  await mod.handler(turnEvent(), s, deps({ invokeAsk: async () => askReply({ kind: "answer", answer: "x" }) }));
});

test("probes are inert unless VOICE_LLM_PROBES is true, and still work when it is", async () => {
  const probeEv = () => event({ body: { messages: [{ role: "user", content: "delay 0" }],
    elevenlabs_extra_body: { voice_session: F.token } } });
  const off = await run(probeEv(), deps());
  assert.equal(off.log.outcome, "invoke_failed", "without the flag a probe phrase is an ordinary question");
  assert.equal(off.log.probe, undefined);
  process.env.VOICE_LLM_PROBES = "true";
  try {
    const on = await run(probeEv(), deps());
    assert.equal(on.content[0], mod.RESTATEMENT);
    assert.ok(on.spoken.includes(mod.ANSWER));
    assert.deepEqual(on.log.probe.kind, "delay");
  } finally { delete process.env.VOICE_LLM_PROBES; }
});

test("the restatement ends with an ellipsis AND a space", () => {
  // ElevenLabs' buffer-words form. Their docs call the trailing space
  // crucial: without it the answer is glued onto the ellipsis and distorts.
  assert.ok(mod.RESTATEMENT.endsWith("... "), JSON.stringify(mod.RESTATEMENT));
});

test("a wrong or missing bearer is refused before any stream opens", async () => {
  for (const auth of ["Bearer nope", "", "test-bearer"]) {
    const s = new FakeStream();
    await mod.handler(event({ auth }), s);
    assert.equal(s.meta.statusCode, 401, `auth=${JSON.stringify(auth)}`);
    assert.ok(!s.text().includes("data:"), "no SSE may leak to an unauthorised caller");
  }
});

test("only POST /…/chat/completions is served", async () => {
  const s = new FakeStream();
  await mod.handler(event({ path: "/v1/responses" }), s);
  assert.equal(s.meta.statusCode, 404);
});

test("a malformed body is a 400, not a crash", async () => {
  const s = new FakeStream();
  const ev = event();
  ev.body = "{not json";
  await mod.handler(ev, s);
  assert.equal(s.meta.statusCode, 400);
});

test("probes are parsed from the last user message only", () => {
  assert.deepEqual(mod.parseProbe("hold 20"), { kind: "hold", seconds: 20, flags: [] });
  assert.deepEqual(mod.parseProbe("please Delay 5 thanks"), { kind: "normal", seconds: 0, flags: [] }, "anchored to the whole message");
  assert.deepEqual(mod.parseProbe("Delay 5"), { kind: "delay", seconds: 5, flags: [] });
  assert.deepEqual(mod.parseProbe("hold 999"), { kind: "hold", seconds: 60, flags: [] }, "capped");
  assert.deepEqual(mod.parseProbe("what is the backfill date"), { kind: "normal", seconds: 0, flags: [] });
  assert.deepEqual(mod.parseProbe("delay 8 ka p"), { kind: "delay", seconds: 8, flags: ["ka", "p"] });

  const body = { messages: [
    { role: "user", content: "hold 30" },
    { role: "assistant", content: "..." },
    { role: "user", content: "normal question" },
  ] };
  assert.equal(mod.lastUserText(body), "normal question");
});

test("the custom key header is accepted, in any case, because ElevenLabs reserves Authorization", async () => {
  for (const name of ["x-voice-llm-key", "X-Voice-Llm-Key"]) {
    const s = new FakeStream();
    const ev = event({ auth: "" });
    ev.headers = { [name]: "test-bearer" };
    await mod.handler(ev, s, deps());
    assert.equal(s.meta.statusCode, 200, name);
  }
  const s = new FakeStream();
  const ev = event({ auth: "" });
  ev.headers = { "x-voice-llm-key": "wrong" };
  await mod.handler(ev, s);
  assert.equal(s.meta.statusCode, 401);
});

test("a refusal logs which headers arrived but never their values", async () => {
  const lines = []; const orig = console.log; console.log = (l) => lines.push(l);
  try {
    const s = new FakeStream();
    const ev = event({ auth: "Bearer secret-value-that-must-not-leak" });
    await mod.handler(ev, s);
  } finally { console.log = orig; }
  const refused = lines.find((l) => String(l).includes("voice_llm_refused"));
  assert.ok(refused, "the refusal must be logged");
  assert.ok(JSON.parse(refused).header_names.includes("authorization"));
  assert.ok(!refused.includes("secret-value-that-must-not-leak"), "a credential reached the log");
});

test("the 30 s turn guard ends the stream and aborts the OpenRouter fetch", async () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    let aborted = false;
    // One delta (so the first-token timer is satisfied), then silence until aborted.
    const hang = async (url, { signal }) => ({ ok: true, status: 200, body: (async function* () {
      yield new TextEncoder().encode(`data: ${JSON.stringify({ choices: [{ delta: { content: "Start" } }] })}

`);
      await new Promise((_, rej) => signal.addEventListener("abort", () => { aborted = true; rej(new Error("aborted")); }));
    })() });
    const d = deps({ invokeAsk: async () => askReply(PROMPT), fetchImpl: hang });
    const p = run(turnEvent(), d);
    for (let i = 0; i < 40; i++) { await Promise.resolve(); mock.timers.tick(1000); await Promise.resolve(); }
    const r = await p;
    assert.equal(aborted, true, "fetch must be aborted by the guard");
    assert.equal(r.events.at(-1), "[DONE]");
    assert.equal(r.log.outcome, "turn_guard");
    assert.equal(r.spoken.replace(/ +/g, " ").trim(), (OPENER + "Start").replace(/ +/g, " ").trim(), "no fallback appended after speech");
  } finally { mock.timers.reset(); }
});

test("a natural sentence containing a probe phrase is a question, not a probe", () => {
  for (const s of ["what was the delay 3 days on the pour?", "did we hold 4 crews on level two", "delay 3 days"]) {
    assert.equal(mod.parseProbe(s).kind, "normal", s);
  }
  assert.deepEqual(mod.parseProbe("hold 4"), { kind: "hold", seconds: 4, flags: [] });
  assert.deepEqual(mod.parseProbe("  Delay 2 KA  "), { kind: "delay", seconds: 2, flags: ["ka"] });
});

test("a real question containing 'delay 3 days' still runs the real flow with probes on", async () => {
  process.env.VOICE_LLM_PROBES = "true";
  try {
    const d = deps({ invokeAsk: async () => askReply({ kind: "answer", answer: "Two days." }) });
    const r = await run(turnEvent({ messages: [{ role: "user", content: "what was the delay 3 days on the pour?" }] }), d);
    assert.equal(r.log.outcome, "answered");
    assert.equal(r.log.probe, undefined);
  } finally { delete process.env.VOICE_LLM_PROBES; }
});

test("authorised: same-length wrong, wrong-length, right, for both headers; empty bearer refuses all", () => {
  const ok = (h) => mod.authorised(h);
  assert.equal(ok({ authorization: "Bearer test-bearer" }), true);
  assert.equal(ok({ Authorization: "Bearer test-bearer" }), true);
  assert.equal(ok({ "x-voice-llm-key": "test-bearer" }), true);
  assert.equal(ok({ authorization: "Bearer test-bearex" }), false, "same length, wrong value");
  assert.equal(ok({ "x-voice-llm-key": "test-bearex" }), false, "same length, wrong value");
  assert.equal(ok({ authorization: "Bearer test-bearer-longer" }), false);
  assert.equal(ok({ "x-voice-llm-key": "test" }), false);
  assert.equal(ok({ authorization: "test-bearer" }), false, "no Bearer prefix");
  assert.equal(ok({}), false);
  assert.equal(ok(undefined), false);
  const saved = process.env.VOICE_LLM_BEARER;
  process.env.VOICE_LLM_BEARER = "";
  try {
    assert.equal(ok({ authorization: "Bearer " }), false);
    assert.equal(ok({ "x-voice-llm-key": "" }), false);
    assert.equal(ok({}), false);
  } finally { process.env.VOICE_LLM_BEARER = saved; }
});

test("a throw while preparing the turn (after the opener) still ends in FALLBACK and [DONE]", async () => {
  // retrySuspected keys its Map by a sha256 hex digest; make that write throw.
  const realSet = Map.prototype.set;
  Map.prototype.set = function (k, v) {
    if (typeof k === "string" && /^[0-9a-f]{64}$/.test(k)) throw new Error("boom");
    return realSet.call(this, k, v);
  };
  let r;
  try { r = await run(turnEvent(), deps()); } finally { Map.prototype.set = realSet; }
  assert.equal(r.events.at(-1), "[DONE]");
  assert.ok(r.spoken.includes(FALLBACK));
  assert.equal(r.log.outcome, "error");
});

// First-token retry. streamAnswer is injected (deps.streamAnswer) so each outcome
// is scripted exactly; the calls it records show what each attempt was given.
function scripted(results) {
  const calls = [];
  const fn = async (opts) => {
    calls.push({ firstTokenMs: opts.firstTokenMs, prompt: opts.prompt, llm: opts.llm, signal: opts.signal });
    const r = results[calls.length - 1];
    if (r.speak) opts.onText(r.speak);
    const { speak, ...res } = r;
    return res;
  };
  return { fn, calls };
}
const TIMEOUT = { ok: false, chars: 0, firstTokenMs: null, error: "first_token_timeout" };
const SPOKE = { speak: "Friday.", ok: true, chars: 7, firstTokenMs: 2100 };

test("a first-token timeout is retried once with the same request and a longer wait", async () => {
  const s = scripted([TIMEOUT, SPOKE]);
  const r = await run(turnEvent(), deps({ invokeAsk: async () => askReply(PROMPT), streamAnswer: s.fn }));
  assert.deepEqual(r.content.filter(Boolean), [OPENER, "Friday."]);
  assert.equal(r.spoken.includes(FALLBACK), false);
  assert.equal(r.log.outcome, "answered");
  assert.equal(r.log.answer_attempts, 2);
  assert.equal(r.log.first_attempt_error, "first_token_timeout");
  assert.deepEqual(s.calls.map((c) => c.firstTokenMs), [5000, 8000]);
  assert.equal(s.calls[1].prompt, s.calls[0].prompt);
  assert.deepEqual(s.calls[1].llm, s.calls[0].llm);
  assert.equal(s.calls[1].signal, s.calls[0].signal, "both attempts share the turn abort signal");
});

test("an http_5xx on the first attempt is retried; a success needs no retry", async () => {
  const s = scripted([{ ok: false, chars: 0, firstTokenMs: null, error: "http_503" }, SPOKE]);
  const r = await run(turnEvent(), deps({ invokeAsk: async () => askReply(PROMPT), streamAnswer: s.fn }));
  assert.equal(r.log.outcome, "answered");
  assert.equal(r.log.answer_attempts, 2);
  assert.equal(r.log.first_attempt_error, "http_503");

  const ok = scripted([SPOKE]);
  const r1 = await run(turnEvent(), deps({ invokeAsk: async () => askReply(PROMPT), streamAnswer: ok.fn }));
  assert.equal(r1.log.answer_attempts, 1);
  assert.equal(r1.log.first_attempt_error ?? null, null);
  assert.deepEqual(ok.calls.map((c) => c.firstTokenMs), [5000]);
});

test("two first-token timeouts end in the fallback after exactly two attempts", async () => {
  const s = scripted([TIMEOUT, TIMEOUT]);
  const r = await run(turnEvent(), deps({ invokeAsk: async () => askReply(PROMPT), streamAnswer: s.fn }));
  assert.deepEqual(r.content.filter(Boolean), [OPENER, FALLBACK]);
  assert.equal(r.log.outcome, "answer_failed");
  assert.equal(r.log.answer_attempts, 2);
  assert.equal(r.log.first_attempt_error, "first_token_timeout");
  assert.equal(r.log.error, "first_token_timeout");
  assert.equal(s.calls.length, 2);
});

test("text followed by an error is never retried", async () => {
  const s = scripted([{ speak: "Half an", ok: true, chars: 7, firstTokenMs: 900, error: "socket reset", truncated: true }, SPOKE]);
  const r = await run(turnEvent(), deps({ invokeAsk: async () => askReply(PROMPT), streamAnswer: s.fn }));
  assert.deepEqual(r.content.filter(Boolean), [OPENER, "Half an"]);
  assert.equal(r.log.outcome, "answered_truncated");
  assert.equal(r.log.answer_attempts, 1);
  assert.equal(s.calls.length, 1);
});

test("a 4xx is not retried and gives the fallback", async () => {
  const s = scripted([{ ok: false, chars: 0, firstTokenMs: null, error: "http_401" }, SPOKE]);
  const r = await run(turnEvent(), deps({ invokeAsk: async () => askReply(PROMPT), streamAnswer: s.fn }));
  assert.deepEqual(r.content.filter(Boolean), [OPENER, FALLBACK]);
  assert.equal(r.log.answer_attempts, 1);
  assert.equal(s.calls.length, 1);
});

test("an aborted attempt is not retried", async () => {
  const s = scripted([{ ok: false, chars: 0, firstTokenMs: null, error: "aborted" }, SPOKE]);
  const r = await run(turnEvent(), deps({ invokeAsk: async () => askReply(PROMPT), streamAnswer: s.fn }));
  assert.equal(r.log.answer_attempts, 1);
  assert.equal(s.calls.length, 1);
  assert.equal(r.spoken.includes("Friday."), false);
});

test("end to end through the real streamAnswer: HTTP 503 then a good stream is answered", async () => {
  let n = 0;
  const good = fakeFetch(["Back on ", "Friday."]);
  const flaky = async (...a) => (++n === 1 ? { ok: false, status: 503 } : good(...a));
  const r = await run(turnEvent(), deps({ invokeAsk: async () => askReply(PROMPT), fetchImpl: flaky }));
  assert.deepEqual(r.content.filter(Boolean), [OPENER, "Back on ", "Friday."]);
  assert.equal(r.log.answer_attempts, 2);
  assert.equal(r.log.first_attempt_error, "http_503");
});

// ---- Task 11: model restatement behind VOICE_RESTATE_ENABLED -------------------
const RQ = "when does the Unit 11 backfill have to be finished and who is doing the trench work";
const RGOOD = "you're asking when the Unit 11 backfill has to be finished and who is doing the trench work";
const RGOOD_TEXT = `Got it, ${RGOOD}, let me check your site records and pull up what was said on site... `;
async function withRestate(fn, env = {}) {
  const saved = { ...process.env };
  process.env.VOICE_RESTATE_ENABLED = "true";
  Object.assign(process.env, env);
  try { return await fn(); } finally {
    for (const k of ["VOICE_RESTATE_ENABLED", "VOICE_RESTATE_MODEL"]) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; }
  }
}
function restateDeps(middle, over = {}) {
  const rcalls = [];
  const d = deps({ invokeAsk: async () => askReply({ kind: "answer", answer: "Friday." }),
    restate: async (a) => { rcalls.push(a); return typeof middle === "function" ? middle(a) : { middle }; }, ...over });
  d.rcalls = rcalls;
  return d;
}
const rturn = (q = RQ, extra = {}) => turnEvent({ messages: [{ role: "user", content: q }], ...extra });

test("restate flag off: the opener is written synchronously and the model is never called", async () => {
  const d = restateDeps(RGOOD);
  const s = new FakeStream();
  const lines = []; const orig = console.log; console.log = (l) => lines.push(l);
  let p;
  try {
    p = mod.handle(rturn(), s, d);
    assert.ok(s.text().includes(OPENER), "opener written before the first await");
    await p;
  } finally { console.log = orig; }
  assert.equal(d.rcalls.length, 0);
  const log = lines.map((l) => { try { return JSON.parse(l); } catch { return {}; } }).find((l) => l.evt === "voice_llm_turn");
  assert.deepEqual([log.restate_used, log.restate_reject], [false, "disabled"]);
});

test("restate on, good result: a keep-alive space, then the assembled restatement, then the answer", async () => {
  await withRestate(async () => {
    const d = restateDeps(RGOOD);
    const r = await run(rturn(), d);
    assert.deepEqual(r.content.filter(Boolean), [" ", RGOOD_TEXT, "Friday."]);
    assert.equal(r.events.at(-1), "[DONE]");
    assert.equal(r.log.restate_used, true);
    assert.equal(r.log.restate_reject, null);
    assert.equal(typeof r.log.restate_ms, "number");
    assert.ok(!JSON.stringify(r.log).includes("backfill"), "no question text in the log");
    assert.equal(d.rcalls.length, 1);
    assert.equal(d.rcalls[0].question, RQ);
    assert.equal(d.rcalls[0].prevQuestion, undefined);
  });
});

test("restate gets the previous question, never an answer", async () => {
  await withRestate(async () => {
    const d = restateDeps(RGOOD);
    await run(rturn(RQ, { messages: [{ role: "user", content: "who is on site" }, { role: "assistant", content: OPENER + "Three crews." }, { role: "user", content: RQ }] }), d);
    assert.equal(d.rcalls[0].prevQuestion, "who is on site");
    assert.ok(!JSON.stringify(d.rcalls[0]).includes("Three crews"));
  });
});

test("a number the caller said is accepted; a number the model invented gives the opener", async () => {
  await withRestate(async () => {
    const ok = await run(rturn(), restateDeps(RGOOD));
    assert.equal(ok.content[1], RGOOD_TEXT);
    const bad = await run(rturn(), restateDeps(RGOOD.replace("Unit 11", "Unit 12")));
    assert.equal(bad.content[1], OPENER);
    assert.equal(bad.log.restate_used, false);
    assert.equal(bad.log.restate_reject, "guard_number");
  });
});

test("an invented capitalised name, a short result and a wrong lead all give the opener", async () => {
  await withRestate(async () => {
    const name = await run(rturn(), restateDeps(RGOOD.replace("trench work", "Hanlon trench work")));
    assert.deepEqual([name.content[1], name.log.restate_reject], [OPENER, "guard_name"]);
    const short = await run(rturn("about the pour"), restateDeps("you're asking about the pour"));
    assert.deepEqual([short.content[1], short.log.restate_reject], [OPENER, "too_short"]);
    const lead = await run(rturn(), restateDeps("the question is when the backfill is due"));
    assert.deepEqual([lead.content[1], lead.log.restate_reject], [OPENER, "guard_prefix"]);
  });
});

test("restate past the 1800 ms deadline gives the opener and aborts the model call", async () => {
  await withRestate(async () => {
    mock.timers.enable({ apis: ["setTimeout"] });
    try {
      let aborted = false;
      const d = restateDeps((a) => new Promise(() => { a.signal.addEventListener("abort", () => { aborted = true; }); }));
      const p = run(rturn(), d);
      for (let i = 0; i < 6; i++) { await Promise.resolve(); mock.timers.tick(500); await Promise.resolve(); }
      const r = await p;
      assert.equal(r.content.filter(Boolean)[0], " ");
      assert.ok(r.content.includes(OPENER), "opener after the deadline");
      assert.equal(aborted, true);
      assert.deepEqual([r.log.restate_used, r.log.restate_reject], [false, "deadline"]);
      assert.ok(r.events.at(-1) === "[DONE]");
    } finally { mock.timers.reset(); }
  });
});

test("a restate that fails (http or thrown) gives the opener", async () => {
  await withRestate(async () => {
    const h = await run(rturn(), restateDeps(() => ({ error: "http" })));
    assert.deepEqual([h.content[1], h.log.restate_reject], [OPENER, "http"]);
    const t = await run(rturn(), restateDeps(() => { throw new Error("boom"); }));
    assert.deepEqual([t.content[1], t.log.restate_reject], [OPENER, "error"]);
  });
});

test("a bad token under the restate flag keeps today's behaviour and never calls the model", async () => {
  await withRestate(async () => {
    const d = restateDeps(RGOOD);
    const r = await run(rturn(RQ, { token: "garbage" }), d);
    assert.equal(r.content[0], OPENER);
    assert.equal(r.spoken.trim(), (OPENER + FALLBACK).trim());
    assert.equal(r.log.outcome, "bad_token");
    assert.equal(d.rcalls.length, 0);
  });
});

test("correction turns and reasoning models skip the model call", async () => {
  await withRestate(async () => {
    const d = restateDeps(RGOOD);
    const c = await run(rturn("No, I meant the level three pour"), d);
    assert.deepEqual([c.content[0], c.log.restate_reject], [OPENER, "correction"]);
    assert.equal(d.rcalls.length, 0);
  });
  await withRestate(async () => {
    const d = restateDeps(RGOOD);
    const r = await run(rturn(), d);
    assert.deepEqual([r.content[0], r.log.restate_reject], [OPENER, "reasoning_model"]);
    assert.equal(d.rcalls.length, 0);
  }, { VOICE_RESTATE_MODEL: "deepseek/deepseek-r1" });
});

test("a failing Ask still waits for the restatement so the fallback never precedes it", async () => {
  await withRestate(async () => {
    const d = restateDeps(RGOOD, { invokeAsk: async () => { throw new Error("down"); } });
    const r = await run(rturn(), d);
    assert.deepEqual(r.content.filter(Boolean), [" ", RGOOD_TEXT, FALLBACK]);
  });
});

test("the default restate model call uses VOICE_RESTATE_MODEL and the OpenRouter settings", async () => {
  await withRestate(async () => {
    const seen = [];
    const d = deps({ invokeAsk: async () => askReply({ kind: "answer", answer: "Friday." }),
      fetchImpl: async (url, init) => { seen.push({ url, body: JSON.parse(init.body) });
        return { ok: true, status: 200, json: async () => ({ choices: [{ message: { content: RGOOD } }] }) }; } });
    const r = await run(rturn(), d);
    assert.equal(r.content[1], RGOOD_TEXT);
    assert.equal(seen[0].url, "http://openrouter.test/v1/chat/completions");
    assert.equal(seen[0].body.model, "meta-llama/llama-3.1-8b-instruct");
  });
  await withRestate(async () => {
    const seen = [];
    const d = deps({ invokeAsk: async () => askReply({ kind: "answer", answer: "Friday." }),
      fetchImpl: async (url, init) => { seen.push(JSON.parse(init.body)); return { ok: false, status: 500 }; } });
    await run(rturn(), d);
    assert.equal(seen[0].model, "acme/small");
  }, { VOICE_RESTATE_MODEL: "acme/small" });
});
