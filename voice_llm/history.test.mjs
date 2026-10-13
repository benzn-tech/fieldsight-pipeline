import { test } from "node:test";
import assert from "node:assert/strict";
import { toHistory } from "./history.mjs";
import { OPENER } from "./stream.mjs";

test("pairs prior turns and strips our opener and keep-alive spaces", () => {
  const r = toHistory([
    { role: "system", content: "sys" },
    { role: "user", content: "when is the backfill due" },
    { role: "assistant", content: OPENER + "   Friday, before the digger goes back.  " },
    { role: "user", content: "who is doing it" },
  ]);
  assert.equal(r.question, "who is doing it");
  assert.deepEqual(r.history, [{ question: "when is the backfill due", answer: "Friday, before the digger goes back." }]);
});

test("tolerates whitespace normalised around the ellipsis", () => {
  const r = toHistory([
    { role: "user", content: "q1" },
    { role: "assistant", content: "Got it, let me check that...Friday." },
    { role: "user", content: "q2" },
  ]);
  assert.deepEqual(r.history, [{ question: "q1", answer: "Friday." }]);
});

test("an assistant turn that was only the opener is dropped with its question", () => {
  const r = toHistory([
    { role: "user", content: "q1" }, { role: "assistant", content: OPENER + "     " },
    { role: "user", content: "q2" },
  ]);
  assert.deepEqual(r.history, []);
});

test("a leading assistant greeting with no user turn is ignored", () => {
  const r = toHistory([{ role: "assistant", content: "Hello" }, { role: "user", content: "q" }]);
  assert.deepEqual(r, { question: "q", history: [] });
});

test("array content parts are joined", () => {
  const r = toHistory([{ role: "user", content: [{ type: "text", text: "part one" }, { type: "text", text: "two" }] }]);
  assert.equal(r.question, "part one two");
});

test("an assistant turn with no ellipsis keeps its whole text", () => {
  const r = toHistory([
    { role: "user", content: "q1" }, { role: "assistant", content: "  Friday.  " },
    { role: "user", content: "q2" },
  ]);
  assert.deepEqual(r.history, [{ question: "q1", answer: "Friday." }]);
});
