// voice_llm/restate.mjs -- the model-written restatement of the caller's question
// that replaces the fixed OPENER while VOICE_RESTATE_ENABLED=true (spec §3).
// Nothing here logs or retains question text. The guards are model-free on
// purpose: a restatement is spoken aloud before any record is read, so it may
// only rephrase what the caller said, never add to it.

export const DEFAULT_MODEL = "meta-llama/llama-3.1-8b-instruct";
export const DEADLINE_MS = 1800;
const MIN_CHARS = 141;     // under ~120 chars ElevenLabs' TTS holds the text back (measured 2026-10-14)
const MAX_WORDS = 25;
const TAIL = ", let me check your site records and pull up what was said on site";
const FILLER = ", and this will only take a moment";

export const RESTATE_SYSTEM = [
  "Restate the user's question as a short spoken phrase that starts with \"you're asking\".",
  "Use natural spoken English, at most 18 words.",
  "Keep any standard, product, quantity or location words exactly as the user said them.",
  "Never answer the question. Never add facts, names, dates or numbers that are not in the question.",
  "Output only the phrase.",
].join(" ");

// "No, I meant ..." re-asks something already restated; do not restate the correction.
export function isCorrection(question) {
  return /^\s*(no\s*[,.!]|nope\b|i\s+mean(t)?\b)/i.test(String(question ?? ""));
}

// Reasoning models burn the 40-token budget on thinking and answer with nothing.
export function modelRejected(model) {
  return /gemini-3|-thinking|r1/i.test(String(model ?? ""));
}

const words = (s) => String(s).split(/\s+/).filter(Boolean);
const numbers = (s) => String(s).match(/\d+(?:[.,]\d+)*/g) || [];
const bareWord = (w) => w.replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, "").replace(/['’]s$/i, "");

// {ok:true, middle} (trimmed, trailing punctuation removed, leading "You're"
// lower-cased for the splice into "Got it, ...") or {ok:false, reason}.
export function guardMiddle(question, middle) {
  const m = typeof middle === "string" ? middle.trim().replace(/[\s.,;:!?]+$/, "") : "";
  if (!/^you['’]re asking\b/i.test(m)) return { ok: false, reason: "guard_prefix" };
  if (words(m).length > MAX_WORDS || /\.\.\.|…|[\r\n]/.test(m)) return { ok: false, reason: "guard_words" };
  const q = String(question ?? "");
  if (numbers(m).some((n) => !numbers(q).includes(n))) return { ok: false, reason: "guard_number" };
  const known = new Set(words(q).map((w) => bareWord(w).toLowerCase()));
  const ws = words(m);
  for (let i = 1; i < ws.length; i++) {   // word 0 is "You're"
    const w = bareWord(ws[i]);
    if (/^\p{Lu}/u.test(w) && w !== "I" && !known.has(w.toLowerCase())) return { ok: false, reason: "guard_name" };
  }
  return { ok: true, middle: m[0].toLowerCase() + m.slice(1) };
}

// {text, reason}: text is the assembled restatement, or null with the reason.
export function assessRestatement(question, middle) {
  const g = guardMiddle(question, middle);
  if (!g.ok) return { text: null, reason: g.reason };
  const head = `Got it, ${g.middle}${TAIL}`;
  for (const t of [`${head}... `, `${head}${FILLER}... `]) if (t.length >= MIN_CHARS) return { text: t, reason: null };
  return { text: null, reason: "too_short" };
}

// The assembled text, or null when the caller should speak OPENER instead.
export function buildRestatement(question, middle) {
  return assessRestatement(question, middle).text;
}

// One non-streaming call. Resolves {middle} or {error:"http"|"error"}; never throws.
// Only the current question and, if any, the previous question are sent -- never
// answers or records.
export async function restateViaModel({ question, prevQuestion, model, apiKey, baseUrl, fetchImpl = fetch, signal }) {
  const user = prevQuestion
    ? `Previous question: ${prevQuestion}\nCurrent question: ${question}`
    : `Current question: ${question}`;
  try {
    const res = await fetchImpl(`${String(baseUrl).replace(/\/$/, "")}/chat/completions`, {
      method: "POST", signal,
      headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" },
      body: JSON.stringify({ model, max_tokens: 40, temperature: 0,
        messages: [{ role: "system", content: RESTATE_SYSTEM }, { role: "user", content: user }] }) });
    if (!res.ok) return { error: "http" };
    const j = await res.json();
    const c = j?.choices?.[0]?.message?.content;
    if (typeof c !== "string") return { error: "error" };
    return { middle: c.trim().replace(/^["'“]+|["'”]+$/g, "").trim() };
  } catch {
    return { error: "error" };
  }
}
