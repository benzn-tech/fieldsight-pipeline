import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { createHmac } from "node:crypto";
import { verifyVoiceSession } from "./token.mjs";

const f = JSON.parse(fs.readFileSync(new URL("../tests/fixtures/voice_session_token.json", import.meta.url)));
const b64 = (b) => Buffer.from(b).toString("base64url");

test("verifies the token Python minted", () => {
  assert.equal(verifyVoiceSession(f.token, f.key, f.now + 1), f.sub);
});

test("every tamper is refused", () => {
  const [p, s] = f.token.split(".");
  const flip = (x) => x.slice(0, -1) + (x.at(-1) === "A" ? "B" : "A");
  const audP = b64(JSON.stringify({ sub: f.sub, aud: "other", exp: f.expires_at }));
  const audS = b64(createHmac("sha256", f.key).update(audP).digest());
  for (const [label, tok, now] of [
    ["payload", flip(p) + "." + s, f.now],
    ["signature", p + "." + flip(s), f.now],
    ["aud", audP + "." + audS, f.now],
    ["expired", f.token, f.expires_at + 31],
    ["garbage", "nonsense", f.now],
    ["two dots", f.token + ".x", f.now],
  ]) assert.equal(verifyVoiceSession(tok, f.key, now), null, label);
});

test("an empty key refuses everything", () => {
  assert.equal(verifyVoiceSession(f.token, "", f.now), null);
});

test("inside the 30 s skew allowance still verifies", () => {
  assert.equal(verifyVoiceSession(f.token, f.key, f.expires_at + 29), f.sub);
});

const sign = (p) => b64(createHmac("sha256", f.key).update(p).digest());

test("non-ASCII payload char is refused (no low-byte truncation)", () => {
  const [p, s] = f.token.split(".");
  const bad = String.fromCharCode(p.charCodeAt(0) + 256) + p.slice(1);
  assert.equal(verifyVoiceSession(bad + "." + s, f.key, f.now), null);
});

test("a non-finite clock refuses", () => {
  assert.equal(verifyVoiceSession(f.token, f.key, undefined), null);
  assert.equal(verifyVoiceSession(f.token, f.key, NaN), null);
});

test("a non-string key refuses", () => {
  assert.equal(verifyVoiceSession(f.token, 123, f.now), null);
});

test("malformed inputs refuse", () => {
  const [p, s] = f.token.split(".");
  const notJson = b64("not json");
  const noExp = b64(JSON.stringify({ sub: f.sub, aud: "voice-llm" }));
  for (const [label, tok] of [
    ["empty p", "." + s],
    ["empty s", p + "."],
    ["empty", ""],
    ["non-string", 42],
    ["non-json", notJson + "." + sign(notJson)],
    ["no exp", noExp + "." + sign(noExp)],
  ]) assert.equal(verifyVoiceSession(tok, f.key, f.now), null, label);
});

test("exact boundary now = exp + 30 is refused", () => {
  assert.equal(verifyVoiceSession(f.token, f.key, f.expires_at + 30), null);
});
