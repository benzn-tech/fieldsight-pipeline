// voice_llm/answer.mjs -- stream the synthesis with the request Python resolved.
// Never logs or retains the prompt or the answer text; text only goes to onText.

// Parse one SSE event block into its data payload ("" when it carries none).
// Comment lines (": ...") are skipped; "data:" may or may not be followed by a space.
function eventData(block) {
  const out = [];
  for (const line of block.split("\n")) {
    if (!line.startsWith("data:")) continue;
    out.push(line.slice(line.startsWith("data: ") ? 6 : 5));
  }
  return out.join("\n");
}

// No idle timeout after the first token: callers must pass `signal` (the whole-turn guard).
// ok === (chars > 0) in every outcome; a stream that ended abnormally after speech adds truncated:true + error.
export async function streamAnswer({ prompt, llm, baseUrl, apiKey, fetchImpl = fetch, firstTokenMs = 6000, onText, signal }) {
  const body = { model: llm.model, messages: [{ role: "user", content: prompt }], stream: true };
  if (llm.reasoning) body.reasoning = llm.reasoning;
  if (llm.max_tokens) body.max_tokens = llm.max_tokens;
  if (llm.temperature !== null && llm.temperature !== undefined) body.temperature = llm.temperature;
  const ac = new AbortController();
  let first = null, chars = 0, timedOut = false, callerAborted = false;
  const onCallerAbort = () => { callerAborted = true; ac.abort(); };
  if (signal) {
    if (signal.aborted) return { ok: false, chars: 0, firstTokenMs: null, error: "aborted" };
    signal.addEventListener("abort", onCallerAbort, { once: true });
  }
  const t0 = Date.now();
  const finish = (error, abnormal) => {
    const r = { ok: chars > 0, chars, firstTokenMs: first };
    if (error) r.error = error;
    if (chars > 0 && abnormal) r.truncated = true;
    return r;
  };
  const guard = setTimeout(() => { if (first === null) { timedOut = true; ac.abort(); } }, firstTokenMs);
  try {
    const res = await fetchImpl(`${baseUrl.replace(/\/$/, "")}/chat/completions`, {
      method: "POST", signal: ac.signal,
      headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    if (!res.ok) return { ok: false, chars: 0, firstTokenMs: null, error: `http_${res.status}` };
    const dec = new TextDecoder(); let buf = "", streamError = null, done = false;
    const handle = (block) => {
      const data = eventData(block);
      if (!data) return;
      if (data.trim() === "[DONE]") { done = true; return; }
      let msg; try { msg = JSON.parse(data); } catch { return; }
      if (msg?.error) { streamError = "stream_error"; done = true; return; }
      const delta = msg.choices?.[0]?.delta?.content;
      if (!delta) return;
      if (callerAborted) { done = true; return; }
      if (first === null) first = Date.now() - t0;
      chars += delta.length; onText(delta);
    };
    for await (const part of res.body) {
      if (callerAborted) break;
      buf += dec.decode(part, { stream: true });
      // Normalise CRLF/CR to LF; a trailing CR may be half of a CRLF, so hold it back.
      const held = buf.endsWith("\r") ? "\r" : "";
      buf = buf.slice(0, buf.length - held.length).replace(/\r\n?/g, "\n") + held;
      let i;
      while (!done && (i = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, i); buf = buf.slice(i + 2);
        handle(block);
      }
      if (done) break;
    }
    // Body ended: flush a final complete event that had no terminating blank line.
    if (!done && !callerAborted) handle((buf + dec.decode()).replace(/\r\n?/g, "\n").replace(/\n+$/, ""));
    if (callerAborted) return finish("aborted", true);
    if (streamError) return finish(streamError, true);
    return finish(chars > 0 ? undefined : "empty", false);
  } catch (e) {
    const error = callerAborted ? "aborted" : timedOut && first === null ? "first_token_timeout" : String(e?.message || e);
    return finish(error, true);
  } finally {
    clearTimeout(guard);
    if (signal) signal.removeEventListener("abort", onCallerAbort);
    ac.abort(); // release the connection if we stopped reading early
  }
}
