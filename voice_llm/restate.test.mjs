// node --test voice_llm/   (no dependencies)
import { test } from "node:test";
import assert from "node:assert/strict";
import { OPENER } from "./stream.mjs";
import { toHistory } from "./history.mjs";
import { guardMiddle, buildRestatement, assessRestatement, isCorrection, modelRejected,
  restateViaModel, RESTATE_SYSTEM } from "./restate.mjs";

const Q = "When does the Unit 11 backfill have to be finished and who is doing the Hanlon trench work";
const GOOD = "you're asking when the Unit 11 backfill has to be finished and who is doing the Hanlon trench work";

test("a faithful restatement passes the guard and is assembled with the fixed lead-in and tail", () => {
  assert.deepEqual(guardMiddle(Q, GOOD), { ok: true, middle: GOOD });
  const t = buildRestatement(Q, GOOD);
  assert.equal(t, `Got it, ${GOOD}, let me check your site records and pull up what was said on site... `);
  assert.ok(t.length >= 141);
});

test("numbers are allowed only when the question says them verbatim", () => {
  assert.equal(guardMiddle(Q, GOOD).ok, true);                       // "11" is in the question
  const r = guardMiddle(Q, GOOD.replace("Unit 11", "Unit 12"));
  assert.deepEqual([r.ok, r.reason], [false, "guard_number"]);
  assert.equal(guardMiddle("how long is the pour", "you're asking how long the pour will take in 3 hours").reason, "guard_number");
});

test("a capitalised word the question never used is an invented name", () => {
  const r = guardMiddle(Q, GOOD.replace("Hanlon", "Hanlon and Smith"));
  assert.deepEqual([r.ok, r.reason], [false, "guard_name"]);
  // The leading "you're" is not a name, and a capitalised word the caller did say is fine.
  assert.equal(guardMiddle("is hanlon on site", "you're asking whether Hanlon is on site").ok, true);
});

test("more than 25 words, or the wrong lead, or an ellipsis, is refused", () => {
  const long = "you're asking " + Array(30).fill("about").join(" ");
  assert.equal(guardMiddle(Q, long).reason, "guard_words");
  assert.equal(guardMiddle(Q, "the question is when the backfill is due").reason, "guard_prefix");
  assert.equal(guardMiddle(Q, "You're asking when the backfill is due... soon").reason, "guard_words");
  assert.equal(guardMiddle(Q, "").reason, "guard_prefix");
  assert.equal(guardMiddle(Q, 42).reason, "guard_prefix");
});

test("the prefix is case-insensitive and the assembled text lower-cases it", () => {
  const t = buildRestatement("when is the backfill due on site for the whole crew today", "You're asking when the backfill is due on site for the whole crew today.");
  assert.ok(t.startsWith("Got it, you're asking when"), t);
  assert.ok(!t.includes("today., "), "trailing punctuation removed");
});

test("a short result gets the filler phrase; still short means the opener", () => {
  const q = "when is the slab pour due";
  const m = "you're asking when the slab pour is due";
  const base = `Got it, ${m}, let me check your site records and pull up what was said on site... `;
  assert.ok(base.length < 141, String(base.length));
  const b = assessRestatement(q, m);
  assert.deepEqual(b, { reason: null,
    text: `Got it, ${m}, let me check your site records and pull up what was said on site, and this will only take a moment... ` });
  assert.ok(b.text.length >= 141);
  assert.deepEqual(assessRestatement("about the pour", "you're asking about the pour"), { text: null, reason: "too_short" });
});

test("the assembled text is stripped by toHistory so it never lands in history as an answer", () => {
  const t = buildRestatement(Q, GOOD);
  const { history } = toHistory([
    { role: "user", content: "first" }, { role: "assistant", content: " " + t + "  The backfill is due Friday." },
    { role: "user", content: "second" }]);
  assert.deepEqual(history, [{ question: "first", answer: "The backfill is due Friday." }]);
  assert.equal(toHistory([{ role: "user", content: "a" }, { role: "assistant", content: " " + t }, { role: "user", content: "b" }]).history.length, 0);
});

test("correction turns are detected", () => {
  for (const s of ["No, I meant level three", "no. the other one", "I meant the pour", "Nope, Friday", "i mean the other site"]) assert.equal(isCorrection(s), true, s);
  for (const s of ["Normal question about the pour", "what is the status", "Nobody was on site", "north wall progress"]) assert.equal(isCorrection(s), false, s);
});

test("reasoning-style model ids are rejected", () => {
  for (const m of ["google/gemini-3-flash", "x/y-thinking", "deepseek/deepseek-r1"]) assert.equal(modelRejected(m), true, m);
  assert.equal(modelRejected("meta-llama/llama-3.1-8b-instruct"), false);
});

test("the model request carries no reasoning field, max_tokens 40, temperature 0, and only questions", async () => {
  let sent;
  const fetchImpl = async (url, init) => { sent = { url, init, body: JSON.parse(init.body) };
    return { ok: true, status: 200, json: async () => ({ choices: [{ message: { content: ` "${GOOD}" ` } }] }) }; };
  const r = await restateViaModel({ question: Q, prevQuestion: "what is on today", model: "m/x", apiKey: "k",
    baseUrl: "http://or.test/v1/", fetchImpl });
  assert.deepEqual(r, { middle: GOOD });
  assert.equal(sent.url, "http://or.test/v1/chat/completions");
  assert.equal(sent.init.headers.Authorization, "Bearer k");
  assert.equal(sent.body.max_tokens, 40);
  assert.equal(sent.body.temperature, 0);
  assert.equal("reasoning" in sent.body, false);
  assert.equal(sent.body.messages[0].role, "system");
  assert.equal(sent.body.messages[0].content, RESTATE_SYSTEM);
  assert.ok(sent.body.messages[1].content.includes(Q) && sent.body.messages[1].content.includes("what is on today"));
  for (const phrase of ["you're asking", "18 words", "Never answer"]) assert.ok(RESTATE_SYSTEM.includes(phrase), phrase);
});

test("http failures and thrown fetches are reported, not thrown", async () => {
  const bad = await restateViaModel({ question: Q, model: "m", apiKey: "k", baseUrl: "http://x", fetchImpl: async () => ({ ok: false, status: 500 }) });
  assert.deepEqual(bad, { error: "http" });
  const boom = await restateViaModel({ question: Q, model: "m", apiKey: "k", baseUrl: "http://x", fetchImpl: async () => { throw new Error("net"); } });
  assert.deepEqual(boom, { error: "error" });
});

test("OPENER is still the 146-character fixed text", () => assert.equal(OPENER.length, 146));
