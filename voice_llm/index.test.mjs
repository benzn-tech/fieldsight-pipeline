// node --test voice_llm/   (no dependencies)
//
// Drives the real handler with a fake Lambda response stream and parses what
// ElevenLabs would receive. The contract being pinned is ElevenLabs' own
// (custom-llm docs, read 2026-10-13): `data: {json}\n\n` chunks of
// chat.completion.chunk, text in choices[0].delta.content, terminated by
// `data: [DONE]\n\n`, served as text/event-stream.
import { test } from "node:test";
import assert from "node:assert/strict";

process.env.VOICE_LLM_BEARER = "test-bearer";
process.env.DEFAULT_ANSWER_DELAY_MS = "0";

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

test("streams the restatement first, then the answer, then [DONE]", async () => {
  const s = new FakeStream();
  await mod.handler(event(), s);

  assert.equal(s.meta.statusCode, 200);
  assert.equal(s.meta.headers["Content-Type"], "text/event-stream");
  assert.ok(s.ended);

  const events = sseEvents(s.text());
  assert.equal(events.at(-1), "[DONE]");
  const chunks = events.slice(0, -1).map((e) => JSON.parse(e));
  for (const c of chunks) assert.equal(c.object, "chat.completion.chunk");

  const content = chunks.map((c) => c.choices[0].delta.content || "");
  assert.equal(content[0], mod.RESTATEMENT, "the restatement must be the very first text");
  assert.equal(chunks[0].choices[0].delta.role, "assistant");
  assert.equal(content.join(""), mod.RESTATEMENT + mod.ANSWER);
  assert.equal(chunks.at(-1).choices[0].finish_reason, "stop");
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
    await mod.handler(ev, s);
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
