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
  const guard = setTimeout(() => { if (first === null) { timedOut = true; ac.abort(); } }, firstTokenMs);
  try {
    const res = await fetchImpl(`${baseUrl.replace(/\/$/, "")}/chat/completions`, {
      method: "POST", signal: ac.signal,
      headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    if (!res.ok) return { ok: false, chars: 0, firstTokenMs: null, error: `http_${res.status}` };
    const dec = new TextDecoder(); let buf = "", streamError = null, done = false;
    for await (const part of res.body) {
      if (callerAborted) break;
      buf += dec.decode(part, { stream: true });
      // Normalise CRLF/CR to LF; a trailing CR may be half of a CRLF, so hold it back.
      const held = buf.endsWith("\r") ? "\r" : "";
      buf = buf.slice(0, buf.length - held.length).replace(/\r\n?/g, "\n") + held;
      let i;
      while (!done && (i = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, i); buf = buf.slice(i + 2);
        const data = eventData(block);
        if (!data) continue;
        if (data.trim() === "[DONE]") { done = true; break; }
        let msg; try { msg = JSON.parse(data); } catch { continue; }
        if (msg?.error) { streamError = "stream_error"; done = true; break; }
        const delta = msg.choices?.[0]?.delta?.content;
        if (!delta) continue;
        if (callerAborted) { done = true; break; }
        if (first === null) first = Date.now() - t0;
        chars += delta.length; onText(delta);
      }
      if (done) break;
    }
    if (callerAborted) return { ok: false, chars, firstTokenMs: first, error: "aborted" };
    if (chars > 0) return { ok: true, chars, firstTokenMs: first };
    return { ok: false, chars, firstTokenMs: first, error: streamError || "empty" };
  } catch (e) {
    const error = callerAborted ? "aborted" : timedOut && first === null ? "first_token_timeout" : String(e?.message || e);
    return { ok: false, chars, firstTokenMs: first, error };
  } finally {
    clearTimeout(guard);
    if (signal) signal.removeEventListener("abort", onCallerAbort);
    ac.abort(); // release the connection if we stopped reading early
  }
}
