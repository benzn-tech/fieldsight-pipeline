// voice_llm/stream.test.mjs
import { test, mock } from "node:test";
import assert from "node:assert/strict";
import { createTurnStream, OPENER, FALLBACK } from "./stream.mjs";

const sink = () => { const w = []; return { w, write: (s) => w.push(s), end: () => w.push("<END>") }; };
const contents = (w) => w.filter((s) => s.startsWith("data: {")).map((s) => JSON.parse(s.slice(6)).choices[0].delta.content ?? null);

test("keep-alive writes a space after each idle second, and stops at finish", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  const o = sink(); const t = createTurnStream(o, { id: "x", created: 1 });
  t.text("opener ");
  mock.timers.tick(999); assert.deepEqual(contents(o.w), ["opener "]);
  mock.timers.tick(1);   assert.deepEqual(contents(o.w), ["opener ", " "]);
  mock.timers.tick(1000); assert.equal(contents(o.w).length, 3);
  t.finish();
  mock.timers.tick(5000);
  assert.equal(o.w.at(-1), "<END>");
  assert.equal(o.w.at(-2), "data: [DONE]\n\n");
  assert.equal(contents(o.w).filter((c) => c === " ").length, 2, "no keep-alive after finish");
  mock.timers.reset();
});

test("a stall mid-answer is bridged (Review Focus 3)", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  const o = sink(); const t = createTurnStream(o, { id: "x", created: 1 });
  t.text("The answer starts");
  // provider stalls 3.5 s; step the clock, a single big tick fires a re-armed timer only once
  for (let i = 0; i < 3; i++) mock.timers.tick(1000);
  mock.timers.tick(500);
  t.text(" and continues.");
  assert.ok(contents(o.w).filter((c) => c === " ").length >= 3);
  t.finish(); mock.timers.reset();
});

test("first chunk carries the assistant role; finish is idempotent", () => {
  const o = sink(); const t = createTurnStream(o, { id: "x", created: 1, setTimer: () => 0, clearTimer: () => {} });
  t.text("a"); t.text("b"); t.finish(); t.finish();
  const chunks = o.w.filter((s) => s.startsWith("data: {")).map((s) => JSON.parse(s.slice(6)));
  assert.equal(chunks[0].choices[0].delta.role, "assistant");
  assert.equal(chunks[1].choices[0].delta.role, undefined);
  assert.equal(o.w.filter((s) => s === "data: [DONE]\n\n").length, 1);
});

test("fail speaks the line then ends", () => {
  const o = sink(); const t = createTurnStream(o, { id: "x", created: 1, setTimer: () => 0, clearTimer: () => {} });
  t.fail("Sorry.");
  assert.deepEqual(contents(o.w).slice(0, 1), ["Sorry."]);
  assert.equal(o.w.at(-1), "<END>");
});

test("OPENER is long enough to be voiced before more text arrives, and trails '... '", () => {
  assert.ok(OPENER.length >= 141, `OPENER is ${OPENER.length} chars`);
  assert.ok(OPENER.endsWith("... "));
});

test("FALLBACK is the exact apology line", () => {
  assert.equal(FALLBACK, "Sorry, I couldn't reach your site records just now. Please try again.");
});
