// voice_llm/history.mjs -- ElevenLabs messages -> the Ask history shape.
// Our opener/restatement is a PREFIX of each assistant turn ending in "...";
// keep-alive spaces pad it. Both are ours, not answers (spec §4, review #10).
const text = (c) => typeof c === "string" ? c
  : Array.isArray(c) ? c.map((p) => typeof p === "string" ? p : p?.text || "").join(" ") : "";
const stripOurs = (s) => {
  const i = s.indexOf("...");
  return (i >= 0 ? s.slice(i + 3) : s).replace(/\s+/g, " ").trim();
};

export function toHistory(messages) {
  const turns = (Array.isArray(messages) ? messages : []).filter((m) => m && (m.role === "user" || m.role === "assistant"));
  let last = -1;
  for (let i = turns.length - 1; i >= 0; i--) if (turns[i].role === "user") { last = i; break; }
  const question = last >= 0 ? text(turns[last].content).trim() : "";
  const history = [];
  let pending = null;
  for (let i = 0; i < last; i++) {
    const m = turns[i];
    if (m.role === "user") pending = text(m.content).trim();
    else if (pending !== null) {
      const answer = stripOurs(text(m.content));
      if (answer) history.push({ question: pending, answer });
      pending = null;
    }
  }
  return { question, history };
}
