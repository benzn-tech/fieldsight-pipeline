// voice_llm/stream.mjs -- one ElevenLabs turn's SSE stream.
// Keep-alive is CONTENT (a single space): measured 2026-10-14, SSE comments do
// not keep a turn alive and any content gap of ~4 s kills it (spec §0 E1-E3).
export const OPENER = "Got it, let me check your site records for that now. I'm pulling up what was said and recorded on site today, and this will only take a moment... ";
export const FALLBACK = "Sorry, I couldn't reach your site records just now. Please try again.";

export function createTurnStream(out, { id, created, idleMs = 1000, setTimer = setTimeout, clearTimer = clearTimeout }) {
  let first = true, done = false, timer = null;
  const chunk = (content, finish = null) => {
    const delta = {};
    if (first) { delta.role = "assistant"; first = false; }
    if (content) delta.content = content;
    return `data: ${JSON.stringify({ id, object: "chat.completion.chunk", created,
      model: "fieldsight-voice", choices: [{ index: 0, delta, finish_reason: finish }] })}\n\n`;
  };
  const arm = () => {
    if (timer !== null) clearTimer(timer);
    timer = setTimer(() => { if (!done) { out.write(chunk(" ")); arm(); } }, idleMs);
  };
  const api = {
    text(s) { if (done || !s) return; out.write(chunk(s)); arm(); },
    finish() {
      if (done) return; done = true;
      if (timer !== null) clearTimer(timer);
      out.write(chunk("", "stop")); out.write("data: [DONE]\n\n"); out.end();
    },
    fail(line) { api.text(line); api.finish(); },
  };
  return api;
}
