// voice_llm/token.mjs -- verifier for org-api's voice-session tokens.
// Format and rules: spec 2026-10-14 phase 2 §2. Signed over the RECEIVED
// payload string; never re-serialise. Empty key refuses all.
import { createHmac, timingSafeEqual } from "node:crypto";

const AUD = "voice-llm";
const SKEW = 30;

export function verifyVoiceSession(token, key, nowSeconds) {
  if (typeof key !== "string" || !key || typeof token !== "string") return null;
  if (!Number.isFinite(nowSeconds)) return null;
  const parts = token.split(".");
  if (parts.length !== 2) return null;
  const [p, s] = parts;
  // Reject anything outside base64url before hashing: update(p, "ascii") would
  // otherwise truncate non-ASCII chars to their low byte and accept them.
  if (!/^[A-Za-z0-9_-]+$/.test(p) || !/^[A-Za-z0-9_-]{43}$/.test(s)) return null;
  const want = Buffer.from(createHmac("sha256", key).update(p, "ascii").digest("base64url"));
  const got = Buffer.from(s);
  if (got.length !== want.length || !timingSafeEqual(got, want)) return null;
  let body;
  try { body = JSON.parse(Buffer.from(p, "base64url").toString("utf8")); } catch { return null; }
  if (body?.aud !== AUD || !Number.isInteger(body?.exp) || body.exp <= nowSeconds - SKEW) return null;
  return typeof body.sub === "string" && body.sub ? body.sub : null;
}
