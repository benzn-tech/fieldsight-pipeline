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
  assert.deepEqual(mod.parseProbe("please Delay 5 thanks"), { kind: "delay", seconds: 5, flags: [] });
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
