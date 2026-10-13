import { test } from "node:test";
import assert from "node:assert/strict";
import { streamAnswer } from "./answer.mjs";

const sse = (lines, delayMs = 0) => new Response(new ReadableStream({
  async start(c) { for (const l of lines) { if (delayMs) await new Promise((r) => setTimeout(r, delayMs)); c.enqueue(new TextEncoder().encode(l)); } c.close(); },
}), { status: 200, headers: { "content-type": "text/event-stream" } });
const d = (t) => `data: ${JSON.stringify({ choices: [{ delta: { content: t } }] })}\n\n`;
const base = { prompt: "P", llm: { model: "m" }, baseUrl: "u", apiKey: "k" };

test("sends exactly one user message and the resolved request, forwards deltas", async () => {
  let sent; const got = [];
  const r = await streamAnswer({ prompt: "P", llm: { model: "m", reasoning: { effort: "low" }, max_tokens: 900, temperature: 0 },
    baseUrl: "https://x/api/v1", apiKey: "k",
    fetchImpl: async (url, init) => { sent = { url, body: JSON.parse(init.body), auth: init.headers.Authorization }; return sse([d("Fri"), d("day."), "data: [DONE]\n\n"]); },
    onText: (t) => got.push(t) });
  assert.equal(sent.url, "https://x/api/v1/chat/completions");
  assert.deepEqual(sent.body.messages, [{ role: "user", content: "P" }]);
  assert.equal(sent.body.model, "m"); assert.deepEqual(sent.body.reasoning, { effort: "low" });
  assert.equal(sent.body.max_tokens, 900); assert.equal(sent.body.temperature, 0);
  assert.equal(sent.body.stream, true); assert.equal(sent.auth, "Bearer k");
  assert.deepEqual(got, ["Fri", "day."]); assert.equal(r.ok, true); assert.equal(r.chars, 7);
});

test("no content inside the first-token budget is a timeout, not a hang", async () => {
  const r = await streamAnswer({ ...base, firstTokenMs: 50,
    fetchImpl: async (u, init) => new Promise((_, rej) => init.signal.addEventListener("abort", () => rej(new Error("aborted")))),
    onText: () => {} });
  assert.equal(r.ok, false); assert.equal(r.error, "first_token_timeout");
});

test("an HTTP error is reported, nothing spoken", async () => {
  const got = [];
  const r = await streamAnswer({ ...base,
    fetchImpl: async () => new Response("nope", { status: 502 }), onText: (t) => got.push(t) });
  assert.equal(r.ok, false); assert.equal(r.error, "http_502"); assert.deepEqual(got, []);
});

test("a delta split across network chunks is reassembled", async () => {
  const got = [];
  const full = d("Hello world");
  await streamAnswer({ ...base,
    fetchImpl: async () => sse([full.slice(0, 15), full.slice(15), "data: [DONE]\n\n"]), onText: (t) => got.push(t) });
  assert.deepEqual(got, ["Hello world"]);
});

test("CRLF event separators are tolerated, even when CR and LF arrive in different chunks", async () => {
  const got = [];
  const crlf = d("Hi there").replace(/\n/g, "\r\n");
  const r = await streamAnswer({ ...base,
    fetchImpl: async () => sse([crlf.slice(0, -3), "\r", "\n", crlf.slice(-1), d("!").replace(/\n/g, "\r\n"), "data: [DONE]\r\n\r\n"]),
    onText: (t) => got.push(t) });
  assert.deepEqual(got, ["Hi there", "!"]); assert.equal(r.ok, true);
});

test("data: without a space is accepted", async () => {
  const got = [];
  const r = await streamAnswer({ ...base,
    fetchImpl: async () => sse([`data:${JSON.stringify({ choices: [{ delta: { content: "ok" } }] })}\n\n`, "data:[DONE]\n\n"]),
    onText: (t) => got.push(t) });
  assert.deepEqual(got, ["ok"]); assert.equal(r.ok, true);
});

test("SSE comment lines are ignored", async () => {
  const got = [];
  const r = await streamAnswer({ ...base,
    fetchImpl: async () => sse([": OPENROUTER PROCESSING\n\n", d("a"), ": keepalive\n\n", d("b"), "data: [DONE]\n\n"]),
    onText: (t) => got.push(t) });
  assert.deepEqual(got, ["a", "b"]); assert.equal(r.ok, true);
});

test("a mid-stream error object with nothing spoken ends the stream with ok:false", async () => {
  const got = [];
  const r = await streamAnswer({ ...base,
    fetchImpl: async () => sse([`data: ${JSON.stringify({ error: { code: 502, message: "upstream" } })}\n\n`, d("never")]),
    onText: (t) => got.push(t) });
  assert.equal(r.ok, false); assert.deepEqual(got, []); assert.match(r.error, /stream_error/);
});

test("a mid-stream error after speech keeps what was spoken and stops reading", async () => {
  const got = [];
  const r = await streamAnswer({ ...base,
    fetchImpl: async () => sse([d("Part"), `data: ${JSON.stringify({ error: { message: "x" } })}\n\n`, d("never")]),
    onText: (t) => got.push(t) });
  assert.deepEqual(got, ["Part"]); assert.equal(r.ok, true); assert.equal(r.chars, 4);
});

test("a caller abort stops the stream, resolves aborted, and speaks no more", async () => {
  const ac = new AbortController(); const got = [];
  const r = await streamAnswer({ ...base, signal: ac.signal,
    fetchImpl: async () => sse([d("one"), d("two"), d("three")], 40),
    onText: (t) => { got.push(t); if (got.length === 1) ac.abort(); } });
  assert.equal(r.ok, false); assert.equal(r.error, "aborted"); assert.deepEqual(got, ["one"]);
});

test("a caller signal already aborted never calls fetch", async () => {
  const ac = new AbortController(); ac.abort(); let called = false;
  const r = await streamAnswer({ ...base, signal: ac.signal,
    fetchImpl: async () => { called = true; return sse([d("x")]); }, onText: () => {} });
  assert.equal(r.error, "aborted"); assert.equal(called, false);
});

test("a caller abort during the fetch is reported as aborted, not a first-token timeout", async () => {
  const ac = new AbortController();
  setTimeout(() => ac.abort(), 20);
  const r = await streamAnswer({ ...base, signal: ac.signal, firstTokenMs: 5000,
    fetchImpl: async (u, init) => new Promise((_, rej) => init.signal.addEventListener("abort", () => rej(new Error("x")))),
    onText: () => {} });
  assert.equal(r.error, "aborted");
});
